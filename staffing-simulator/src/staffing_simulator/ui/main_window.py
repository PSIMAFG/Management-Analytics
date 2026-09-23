"""Ventana principal: panel lateral de escenarios, franja de totales y pestañas de análisis.

La ventana solo conversa con los servicios. Los cálculos del escenario
seleccionado son rápidos y se hacen al momento; la comparación de escenarios,
las importaciones y la exportación corren en segundo plano con avance y
opción de cancelar.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QProgressDialog,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from staffing_simulator import APP_TITLE
from staffing_simulator.domain.comparison import MAX_COMPARED, ScenarioComparison
from staffing_simulator.domain.imports import ImportReport
from staffing_simulator.domain.models import Catalog, Position, Scenario
from staffing_simulator.domain.projection import Projection
from staffing_simulator.domain.text import plural
from staffing_simulator.errors import AppError
from staffing_simulator.services import PositionResult, ScenarioKpis, ScenarioUpdateResult, Services
from staffing_simulator.services.scenario_service import inactive_summary
from staffing_simulator.ui import dialogs
from staffing_simulator.ui.capture import CAPTURE_SIZE, capture_tabs
from staffing_simulator.ui.formatting import format_clp
from staffing_simulator.ui.forms import ImportReviewDialog, NameDialog, PositionDialog, ScenarioDialog, WarningsDialog
from staffing_simulator.ui.presenters import (
    KPI_CARDS,
    identity_colors,
    kpi_texts,
    position_rows,
)
from staffing_simulator.ui.side_panel import SidePanel
from staffing_simulator.ui.style import SERIES
from staffing_simulator.ui.tabs.breakdown import BreakdownTab
from staffing_simulator.ui.tabs.budget import BudgetTab
from staffing_simulator.ui.tabs.comparison import ComparisonTab
from staffing_simulator.ui.tabs.monthly import MonthlyTab
from staffing_simulator.ui.tabs.other_expenses import OtherExpensesTab
from staffing_simulator.ui.tabs.parameters import ParametersTab
from staffing_simulator.ui.tabs.positions import PositionsTab
from staffing_simulator.ui.tabs.program_structure import ProgramStructureTab
from staffing_simulator.ui.widgets import ChartCanvas, KpiStrip
from staffing_simulator.ui.workers import TaskRunner, Worker

log = logging.getLogger(__name__)

EXCEL_FILTER = "Planillas Excel (*.xlsx)"
DEFAULT_COMPARED = 3
INITIAL_SIZE = (1366, 860)
# Tamaño mínimo: cabe en una pantalla de 1366x768 con la barra de tareas, y con él las tarjetas,
# las tablas y los gráficos pasan la autoprueba de diseño (también con seis escenarios).
MINIMUM_SIZE = (1280, 690)


def safe_filename(text: str) -> str:
    """Nombre de archivo legible y válido en Windows a partir de un texto libre."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text)
    return re.sub(r"\s+", " ", clean).strip(" .") or "informe"


class MainWindow(QMainWindow):
    def __init__(self, services: Services, export_dir: Path) -> None:
        super().__init__()
        self.services = services
        self.export_dir = export_dir
        self.tasks = TaskRunner(self)
        self.projection: Projection | None = None
        self.comparison: ScenarioComparison | None = None
        self._scenarios: list[Scenario] = []
        self._catalog = Catalog()
        self._compare_generation = 0
        self._compare_worker: Worker | None = None
        self._preferred_base: int | None = None
        self._first_load = True
        self.setWindowTitle(APP_TITLE)
        self.resize(*INITIAL_SIZE)
        self.setMinimumSize(*MINIMUM_SIZE)

        central = QWidget(objectName="central")
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(10)
        self.side = SidePanel(self)
        self.scenario_list = self.side.scenario_list
        root.addWidget(self.side, 0)
        content = QVBoxLayout()
        content.setSpacing(10)
        self.kpis = KpiStrip(self)
        for key, title in KPI_CARDS:
            self.kpis.add_card(key, title)
        content.addWidget(self.kpis)
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(False)
        self.positions_tab = PositionsTab(self)
        self.other_expenses_tab = OtherExpensesTab(services, self)
        self.program_structure_tab = ProgramStructureTab(services, self)
        self.monthly_tab = MonthlyTab(self)
        self.breakdown_tab = BreakdownTab(self)
        self.comparison_tab = ComparisonTab(self)
        self.budget_tab = BudgetTab(self)
        self.parameters_tab = ParametersTab(services, self._current_year, self)
        self.tabs.addTab(self.positions_tab, "Posiciones")
        self.tabs.addTab(self.other_expenses_tab, "Otros gastos")
        self.tabs.addTab(self.program_structure_tab, "Estructura del programa")
        self.tabs.addTab(self.monthly_tab, "Costo mensual")
        self.tabs.addTab(self.breakdown_tab, "Desglose")
        self.tabs.addTab(self.comparison_tab, "Comparación de escenarios")
        self.tabs.addTab(self.budget_tab, "Presupuesto y ejecución")
        self.tabs.addTab(self.parameters_tab, "Parámetros")
        content.addWidget(self.tabs, 1)
        root.addLayout(content, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Listo")
        self._connect()
        self.reload()

    # Conexiones

    def _connect(self) -> None:
        side = self.side
        side.scenario_changed.connect(self._on_scenario_changed)
        side.program_changed.connect(self._on_program_changed)
        side.new_requested.connect(self.new_scenario)
        side.duplicate_requested.connect(self.duplicate_scenario)
        side.rename_requested.connect(self.rename_scenario)
        side.delete_requested.connect(self.delete_scenario)
        side.apply_requested.connect(self.apply_assumptions)
        side.compare_changed.connect(self.refresh_comparison)
        side.import_positions_requested.connect(self.import_positions)
        side.import_execution_requested.connect(self.import_execution)
        side.export_requested.connect(self.export_report)
        side.positions_template_requested.connect(self.save_positions_template)
        side.execution_template_requested.connect(self.save_execution_template)
        self.positions_tab.add_requested.connect(self.add_position)
        self.positions_tab.edit_requested.connect(self.edit_position)
        self.positions_tab.delete_requested.connect(self.delete_positions)
        self.positions_tab.warnings_requested.connect(self.show_warnings)
        self.comparison_tab.base_changed.connect(self._on_base_changed)
        self.other_expenses_tab.changed.connect(self._on_parameters_changed)
        self.other_expenses_tab.import_requested.connect(self.import_other_expenses)
        self.other_expenses_tab.template_requested.connect(self.save_other_expenses_template)
        self.program_structure_tab.changed.connect(self._on_parameters_changed)
        self.parameters_tab.changed.connect(self._on_parameters_changed)

    # Carga y actualización

    def current_scenario_id(self) -> int | None:
        return self.side.current_id()

    def current_scenario(self) -> Scenario | None:
        scenario_id = self.current_scenario_id()
        return next((item for item in self._scenarios if item.id == scenario_id), None)

    def _current_year(self) -> int:
        scenario = self.current_scenario()
        return scenario.year if scenario is not None else date.today().year

    def reload(self, select_id: int | None = None) -> None:
        """Vuelve a leer los escenarios y actualiza todas las vistas."""
        try:
            self._scenarios = self.services.scenarios.list_scenarios()
            self._catalog = self.services.parameters.catalog()
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        ids = [item.id for item in self._scenarios]
        if self._first_load:
            compared = ids[:DEFAULT_COMPARED]
            self._first_load = False
        else:
            compared = [item for item in self.side.checked_ids() if item in ids]
        current = select_id if select_id in ids else self.current_scenario_id()
        self.side.set_scenarios(self._scenarios, current, compared)
        self.side.set_programs(list(self._catalog.programs))
        self.refresh()
        self.refresh_parameters()
        self.refresh_comparison()

    def _on_program_changed(self) -> None:
        self.refresh()
        self.refresh_comparison()

    def _on_scenario_changed(self) -> None:
        """Cambia de escenario; si los supuestos del anterior tienen cambios sin aplicar, pregunta qué hacer."""
        previous = self.side.shown_scenario()
        if previous is not None and previous.id != self.current_scenario_id():
            target = self.current_scenario_id()
            choice, result = self._ask_pending_assumptions()
            if choice == dialogs.CANCEL:
                self.side.select_scenario(previous.id)
                return
            if result is not None:
                self.reload(target)
                self._report_assumptions(result)
                return
        self.refresh()

    def _ask_pending_assumptions(self) -> tuple[str, ScenarioUpdateResult | None]:
        """Qué hacer con los supuestos editados sin aplicar antes de dejar el escenario mostrado.

        Devuelve (APPLY, resultado) si se aplicaron, (DISCARD, None) si se descartan
        o no había cambios, y (CANCEL, None) si el usuario desiste o no se pudieron aplicar.
        """
        scenario = self.side.shown_scenario()
        if scenario is None or not self.side.is_dirty():
            return dialogs.DISCARD, None
        choice = dialogs.ask_apply_changes(
            self, f"Los supuestos de «{scenario.name}» tienen cambios sin aplicar. ¿Qué desea hacer con ellos?"
        )
        if choice != dialogs.APPLY:
            return choice, None
        result = self._save_assumptions(scenario)
        return (dialogs.APPLY, result) if result is not None else (dialogs.CANCEL, None)

    def _settle_pending_assumptions(self) -> bool:
        """Antes de una acción que cambia de escenario: aplica o descarta los cambios pendientes (False si cancela)."""
        choice, result = self._ask_pending_assumptions()
        if choice == dialogs.CANCEL:
            return False
        if result is not None:
            self.reload(result.scenario.id)
            self._report_assumptions(result)
        else:
            self.side.discard_changes()
        return True

    def refresh(self) -> None:
        """Recalcula el escenario seleccionado: totales, posiciones, gráficos y tablas."""
        scenario = self.current_scenario()
        self.side.show_assumptions(scenario, self._scenarios)
        self.positions_tab.set_enabled_actions(scenario is not None)
        if scenario is None:
            self._clear_views("Cree un escenario con el botón Nuevo del panel lateral.")
            return
        program_id = self.side.program_id()
        self.positions_tab.set_scenario(scenario.name, scenario.description)
        self.other_expenses_tab.set_scenario(scenario, program_id)
        try:
            positions = self.services.scenarios.list_positions(scenario.id, program_id)
            warnings = len(self.services.scenarios.workload_warnings(scenario.id))
            warnings += len(self.services.scenarios.scenario_warnings(scenario.id))
        except AppError as error:
            self._clear_views(error.user_message)
            dialogs.show_error(self, error.user_message)
            return
        try:
            projection = self.services.costs.project(scenario.id, program_id)
            kpis = self.services.costs.kpis(scenario.id, projection, program_id)
            item_report = self.services.costs.item_report(scenario.id, projection, program_id)
            base = self._base_kpis(scenario, program_id)
        except AppError as error:
            log.warning("No se pudo costear el escenario %s: %s", scenario.name, error.user_message)
            self.projection = None
            summary = f"{plural(len(positions), 'registro', 'registros')} de posición; sin costo calculado."
            self.positions_tab.set_rows(position_rows(positions), summary, warnings)
            self._clear_views(f"No se puede costear el escenario: {error.user_message}", keep_positions=True)
            self.statusBar().showMessage(f"Escenario «{scenario.name}»: {error.user_message}")
            return
        self.projection = projection
        self._show_kpis(kpis, base)
        costs = {item.position.id: item for item in projection.position_costs()}
        inactive = sum(1 for item in costs.values() if item.active_months == 0)
        year = scenario.year
        summary = (
            f"{plural(len(positions), 'registro', 'registros')} de posición. Dotación {year}: "
            f"{plural(kpis.headcount, 'puesto', 'puestos')} ({plural(kpis.persons, 'persona', 'personas')} y "
            f"{plural(kpis.vacancies, 'vacante', 'vacantes')})."
        )
        if inactive:
            summary += " " + inactive_summary(inactive, len(positions), year)
        self.positions_tab.set_rows(position_rows(positions, costs, projection.weeks_per_month), summary, warnings)
        contract_colors = self._contract_colors()
        self.monthly_tab.show_projection(projection, item_report.total_assigned, contract_colors)
        self.breakdown_tab.show_projection(projection, contract_colors)
        self.budget_tab.show_data(item_report)
        programs = list(self._catalog.programs)
        if program_id is not None:
            programs = [program for program in programs if program.id == program_id]
        self.program_structure_tab.show_data(item_report, programs, scenario.year)
        status = (
            f"Escenario «{scenario.name}»: {plural(kpis.headcount, 'puesto', 'puestos')}, "
            f"costo anual {format_clp(kpis.total_cost)}"
        )
        if inactive:
            status += f"; {plural(inactive, 'registro', 'registros')} sin vigencia en {year}"
        self.statusBar().showMessage(status)

    def _base_kpis(self, scenario: Scenario, program_id: int | None) -> ScenarioKpis | None:
        if scenario.base_scenario_id is None:
            return None
        try:
            return self.services.costs.kpis(scenario.base_scenario_id, program_id=program_id)
        except AppError as error:
            log.info("No se pudo costear el escenario base: %s", error.user_message)
            return None

    def _show_kpis(self, kpis: ScenarioKpis, base: ScenarioKpis | None) -> None:
        texts = kpi_texts(kpis, base=base)
        for key, text in texts.items():
            self.kpis.set_value(key, text.value, text.caption, text.tone, text.tooltip)

    def _clear_views(self, message: str, *, keep_positions: bool = False) -> None:
        self.kpis.clear("Sin datos")
        if not keep_positions:
            self.positions_tab.set_rows([], "", 0)
        self.monthly_tab.clear(message)
        self.breakdown_tab.clear(message)
        self.budget_tab.clear(message)
        self.program_structure_tab.clear(message)

    def _contract_colors(self) -> dict[int, str]:
        return identity_colors([item.id for item in self._catalog.contract_types], SERIES)

    def refresh_parameters(self) -> None:
        try:
            self.parameters_tab.refresh()
        except AppError as error:
            dialogs.show_error(self, error.user_message)

    def _on_parameters_changed(self, message: str) -> None:
        try:
            self._catalog = self.services.parameters.catalog()
        except AppError as error:
            dialogs.show_error(self, error.user_message)
        self.refresh()
        self.refresh_comparison()
        self.statusBar().showMessage(f"{message}. Las proyecciones se recalcularon.")

    # Comparación en segundo plano

    def refresh_comparison(self) -> None:
        by_id = {item.id: item for item in self._scenarios}
        ids = [item for item in self.side.checked_ids() if item in by_id]
        if self._compare_worker is not None:
            self._compare_worker.cancel()
            self._compare_worker = None
        self._compare_generation += 1
        candidates = [by_id[item] for item in ids]
        base = self._choose_base(ids)
        self.comparison_tab.set_candidates(candidates, base)
        if len(ids) < 2:
            self.comparison = None
            self.comparison_tab.show_message("Marque al menos dos escenarios en el panel lateral para compararlos.")
            return
        if len(ids) > MAX_COMPARED:
            self.comparison = None
            self.comparison_tab.show_message(
                f"Marque como máximo {MAX_COMPARED} escenarios para que el gráfico se pueda leer."
            )
            return
        generation = self._compare_generation
        worker = Worker(self.services.costs.compare, ids, base, self.side.program_id(), with_progress=True)
        self._compare_worker = worker
        self.comparison_tab.set_busy(True)
        self.tasks.start(
            worker,
            partial(self._on_compared, generation),
            partial(self._on_compare_failed, generation),
            on_cancelled=lambda _message: None,
        )

    def _choose_base(self, ids: list[int]) -> int | None:
        if not ids:
            return None
        for candidate in (self.comparison_tab.base_id(), self._preferred_base):
            if candidate in ids:
                return candidate
        return ids[0]

    def _on_base_changed(self) -> None:
        self._preferred_base = self.comparison_tab.base_id()
        self.refresh_comparison()

    def _on_compared(self, generation: int, result: object) -> None:
        if generation != self._compare_generation or not isinstance(result, ScenarioComparison):
            return
        self._compare_worker = None
        self.comparison = result
        # Los colores se reparten solo entre los escenarios comparados (a lo más MAX_COMPARED, menos que la
        # paleta), en orden estable por identificador: dos escenarios de un mismo gráfico nunca comparten color.
        colors = identity_colors([item.scenario_id for item in result.scenarios], SERIES)
        self.comparison_tab.show_comparison(result, colors)

    def _on_compare_failed(self, generation: int, message: str) -> None:
        if generation != self._compare_generation:
            return
        self._compare_worker = None
        self.comparison = None
        self.comparison_tab.show_message(f"No se pudo comparar: {message}")
        self.statusBar().showMessage(f"No se pudo comparar: {message}")

    # Escenarios

    def new_scenario(self) -> None:
        if not self._settle_pending_assumptions():
            return
        dialog = ScenarioDialog(self.services, self._scenarios, self._current_year(), self)
        if dialogs.run_dialog(dialog) and isinstance(dialog.result_value, Scenario):
            self.reload(dialog.result_value.id)
            self.statusBar().showMessage(f"Escenario «{dialog.result_value.name}» creado")

    def duplicate_scenario(self) -> None:
        scenario = self.current_scenario()
        if scenario is None or not self._settle_pending_assumptions():
            return
        try:
            suggestion = self.services.scenarios.suggest_copy_name(scenario.id)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        dialog = NameDialog(
            "Duplicar escenario",
            suggestion,
            lambda name, _description: self.services.scenarios.duplicate_scenario(scenario.id, name),
            self,
            intro=f"Se copiarán los supuestos y todas las posiciones de «{scenario.name}».",
            save_text="Duplicar",
        )
        if dialogs.run_dialog(dialog) and isinstance(dialog.result_value, Scenario):
            self.reload(dialog.result_value.id)
            self.statusBar().showMessage(f"Escenario duplicado como «{dialog.result_value.name}»")

    def rename_scenario(self) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return

        def save(name: str, description: str | None) -> Scenario:
            draft = replace(scenario.to_draft(), name=name, description=description or "")
            return self.services.scenarios.update_scenario(scenario.id, draft)

        dialog = NameDialog("Renombrar escenario", scenario.name, save, self, description=scenario.description)
        if dialogs.run_dialog(dialog):
            self.reload(scenario.id)
            self.statusBar().showMessage("Escenario actualizado")

    def delete_scenario(self) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return
        try:
            count = len(self.services.scenarios.list_positions(scenario.id))
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        if count == 0:
            question = f"Se eliminará el escenario «{scenario.name}», que no tiene posiciones."
        else:
            records = "su registro de posición" if count == 1 else f"sus {count} registros de posición"
            question = f"Se eliminará el escenario «{scenario.name}» con {records}."
        dependents = [item.name for item in self._scenarios if item.base_scenario_id == scenario.id]
        detail = "Esta acción no se puede deshacer."
        if dependents:
            detail += " Los escenarios que lo usan como base quedarán sin base: " + ", ".join(dependents) + "."
        if not dialogs.ask_confirmation(
            self,
            question,
            "Eliminar escenario",
            "Eliminar",
            detail=detail,
        ):
            return
        try:
            self.services.scenarios.delete_scenario(scenario.id)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        self.reload()
        self.statusBar().showMessage(f"Escenario «{scenario.name}» eliminado")

    def apply_assumptions(self) -> None:
        scenario = self.side.shown_scenario()
        if scenario is None:
            return
        result = self._save_assumptions(scenario)
        if result is None:
            return
        self.reload(scenario.id)
        self._report_assumptions(result)

    def _save_assumptions(self, scenario: Scenario) -> ScenarioUpdateResult | None:
        """Guarda los supuestos editados del escenario; None si el usuario desiste o hay un error."""
        draft = self.side.assumptions_draft()
        if draft is None:
            return None
        if draft.year != scenario.year and not self._confirm_year_change(scenario, draft.year):
            return None
        try:
            return self.services.scenarios.update_scenario(scenario.id, draft)
        except AppError as error:
            dialogs.show_error(self, error.user_message, "No se pudieron aplicar los supuestos")
            return None

    def _confirm_year_change(self, scenario: Scenario, year: int) -> bool:
        """Pide confirmar un cambio de año que deja posiciones sin vigencia (y sin costo)."""
        try:
            total = len(self.services.scenarios.list_positions(scenario.id))
            inactive = len(self.services.scenarios.inactive_positions(scenario.id, year))
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return False
        if not inactive:
            return True
        return dialogs.ask_confirmation(
            self,
            f"Con el año {year}, {inactive_summary(inactive, total, year)}",
            "Cambiar el año del escenario",
            "Cambiar año",
            detail="Las posiciones no se modifican: revise sus fechas en la pestaña Posiciones si corresponde.",
        )

    def _report_assumptions(self, result: ScenarioUpdateResult) -> None:
        message = f"Supuestos de «{result.scenario.name}» actualizados"
        if result.warnings:
            message += ". " + " ".join(result.warnings)
        self.statusBar().showMessage(message)

    # Posiciones

    def add_position(self) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return
        self._open_position_dialog(scenario, None)

    def edit_position(self, position_id: int) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return
        try:
            position = self.services.scenarios.get_position(position_id)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        self._open_position_dialog(scenario, position)

    def _open_position_dialog(self, scenario: Scenario, position: Position | None) -> None:
        try:
            catalog = self.services.parameters.catalog()
            budget_items = self.services.financial.items(year=scenario.year)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        dialog = PositionDialog(self.services, scenario, catalog, budget_items, position, self)
        if not dialogs.run_dialog(dialog):
            return
        self._catalog = catalog
        self._data_changed("Posición guardada")
        result = dialog.result_value
        warnings = result.warnings if isinstance(result, PositionResult) else ()
        if warnings:
            dialogs.show_info(
                self,
                "La posición se guardó, pero la persona supera la jornada de referencia:",
                "Advertencia de jornada",
                detail="\n".join(warnings),
            )

    def delete_positions(self, position_ids: list[int]) -> None:
        if not position_ids:
            return
        count = len(position_ids)
        question = (
            "Se eliminará la posición seleccionada."
            if count == 1
            else f"Se eliminarán las {count} posiciones seleccionadas."
        )
        if not dialogs.ask_confirmation(self, question, "Eliminar posiciones", "Eliminar"):
            return
        try:
            self.services.scenarios.delete_positions(position_ids)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            self._data_changed("No se eliminó ninguna posición")
            return
        self._data_changed("Posición eliminada" if count == 1 else f"{count} posiciones eliminadas")

    def show_warnings(self) -> None:
        """Advertencias del escenario: jornada por persona, posiciones sin vigencia y parámetros faltantes."""
        scenario = self.current_scenario()
        if scenario is None:
            return
        try:
            warnings = self.services.scenarios.workload_warnings(scenario.id)
            notices = self.services.scenarios.scenario_warnings(scenario.id)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        dialogs.run_dialog(WarningsDialog(warnings, self, notices))

    def _data_changed(self, message: str) -> None:
        self.refresh()
        self.refresh_comparison()
        self.statusBar().showMessage(message)

    # Excel

    def _choose_open_path(self, title: str) -> Path | None:
        path, _selected = QFileDialog.getOpenFileName(self, title, str(self.export_dir), EXCEL_FILTER)
        return Path(path) if path else None

    def _choose_save_path(self, title: str, filename: str) -> Path | None:
        suggested = self.export_dir / f"{safe_filename(filename)}.xlsx"
        path, _selected = QFileDialog.getSaveFileName(self, title, str(suggested), EXCEL_FILTER)
        if not path:
            return None
        chosen = Path(path)
        return chosen if chosen.suffix.lower() == ".xlsx" else chosen.with_suffix(".xlsx")

    def run_task(
        self,
        title: str,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None],
        with_progress: bool = True,
        **kwargs: Any,
    ) -> Worker:
        """Ejecuta `fn` en segundo plano con un diálogo de avance (y Cancelar si la tarea lo admite)."""
        worker = Worker(fn, *args, with_progress=with_progress, **kwargs)
        progress = QProgressDialog(title, "Cancelar" if with_progress else "", 0, 100, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumWidth(380)
        if not with_progress:
            progress.setCancelButton(None)
            progress.setRange(0, 0)
        progress.setValue(0)

        done = False

        def request_cancel() -> None:
            worker.cancel()
            progress.setLabelText("Cancelando...")

        progress.canceled.connect(request_cancel)

        def close() -> None:
            nonlocal done
            done = True
            progress.blockSignals(True)
            progress.close()
            progress.deleteLater()

        def later(action: Callable[[], None]) -> None:
            # El resultado se entrega en una vuelta propia del ciclo de eventos, nunca anidado dentro de
            # `progress.setValue` (que procesa eventos cuando el diálogo modal está visible).
            QTimer.singleShot(0, self, action)

        def finished(result: Any) -> None:
            close()
            self.statusBar().showMessage(f"{title}: terminado")
            later(lambda: on_done(result))

        def failed(message: str) -> None:
            close()
            self.statusBar().showMessage(f"{title}: no se completó")
            later(lambda: dialogs.show_error(self, message))

        def cancelled(message: str) -> None:
            close()
            self.statusBar().showMessage(message)
            later(lambda: dialogs.show_info(self, message, "Operación cancelada"))

        def step(percent: int, message: str) -> None:
            # setValue puede entregar aquí mismo avances y el término que estaban en cola: los textos se
            # actualizan antes y un avance que llega después del término se ignora.
            if done:
                return
            progress.setLabelText(message)
            self.statusBar().showMessage(f"{title}: {message}")
            progress.setValue(percent)

        self.statusBar().showMessage(f"{title}...")
        return self.tasks.start(worker, finished, failed, step if with_progress else None, cancelled)

    def import_positions(self) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return
        path = self._choose_open_path("Importar posiciones")
        if path is None:
            return
        self.run_task(
            "Validando la planilla",
            self.services.excel.preview_positions,
            scenario.id,
            path,
            with_progress=False,
            on_done=partial(self._review_positions, scenario, path),
        )

    def _review_import(self, title: str, report: object, question: str, start_import: Callable[[bool], None]) -> None:
        """Revisa la validación de una planilla y, si el usuario confirma, lanza la importación.

        Con filas rechazadas muestra el detalle y ofrece importar solo las válidas;
        sin rechazos pide confirmar `question`. `start_import(solo_validas)` importa.
        """
        if not isinstance(report, ImportReport):
            return
        if report.total_rows == 0 or (report.accepted == 0 and not report.rejected):
            dialogs.show_info(self, report.summary, title)
            return
        if report.rejected:
            if not dialogs.run_dialog(ImportReviewDialog(title, report, self)) or not report.accepted:
                return
            start_import(True)
            return
        if dialogs.ask_confirmation(self, question, title, "Importar"):
            start_import(False)

    def _review_positions(self, scenario: Scenario, path: Path, report: object) -> None:
        question = ""
        if isinstance(report, ImportReport):
            verb = "Se agregará" if report.accepted == 1 else "Se agregarán"
            added = plural(report.accepted, "posición", "posiciones")
            question = f"{verb} {added} al escenario «{scenario.name}»."
            if report.created_persons:
                verb = "se registrará" if report.created_persons == 1 else "se registrarán"
                persons = plural(report.created_persons, "persona nueva", "personas nuevas")
                question += f" También {verb} {persons}."

        def start_import(only_valid: bool) -> None:
            self.run_task(
                "Importando posiciones",
                self.services.excel.import_positions,
                scenario.id,
                path,
                only_valid=only_valid,
                on_done=self._positions_imported,
            )

        self._review_import("Importar posiciones", report, question, start_import)

    def _positions_imported(self, report: object) -> None:
        if not isinstance(report, ImportReport):
            return
        self.reload(self.current_scenario_id())
        message = report.summary
        if report.created_persons:
            persons = plural(report.created_persons, "persona nueva", "personas nuevas")
            verb = "Se registró" if report.created_persons == 1 else "Se registraron"
            message += f" {verb} {persons}."
        dialogs.show_info(self, message, "Importación terminada")

    def import_other_expenses(self) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return
        path = self._choose_open_path("Importar otros gastos")
        if path is None:
            return
        self.run_task(
            "Validando la planilla",
            self.services.excel.preview_other_expenses,
            scenario.id,
            path,
            with_progress=False,
            on_done=partial(self._review_other_expenses, scenario, path),
        )

    def _review_other_expenses(self, scenario: Scenario, path: Path, report: object) -> None:
        question = ""
        if isinstance(report, ImportReport):
            verb = "Se agregará" if report.accepted == 1 else "Se agregarán"
            question = f"{verb} {plural(report.accepted, 'gasto', 'gastos')} al escenario «{scenario.name}»."

        def start_import(only_valid: bool) -> None:
            self.run_task(
                "Importando otros gastos",
                self.services.excel.import_other_expenses,
                scenario.id,
                path,
                only_valid=only_valid,
                on_done=self._other_expenses_imported,
            )

        self._review_import("Importar otros gastos", report, question, start_import)

    def _other_expenses_imported(self, report: object) -> None:
        if not isinstance(report, ImportReport):
            return
        self._data_changed("Otros gastos importados")
        dialogs.show_info(self, report.summary, "Importación terminada")

    def save_other_expenses_template(self) -> None:
        self._save_template(
            "Plantilla de otros gastos", "Plantilla otros gastos", self.services.excel.write_other_expenses_template
        )

    def import_execution(self) -> None:
        """Valida la planilla sin guardar y pide confirmación antes de registrar o reemplazar montos."""
        path = self._choose_open_path("Importar ejecución")
        if path is None:
            return
        self.run_task(
            "Validando la planilla",
            self.services.execution.preview_file,
            path,
            with_progress=False,
            on_done=partial(self._review_execution, path),
        )

    def _review_execution(self, path: Path, report: object) -> None:
        question = ""
        if isinstance(report, ImportReport):
            verb = "registrará" if report.accepted == 1 else "registrarán"
            question = f"Se {verb} {plural(report.accepted, 'monto ejecutado', 'montos ejecutados')}."
            if report.replaced:
                verb = "reemplazará" if report.replaced == 1 else "reemplazarán"
                question += f" Se {verb} {plural(report.replaced, 'monto ya registrado', 'montos ya registrados')}."

        def start_import(only_valid: bool) -> None:
            self.run_task(
                "Importando ejecución",
                self.services.execution.import_file,
                path,
                only_valid=only_valid,
                on_done=self._execution_imported,
            )

        self._review_import("Importar ejecución", report, question, start_import)

    def _execution_imported(self, report: object) -> None:
        if not isinstance(report, ImportReport):
            return
        if report.applied:
            self._data_changed("Ejecución importada")
        dialogs.show_info(self, report.summary, "Importación terminada")

    def export_report(self) -> None:
        scenario = self.current_scenario()
        if scenario is None:
            return
        path = self._choose_save_path("Exportar informe", f"Informe {scenario.name} {scenario.year}")
        if path is None:
            return
        compared = self.side.checked_ids()
        # La comparación del informe usa el mismo base que la pestaña de comparación.
        base = self.comparison_tab.base_id() if len(compared) >= 2 else None
        self.run_task(
            "Exportando el informe",
            self.services.excel.export_report,
            scenario.id,
            path,
            compared,
            base,
            on_done=self._report_exported,
        )

    def _offer_open(self, result: object, saved: str, title: str, open_text: str) -> None:
        """Informa dónde quedó un archivo (`saved`, por ejemplo «Informe guardado») y ofrece abrirlo."""
        path = Path(str(result))
        self.statusBar().showMessage(f"{saved}: {path.name}")
        if dialogs.ask_open_or_close(self, f"{saved} en:\n{path}", title, open_text):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _report_exported(self, result: object) -> None:
        self._offer_open(result, "Informe guardado", "Exportación terminada", "Abrir informe")

    def _save_template(self, title: str, filename: str, write: Callable[[Path, int], Path]) -> None:
        year = self._current_year()
        path = self._choose_save_path(title, f"{filename} {year}")
        if path is not None:
            self.run_task(
                "Creando la plantilla",
                write,
                path,
                year,
                with_progress=False,
                on_done=lambda result: self._offer_open(
                    result, "Plantilla guardada", "Plantilla lista", "Abrir plantilla"
                ),
            )

    def save_positions_template(self) -> None:
        self._save_template(
            "Plantilla de posiciones", "Plantilla posiciones", self.services.excel.write_positions_template
        )

    def save_execution_template(self) -> None:
        self._save_template("Plantilla de ejecución", "Plantilla ejecución", self.services.execution.write_template)

    # Cierre, autoprueba y capturas

    def closeEvent(self, event: QCloseEvent) -> None:
        choice, _result = self._ask_pending_assumptions()
        if choice == dialogs.CANCEL:
            event.ignore()
            return
        self.tasks.cancel_all()
        self.tasks.wait_all(10_000)
        super().closeEvent(event)

    def dispose(self) -> None:
        """Cierra la ventana y la destruye de inmediato en el hilo de la interfaz."""
        self.close()
        self.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def settle(self) -> None:
        """Espera las tareas en curso y procesa sus resultados (autoprueba y capturas)."""
        for _ in range(3):
            self.tasks.wait_all()
            for _ in range(5):
                QApplication.processEvents()
            if not self.tasks.busy:
                break

    def charts(self) -> list[ChartCanvas]:
        return self.findChildren(ChartCanvas)

    def autotest(self) -> list[str]:
        """Recorre las pestañas, fuerza el dibujo de los gráficos y valida que las vistas tengan datos."""
        problems: list[str] = []
        self.settle()
        for index in range(self.tabs.count()):
            self.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            name = self.tabs.tabText(index)
            page = self.tabs.widget(index)
            if page is self.parameters_tab:
                for sub in range(self.parameters_tab.pages.count()):
                    self.parameters_tab.pages.setCurrentIndex(sub)
                    QApplication.processEvents()
                self.parameters_tab.pages.setCurrentIndex(0)
            # La pestaña de comparación necesita al menos dos escenarios marcados: con un solo
            # escenario (por ejemplo, una base real con un único escenario cargado) su gráfico
            # queda vacío legítimamente, así que no cuenta como problema.
            skip_empty_chart = page is self.comparison_tab and len(self._scenarios) < 2
            for chart in page.findChildren(ChartCanvas):
                try:
                    chart.draw_if_pending()
                except Exception as error:
                    log.exception("Error al dibujar un gráfico de la pestaña %s", name)
                    problems.append(f"El gráfico de «{name}» no se pudo dibujar: {error}")
                    continue
                if not chart.has_data() and not skip_empty_chart:
                    problems.append(f"El gráfico de «{name}» no muestra datos: {chart.message or 'vacío'}")
        self.tabs.setCurrentIndex(0)
        QApplication.processEvents()

        checks = (
            (self.positions_tab.model, "La tabla de posiciones está vacía."),
            (self.program_structure_tab.model, "La tabla de estructura del programa está vacía."),
            (self.monthly_tab.model, "La tabla de costo mensual está vacía."),
            (self.breakdown_tab.model, "La tabla de desglose está vacía."),
            (self.budget_tab.model, "La tabla de presupuesto y ejecución está vacía."),
        )
        problems.extend(message for model, message in checks if model.rowCount() == 0)
        for page in self.parameters_tab.tables():
            if page.model.rowCount() == 0:
                problems.append("Una tabla de la pestaña Parámetros está vacía.")
        if self.projection is None or self.projection.total_cost <= 0:
            problems.append("El escenario seleccionado no tiene costo proyectado.")
        # La comparación necesita al menos dos escenarios: con datos reales puede haber uno
        # solo (por ejemplo, un único escenario "Dotación vigente"), así que estas dos
        # comprobaciones solo aplican cuando hay escenarios suficientes para comparar.
        if len(self._scenarios) >= 2:
            if self.comparison_tab.model.rowCount() == 0:
                problems.append("La tabla de comparación de escenarios está vacía.")
            if self.comparison is None:
                problems.append("La comparación de escenarios no se calculó.")
        if any(card.value_text() in {"", "-"} for card in self.kpis.cards().values()):
            problems.append("Hay tarjetas de totales sin valor.")
        problems.extend(self.layout_problems())
        return problems

    def layout_problems(self) -> list[str]:
        """Panel lateral angostado, textos cortados en las tarjetas o mal ubicados en los gráficos.

        La geometría del panel lateral se revisa siempre; los textos, solo con
        pantalla real (sin ella las fuentes no se miden igual).
        """
        real_screen = QGuiApplication.platformName() not in {"offscreen", "minimal"}
        problems = self.side.layout_problems(measure_text=real_screen)
        if not real_screen:
            return problems
        for key, card in self.kpis.cards().items():
            problems.extend(f"Texto cortado en la tarjeta «{key}»: {text}" for text in card.truncated_labels())
        tables = (
            (self.positions_tab, self.positions_tab.view),
            (self.other_expenses_tab, self.other_expenses_tab.view),
            (self.program_structure_tab, self.program_structure_tab.items_page.view),
            (self.monthly_tab, self.monthly_tab.view),
            (self.breakdown_tab, self.breakdown_tab.view),
            (self.budget_tab, self.budget_tab.view),
        )
        for page, view in tables:
            self.tabs.setCurrentWidget(page)
            QApplication.processEvents()
            if view.horizontalScrollBar().maximum() > 0:
                problems.append(f"La tabla de «{self.tabs.tabText(self.tabs.indexOf(page))}» no cabe a lo ancho.")
        for index in range(self.tabs.count()):
            self.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            for chart in self.tabs.widget(index).findChildren(ChartCanvas):
                chart.draw_now()
                problems.extend(
                    f"Texto mal ubicado en «{self.tabs.tabText(index)}»: {text}" for text in chart.clipped_texts()
                )
        self.tabs.setCurrentIndex(0)
        return problems

    def capture_screens(self, out_dir: Path, size: tuple[int, int] = CAPTURE_SIZE) -> list[Path]:
        """Guarda una captura por pestaña para la documentación."""
        self.settle()
        return capture_tabs(self, self.tabs, out_dir, size=size, settle=self.settle)
