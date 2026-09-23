"""Ventana principal: panel de parámetros, franja de totales y pestañas de resultados."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from optibox import APP_TITLE
from optibox.domain.run import RunDetail, RunSummary
from optibox.domain.timegrid import week_monday
from optibox.errors import AppError
from optibox.services.excel_service import ExcelService, ImportReport
from optibox.services.master_data_service import MasterDataService
from optibox.services.planning_service import PlanningService
from optibox.ui.agenda_tab import ROOM_MODE, STAFF_MODE, AgendaTab
from optibox.ui.coverage_tab import CoverageTab
from optibox.ui.demand_tab import DemandTab
from optibox.ui.dialogs import ask_confirmation, run_dialog, show_error, show_info
from optibox.ui.formatting import format_count, format_decimal, format_hours, format_int, format_pct, format_seconds
from optibox.ui.hours_tab import HoursTab
from optibox.ui.load_tab import LoadTab
from optibox.ui.master_dialogs import AbsencesDialog, ContractDialog, ImportReportDialog, to_date, to_qdate
from optibox.ui.presentation import (
    coverage_tone,
    progress_text,
    run_info_text,
    run_list_text,
    status_label,
    week_range_text,
)
from optibox.ui.runs_tab import RunsTab
from optibox.ui.tab_base import TabPage
from optibox.ui.unmet_tab import UnmetTab
from optibox.ui.utilization_tab import UtilizationTab
from optibox.ui.widgets import KpiStrip, hint_label, panel, section_title
from optibox.ui.workers import TaskRunner, Worker

log = logging.getLogger(__name__)

SIDE_PANEL_WIDTH = 272
KPI_CARDS = (
    ("demand", "Demanda"),
    ("covered", "Cubierta"),
    ("coverage", "Cobertura"),
    ("unmet", "Turnos sin cubrir"),
    ("hours", "Horas asignadas"),
    ("rooms", "Utilización de salas"),
    ("staff_idle", "Horas ociosas del personal"),
    ("room_idle", "Horas ociosas de salas"),
)


class MainWindow(QMainWindow):
    """Ventana única de la aplicación."""

    def __init__(
        self,
        planning: PlanningService,
        master_data: MasterDataService,
        excel: ExcelService,
        export_dir: Path,
    ) -> None:
        super().__init__()
        self.planning = planning
        self.master_data = master_data
        self.excel = excel
        self.export_dir = export_dir
        self.tasks = TaskRunner(self)
        self.worker: Worker | None = None
        self.detail: RunDetail | None = None
        self.runs: tuple[RunSummary, ...] = ()
        self.setWindowTitle(APP_TITLE)
        self.resize(1366, 860)

        central = QWidget(objectName="central")
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        root.addWidget(self._build_side_panel(), 0)
        root.addLayout(self._build_content(), 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Listo")

        self._load_scenarios()
        self.reload_runs()
        initial = self.planning.initial_run()
        if initial is not None:
            self.show_run(initial.id)
        else:
            self._apply_detail(None)
        self.tabs.setFocus()

    def _build_side_panel(self) -> QWidget:
        side = panel(self)
        side.setFixedWidth(SIDE_PANEL_WIDTH)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        layout.addWidget(section_title("Planificación"))
        layout.addWidget(QLabel("Semana a planificar"))
        self.week_edit = QDateEdit(calendarPopup=True)
        self.week_edit.setDisplayFormat("dd-MM-yyyy")
        self.week_edit.setDate(to_qdate(self.planning.default_week()))
        self.week_edit.dateChanged.connect(self._update_week_hint)
        layout.addWidget(self.week_edit)
        self.week_hint = hint_label()
        layout.addWidget(self.week_hint)
        layout.addWidget(QLabel("Escenario"))
        self.scenario_combo = QComboBox()
        self.scenario_combo.currentIndexChanged.connect(self._update_scenario_hint)
        layout.addWidget(self.scenario_combo)
        self.scenario_hint = hint_label()
        layout.addWidget(self.scenario_hint)
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Límite de tiempo"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(5, 600)
        self.limit_spin.setSuffix(" s")
        self.limit_spin.setValue(round(self.planning.settings().time_limit_s))
        self.limit_spin.setToolTip("Tiempo máximo de búsqueda del optimizador, repartido entre sus dos fases.")
        limit_row.addWidget(self.limit_spin, 1)
        layout.addLayout(limit_row)
        buttons = QHBoxLayout()
        self.run_button = QPushButton("Optimizar", objectName="primary")
        self.run_button.clicked.connect(self.start_optimization)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_optimization)
        buttons.addWidget(self.run_button, 3)
        buttons.addWidget(self.cancel_button, 2)
        layout.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p %")
        layout.addWidget(self.progress)
        self.progress_label = hint_label("")
        layout.addWidget(self.progress_label)

        layout.addSpacing(6)
        layout.addWidget(section_title("Corridas guardadas"))
        self.run_list = QListWidget()
        self.run_list.setWordWrap(True)
        self.run_list.itemSelectionChanged.connect(self._on_run_selected)
        layout.addWidget(self.run_list, 1)
        self.export_plan_button = QPushButton("Exportar plan a Excel")
        self.export_plan_button.clicked.connect(self.export_plan)
        layout.addWidget(self.export_plan_button)

        layout.addSpacing(6)
        layout.addWidget(section_title("Datos maestros"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.absences_button = QPushButton("Ausencias")
        self.absences_button.clicked.connect(self.edit_absences)
        self.contract_button = QPushButton("Contratos")
        self.contract_button.clicked.connect(self.edit_contracts)
        self.import_button = QPushButton("Importar Excel")
        self.import_button.clicked.connect(self.import_master_data)
        self.export_master_button = QPushButton("Exportar Excel")
        self.export_master_button.clicked.connect(self.export_master_data)
        grid.addWidget(self.absences_button, 0, 0)
        grid.addWidget(self.contract_button, 0, 1)
        grid.addWidget(self.import_button, 1, 0)
        grid.addWidget(self.export_master_button, 1, 1)
        layout.addLayout(grid)
        self._update_week_hint()
        return side

    def _build_content(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        self.kpis = KpiStrip(self)
        for key, title in KPI_CARDS:
            self.kpis.add_card(key, title)
        layout.addWidget(self.kpis)
        self.run_info = QLabel("", objectName="runInfo")
        self.run_info.setWordWrap(True)
        layout.addWidget(self.run_info)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(False)
        self.coverage_tab = CoverageTab(self)
        self.load_tab = LoadTab(self)
        self.utilization_tab = UtilizationTab(self)
        self.hours_tab = HoursTab(self)
        self.unmet_tab = UnmetTab(self)
        self.agenda_tab = AgendaTab(self.planning, self)
        self.demand_tab = DemandTab(self.master_data, self)
        self.demand_tab.status_message.connect(self._status)
        self.runs_tab = RunsTab(self)
        self.runs_tab.show_requested.connect(self.show_run)
        self.runs_tab.delete_requested.connect(self.delete_run)
        self.pages: tuple[TabPage, ...] = (
            self.coverage_tab,
            self.load_tab,
            self.utilization_tab,
            self.hours_tab,
            self.unmet_tab,
            self.agenda_tab,
            self.demand_tab,
            self.runs_tab,
        )
        for page in self.pages:
            self.tabs.addTab(page, page.title)
        layout.addWidget(self.tabs, 1)
        return layout

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message, 15_000)

    def _selected_week(self) -> date:
        return to_date(self.week_edit.date())

    def _update_week_hint(self) -> None:
        monday = week_monday(self._selected_week())
        self.week_hint.setText(f"Se planifica de {week_range_text(monday)}.")

    def _load_scenarios(self) -> None:
        current = self.scenario_combo.currentData()
        self.scenario_combo.blockSignals(True)
        self.scenario_combo.clear()
        try:
            scenarios = self.planning.scenarios()
        except AppError as error:
            show_error(self, error.user_message)
            scenarios = ()
        for scenario in scenarios:
            self.scenario_combo.addItem(scenario.name, scenario.id)
            self.scenario_combo.setItemData(
                self.scenario_combo.count() - 1, scenario.description, Qt.ItemDataRole.ToolTipRole
            )
        self.scenario_combo.setCurrentIndex(max(0, self.scenario_combo.findData(current)))
        self.scenario_combo.blockSignals(False)
        self._update_scenario_hint()

    def _update_scenario_hint(self) -> None:
        index = self.scenario_combo.currentIndex()
        tooltip = self.scenario_combo.itemData(index, Qt.ItemDataRole.ToolTipRole) if index >= 0 else ""
        self.scenario_hint.setText(str(tooltip or ""))

    def reload_runs(self, select_id: int | None = None) -> None:
        try:
            self.runs = self.planning.list_runs()
        except AppError as error:
            show_error(self, error.user_message)
            self.runs = ()
        self.runs_tab.set_runs(self.runs)
        self.run_list.blockSignals(True)
        self.run_list.clear()
        for run in self.runs:
            item = QListWidgetItem(run_list_text(run))
            item.setData(Qt.ItemDataRole.UserRole, run.id)
            item.setToolTip(
                f"Estado {status_label(run.status).lower()}, heurística {format_pct(run.greedy_coverage_pct)}, "
                f"tiempo {format_seconds(run.total_seconds)}"
            )
            self.run_list.addItem(item)
        self.run_list.blockSignals(False)
        self._select_in_list(select_id if select_id is not None else self._current_id())

    def _current_id(self) -> int | None:
        return self.detail.summary.id if self.detail is not None else None

    def _select_in_list(self, run_id: int | None) -> None:
        self.run_list.blockSignals(True)
        self.run_list.clearSelection()
        for row in range(self.run_list.count()):
            item = self.run_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == run_id:
                item.setSelected(True)
                self.run_list.scrollToItem(item)
                break
        self.run_list.blockSignals(False)

    def _on_run_selected(self) -> None:
        items = self.run_list.selectedItems()
        if items:
            self.show_run(int(items[0].data(Qt.ItemDataRole.UserRole)))

    def show_run(self, run_id: int) -> None:
        """Carga una corrida guardada y actualiza totales y pestañas."""
        try:
            detail = self.planning.load_run(run_id)
        except AppError as error:
            show_error(self, error.user_message)
            return
        self._apply_detail(detail)
        self._select_in_list(run_id)
        self._status(f"Mostrando la corrida {run_id} ({detail.summary.scenario_name}).")

    def _apply_detail(self, detail: RunDetail | None) -> None:
        self.detail = detail
        self.export_plan_button.setEnabled(detail is not None)
        self._fill_kpis(detail)
        self.run_info.setText(
            run_info_text(detail) if detail else "Aún no hay corridas: elija una semana y presione Optimizar."
        )
        for page in self.pages:
            page.set_detail(detail)

    def _fill_kpis(self, detail: RunDetail | None) -> None:
        if detail is None:
            for key, _ in KPI_CARDS:
                self.kpis.set_value(key, "-", "sin corrida", "muted")
            return
        totals = detail.metrics.totals
        self.kpis.set_value(
            "demand",
            format_int(totals.demand_sessions),
            "sesiones requeridas",
            tooltip=f"Demanda ponderada por prioridad: {format_int(totals.weighted_demand)}",
        )
        self.kpis.set_value(
            "covered",
            format_int(totals.covered_sessions),
            f"sesiones, {format_count(totals.attentions, 'atención', 'atenciones')}",
            tooltip="Atenciones = sesiones por su cupo efectivo (un taller en grupo entrega varias).",
        )
        coverage = totals.coverage_pct
        self.kpis.set_value(
            "coverage",
            format_pct(coverage),
            f"ponderada {format_pct(totals.weighted_coverage_pct)}",
            coverage_tone(coverage),
            tooltip=(
                f"Heurística voraz: {format_pct(detail.summary.greedy_coverage_pct)}. "
                "Verde desde 95 %, ámbar desde 85 %."
            ),
        )
        self.kpis.set_value(
            "unmet",
            format_int(totals.unmet_sessions),
            f"en {format_count(totals.unmet_items, 'bloque y tipo', 'bloques y tipos')}",
            tooltip="Sesiones requeridas que no se asignaron; la causa está en la pestaña Turnos sin cubrir.",
        )
        self.kpis.set_value(
            "hours",
            format_decimal(totals.assigned_hours, 1),
            f"de atención y {format_hours(totals.admin_min)} adm.",
            tooltip=(
                f"Administrativo asociado a las sesiones: {format_hours(totals.admin_required_min)}; "
                f"colocado: {format_hours(totals.admin_min)}."
            ),
        )
        self.kpis.set_value(
            "rooms",
            format_pct(totals.room_utilization_pct),
            f"{format_hours(totals.room_session_min, 1)} de {format_hours(totals.room_capacity_min, 0)}",
            tooltip="Minutos de sesión sobre minutos disponibles de las salas de atención en la semana.",
        )
        self.kpis.set_value(
            "staff_idle",
            format_hours(totals.staff_idle_min, 1),
            "dentro del tiempo contratado disponible",
            tooltip="Tiempo contratado disponible sin permisos ni tareas productivas (pestaña Rendimiento de horas).",
        )
        self.kpis.set_value(
            "room_idle",
            format_hours(totals.room_idle_min, 1),
            f"de {format_hours(totals.room_capacity_min, 0)} abiertas",
            tooltip="Horas abiertas de las salas de atención sin atenciones asignadas.",
        )

    def _set_optimizing(self, running: bool) -> None:
        for widget in (
            self.run_button,
            self.week_edit,
            self.scenario_combo,
            self.limit_spin,
            self.import_button,
            self.absences_button,
            self.contract_button,
        ):
            widget.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.demand_tab.setEnabled(not running)
        self.runs_tab.delete_button.setEnabled(not running and bool(self.runs))

    def start_optimization(self) -> None:
        if self.tasks.busy:
            show_error(self, "Espere a que termine la tarea en curso.")
            return
        scenario_id = self.scenario_combo.currentData()
        if scenario_id is None:
            show_error(self, "No hay escenarios configurados.")
            return
        if self.demand_tab.has_changes:
            if not ask_confirmation(
                self,
                "La demanda tiene cambios sin guardar y la optimización usa la demanda guardada. "
                "¿Guardar los cambios y continuar?",
            ):
                return
            if not self.demand_tab.save_changes():
                return
        self._set_optimizing(True)
        self.progress.setValue(0)
        self.progress_label.setText("Preparando la optimización")
        self._status("Optimizando...")
        self.worker = Worker(
            self.planning.optimize,
            self._selected_week(),
            int(scenario_id),
            float(self.limit_spin.value()),
            with_progress=True,
        )
        self.tasks.start(
            self.worker,
            self._on_optimized,
            self._on_optimization_failed,
            self._on_progress,
            self._on_optimization_cancelled,
        )

    def cancel_optimization(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("Cancelando: se detiene la búsqueda en curso")
            self._status("Cancelando...")

    def _on_progress(self, pct: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, pct)))
        self.progress_label.setText(progress_text(message))

    def _on_optimized(self, result: object) -> None:
        self.worker = None
        self._set_optimizing(False)
        if not isinstance(result, RunDetail):
            return
        summary = result.summary
        self.progress.setValue(100)
        self.progress_label.setText(
            f"Corrida {summary.id}: {status_label(summary.status).lower()} en {format_seconds(summary.total_seconds)}"
        )
        self._apply_detail(result)
        self.reload_runs(summary.id)
        self._status(
            f"Corrida {summary.id} guardada: cobertura {format_pct(summary.coverage_pct)} "
            f"(heurística {format_pct(summary.greedy_coverage_pct)})."
        )

    def _on_optimization_failed(self, message: str) -> None:
        self.worker = None
        self._set_optimizing(False)
        self.progress.setValue(0)
        self.progress_label.setText("La optimización no se completó")
        self._status("La optimización no se completó")
        show_error(self, message, "No se pudo optimizar")

    def _on_optimization_cancelled(self, message: str) -> None:
        self.worker = None
        self._set_optimizing(False)
        self.progress.setValue(0)
        self.progress_label.setText("Optimización cancelada")
        self._status(message)

    def _run_task(self, message: str, fn: Callable[..., Any], *args: Any, on_done: Callable[[Any], None]) -> None:
        """Ejecuta una tarea de archivo en segundo plano con el cursor de espera."""
        if self.tasks.busy:
            show_error(self, "Espere a que termine la tarea en curso.")
            return
        buttons = (self.export_plan_button, self.import_button, self.export_master_button, self.run_button)
        for button in buttons:
            button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._status(message)

        def finish() -> None:
            QApplication.restoreOverrideCursor()
            for button in buttons:
                button.setEnabled(True)
            self.export_plan_button.setEnabled(self.detail is not None)

        def done(result: object) -> None:
            finish()
            on_done(result)

        def failed(error_message: str) -> None:
            finish()
            self._status("La tarea no se completó")
            show_error(self, error_message)

        self.tasks.start(Worker(fn, *args), done, failed)

    def export_plan(self) -> None:
        detail = self.detail
        if detail is None:
            show_error(self, "Primero seleccione o genere una corrida.")
            return
        default = self.export_dir / f"plan_corrida_{detail.summary.id}_{detail.instance.week_start:%Y-%m-%d}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Exportar plan", str(default), "Excel (*.xlsx)")
        if path:
            self._run_task(
                "Exportando el plan...",
                self.excel.export_run,
                detail.summary.id,
                Path(path),
                on_done=lambda saved: self._status(f"Plan exportado a {saved}"),
            )

    def export_master_data(self) -> None:
        default = self.export_dir / "datos_maestros.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Exportar datos maestros", str(default), "Excel (*.xlsx)")
        if path:
            self._run_task(
                "Exportando los datos maestros...",
                self.excel.export_master_data,
                Path(path),
                on_done=lambda saved: self._status(f"Datos maestros exportados a {saved}"),
            )

    def import_master_data(self) -> None:
        if self.demand_tab.has_changes and not ask_confirmation(
            self, "Hay cambios de demanda sin guardar que se perderán con la importación. ¿Continuar?"
        ):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Importar datos maestros", str(self.export_dir), "Excel (*.xlsx)")
        if not path:
            return
        sheets = ", ".join(self.excel.template_sheets())
        if not ask_confirmation(
            self,
            f"La importación reemplaza todos los datos maestros de la plantilla ({sheets}) si la planilla no "
            "tiene errores. Las corridas guardadas no cambian. ¿Continuar?",
        ):
            return
        self._run_task(
            "Importando datos maestros...", self.excel.import_master_data, Path(path), on_done=self._on_imported
        )

    def _on_imported(self, result: object) -> None:
        if not isinstance(result, ImportReport):
            return
        if not result.applied:
            self._status("Importación rechazada: no se modificó ningún dato")
            run_dialog(ImportReportDialog(result, self))
            return
        self._load_scenarios()
        self.demand_tab.reload()
        counts = "\n".join(
            f"{sheet}: {format_count(count, 'fila', 'filas')}" for sheet, count in result.rows_read.items()
        )
        self._status("Datos maestros importados")
        show_info(self, f"{result.summary}\n\n{counts}", "Importación completada")

    def edit_absences(self) -> None:
        try:
            dialog = AbsencesDialog(self.master_data, week_monday(self._selected_week()), self)
        except AppError as error:
            show_error(self, error.user_message)
            return
        run_dialog(dialog)
        if dialog.changed:
            self._status("Ausencias actualizadas: se consideran en la próxima optimización.")

    def edit_contracts(self) -> None:
        try:
            dialog = ContractDialog(self.master_data, week_monday(self._selected_week()), self)
        except AppError as error:
            show_error(self, error.user_message)
            return
        run_dialog(dialog)
        if dialog.saved:
            self._status("Contrato guardado: se usa desde su fecha de vigencia en las próximas optimizaciones.")

    def delete_run(self, run_id: int) -> None:
        if self.tasks.busy:
            show_error(self, "Espere a que termine la tarea en curso.")
            return
        if not ask_confirmation(self, f"¿Eliminar la corrida {run_id}? Esta acción no se puede deshacer."):
            return
        try:
            self.planning.delete_run(run_id)
        except AppError as error:
            show_error(self, error.user_message)
            return
        was_current = self._current_id() == run_id
        self.reload_runs()
        if was_current:
            if self.runs:
                self.show_run(self.runs[0].id)
            else:
                self._apply_detail(None)
        self._status(f"Corrida {run_id} eliminada.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tasks.busy:
            if not ask_confirmation(self, "Hay una tarea en curso. ¿Cancelarla y salir?"):
                event.ignore()
                return
            self.tasks.cancel_all()
            self.tasks.wait_all(15_000)
        elif self.demand_tab.has_changes and not ask_confirmation(
            self, "La demanda tiene cambios sin guardar. ¿Salir sin guardarlos?"
        ):
            event.ignore()
            return
        event.accept()

    def autotest(self) -> list[str]:
        """Recorre todas las pestañas, fuerza el dibujo de los gráficos y valida que haya datos."""
        problems: list[str] = []
        if self.detail is None:
            problems.append("No hay ninguna corrida cargada.")
        elif not self.detail.plan.sessions:
            problems.append("La corrida cargada no tiene sesiones.")
        for index in range(self.tabs.count()):
            self.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            page = self.tabs.widget(index)
            if not isinstance(page, TabPage):
                continue
            for chart in page.charts():
                chart.force_draw()
            if self.detail is not None or page is self.demand_tab:
                problems += [f"{self.tabs.tabText(index)}: {problem}" for problem in page.self_check()]
        if self.detail is not None:
            self.tabs.setCurrentWidget(self.agenda_tab)
            self.agenda_tab.set_mode(ROOM_MODE)
            QApplication.processEvents()
            self.agenda_tab.chart.force_draw()
            problems += [f"Agenda por sala: {problem}" for problem in self.agenda_tab.self_check()]
            self.agenda_tab.set_mode(STAFF_MODE)
            for key in self.kpis.card_keys():
                if self.kpis.card(key).value_text() in {"", "-"}:
                    problems.append(f"La tarjeta de totales '{key}' no tiene valor.")
        if self.run_list.count() == 0:
            problems.append("El historial de corridas está vacío.")
        self.tabs.setCurrentIndex(0)
        QApplication.processEvents()
        return problems
