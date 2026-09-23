"""Ventana principal: panel lateral de parámetros y acciones, franja de totales y pestañas de análisis.

El cálculo de un período, la importación y las exportaciones corren en segundo
plano (Worker) con una ventana de avance que permite cancelarlos; la interfaz
solo se actualiza desde las señales que Qt entrega en el hilo principal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kpi_monitor import APP_TITLE, ORGANIZATION_NAME
from kpi_monitor.domain.models import Period, Thresholds
from kpi_monitor.domain.results import NETWORK_LABEL
from kpi_monitor.errors import AppError
from kpi_monitor.services import ImportReport, ObservationRow, Services
from kpi_monitor.ui.dialogs import TaskProgressDialog, run_modal, show_error, show_info, show_saved_file
from kpi_monitor.ui.editors import ImportPreviewDialog, ObservationDialog, ObservationTarget, SettingsDialog
from kpi_monitor.ui.formatting import MONTHS, format_pct, period_label
from kpi_monitor.ui.presenters import kpi_values, last_data_text, quality_text, scope_detail, scope_title
from kpi_monitor.ui.style import BAD, GOOD, WARNING
from kpi_monitor.ui.views import KEEP_SITE, TAB_TYPES, AnalysisTab, DataTab, ProgressTab, ViewContext
from kpi_monitor.ui.widgets import ElidedComboBox, KpiStrip, field_label, panel, section_title
from kpi_monitor.ui.workers import TaskRunner, Worker

log = logging.getLogger(__name__)

SIDE_WIDTH = 284
# Tarjetas de la franja superior: clave, título y ancho relativo.
KPI_CARDS = (
    ("index", "Índice a la fecha", 5),
    ("projected", "Proyectado al cierre", 5),
    ("green", "Verdes", 3),
    ("yellow", "Amarillos", 3),
    ("red", "Rojos", 3),
    ("no_data", "Sin datos", 3),
    ("weight", "Peso sin datos", 4),
    ("alerts", "Alertas", 4),
)


class MainWindow(QMainWindow):
    """Monitor de indicadores: filtros a la izquierda, totales arriba y análisis en pestañas."""

    def __init__(self, services: Services, export_dir: Path) -> None:
        super().__init__()
        self.services = services
        self.export_dir = export_dir
        self.tasks = TaskRunner(self)
        self._updating = False
        self._evaluating: Period | None = None
        self._shown_period: Period | None = None
        self._context: ViewContext | None = None
        self._dirty: set[int] = set()
        self.setWindowTitle(APP_TITLE)
        self.resize(1366, 860)
        self.setMinimumSize(1100, 700)

        central = QWidget(objectName="central")
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(10)
        root.addWidget(self._build_side_panel())
        root.addLayout(self._build_content(), 1)
        self.setCentralWidget(central)
        self._fill_indicators()
        self._update_threshold_legend()
        for view in self.views:
            view.show_unavailable("Calculando indicadores...")
        self.refresh()

    def _build_side_panel(self) -> QWidget:
        side = panel(self, "sidePanel")
        side.setFixedWidth(SIDE_WIDTH)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        layout.addWidget(QLabel("Monitor de indicadores", objectName="appTitle"))
        layout.addWidget(QLabel(ORGANIZATION_NAME, objectName="appSubtitle"))

        layout.addWidget(section_title("Período de corte"))
        period = self.services.monitor.default_period()
        self.year_combo = ElidedComboBox()
        for year in self.services.catalog.years():
            self.year_combo.addItem(str(year), year)
        self.year_combo.setCurrentIndex(max(0, self.year_combo.findData(period.year)))
        self.month_combo = ElidedComboBox()
        for number, name in enumerate(MONTHS, start=1):
            self.month_combo.addItem(name.capitalize(), number)
        self.month_combo.setCurrentIndex(period.month - 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        for label, combo, stretch in (("Año", self.year_combo, 2), ("Mes de corte", self.month_combo, 3)):
            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(field_label(label))
            column.addWidget(combo)
            row.addLayout(column, stretch)
        layout.addLayout(row)
        self.last_data_label = QLabel("", objectName="hint")
        self.last_data_label.setWordWrap(True)
        layout.addWidget(self.last_data_label)

        layout.addWidget(section_title("Filtros"))
        self.program_combo = ElidedComboBox(popup_width=280)
        self.program_combo.addItem("Todos los programas", "")
        for program in self.services.catalog.programs():
            self.program_combo.addItem(program.name, program.code)
        self.site_combo = ElidedComboBox()
        self.site_combo.addItem("Red (todas las sedes)", "")
        for site in self.services.catalog.sites():
            self.site_combo.addItem(site.name, site.code)
        self.indicator_combo = ElidedComboBox(popup_width=340)
        for label, combo in (
            ("Programa", self.program_combo),
            ("Sede", self.site_combo),
            ("Indicador", self.indicator_combo),
        ):
            layout.addWidget(field_label(label))
            layout.addWidget(combo)
        self.year_combo.currentIndexChanged.connect(self._on_scope_changed)
        self.program_combo.currentIndexChanged.connect(self._on_scope_changed)
        self.month_combo.currentIndexChanged.connect(self.refresh)
        self.site_combo.currentIndexChanged.connect(self.refresh)
        self.indicator_combo.currentIndexChanged.connect(self.refresh)

        layout.addWidget(section_title("Datos y reportes"))
        self.import_button = QPushButton("Importar datos...")
        self.import_button.setToolTip("Importa observaciones desde Excel o CSV (Ctrl+I).")
        self.import_button.setShortcut(QKeySequence("Ctrl+I"))
        self.import_button.clicked.connect(self.import_data)
        self.excel_button = QPushButton("Exportar reporte Excel...", objectName="primary")
        self.excel_button.setToolTip("Reporte ejecutivo del período en Excel (Ctrl+E).")
        self.excel_button.setShortcut(QKeySequence("Ctrl+E"))
        self.excel_button.clicked.connect(lambda: self.export_report("xlsx"))
        self.pdf_button = QPushButton("Exportar reporte PDF...")
        self.pdf_button.setToolTip("Reporte ejecutivo del período en PDF (Ctrl+P).")
        self.pdf_button.setShortcut(QKeySequence("Ctrl+P"))
        self.pdf_button.clicked.connect(lambda: self.export_report("pdf"))
        self.settings_button = QPushButton("Parámetros del semáforo...")
        self.settings_button.setToolTip("Umbrales del semáforo y prevalencia usada en la población de referencia.")
        self.settings_button.clicked.connect(self.edit_settings)
        self._action_buttons = (self.import_button, self.excel_button, self.pdf_button, self.settings_button)
        for button in self._action_buttons:
            layout.addWidget(button)

        layout.addWidget(section_title("Semáforo"))
        self.threshold_labels: list[QLabel] = []
        for color in (GOOD, WARNING, BAD):
            item = QHBoxLayout()
            item.setSpacing(8)
            swatch = QLabel()
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background: {color}; border-radius: 2px;")
            text = QLabel("", objectName="hint")
            self.threshold_labels.append(text)
            item.addWidget(swatch)
            item.addWidget(text, 1)
            layout.addLayout(item)
        note = QLabel(
            "Cumplimiento a la fecha respecto de la meta. La red suma las sedes; nunca toma el estado de la peor.",
            objectName="hint",
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return side

    def _build_content(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.scope_title_label = QLabel("", objectName="scopeTitle")
        self.scope_detail_label = QLabel("", objectName="scopeDetail")
        titles.addWidget(self.scope_title_label)
        titles.addWidget(self.scope_detail_label)
        header.addLayout(titles, 1)
        self.quality_label = QLabel("", objectName="scopeDetail")
        self.quality_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.quality_label)
        layout.addLayout(header)

        self.kpis = KpiStrip(self)
        for key, title, stretch in KPI_CARDS:
            self.kpis.add_card(key, title, stretch)
        layout.addWidget(self.kpis)

        self.tabs = QTabWidget(self)
        self.views: list[AnalysisTab] = []
        for tab_type in TAB_TYPES:
            view = tab_type(self.services, self)
            view.navigate.connect(self._navigate)
            self.views.append(view)
            self.tabs.addTab(view, view.title)
        self.data_tab = next(v for v in self.views if isinstance(v, DataTab))
        self.progress_tab = next(v for v in self.views if isinstance(v, ProgressTab))
        self.data_tab.edit_requested.connect(self.edit_observation)
        self.data_tab.export_requested.connect(self.export_observations)
        self.data_tab.template_requested.connect(self.export_template)
        self.tabs.currentChanged.connect(self._render_current)
        layout.addWidget(self.tabs, 1)
        return layout

    def period(self) -> Period:
        return Period(self.year_combo.currentData(), self.month_combo.currentData())

    def selected_site(self) -> str | None:
        return self.site_combo.currentData() or None

    def selected_program(self) -> str | None:
        return self.program_combo.currentData() or None

    def context(self) -> ViewContext:
        site = self.selected_site()
        return ViewContext(
            period=self.period(),
            site_code=site,
            site_name=self.site_combo.currentText() if site else NETWORK_LABEL,
            program_code=self.selected_program(),
            indicator_code=self.indicator_combo.currentData(),
            thresholds=self.services.catalog.settings().thresholds,
        )

    def _on_scope_changed(self) -> None:
        self._fill_indicators()
        self.refresh()

    def _fill_indicators(self) -> None:
        current = self.indicator_combo.currentData()
        previous = self._updating
        self._updating = True
        try:
            self.indicator_combo.clear()
            indicators = self.services.catalog.indicators(self.selected_program(), self.year_combo.currentData())
            for position, ind in enumerate(indicators):
                self.indicator_combo.addItem(f"{ind.code}  {ind.short_name}", ind.code)
                self.indicator_combo.setItemData(position, ind.name, Qt.ItemDataRole.ToolTipRole)
            self.indicator_combo.setCurrentIndex(max(0, self.indicator_combo.findData(current)))
        finally:
            self._updating = previous

    def _update_threshold_legend(self) -> None:
        thresholds = self.services.catalog.settings().thresholds
        texts = (
            f"Verde: desde {format_pct(thresholds.green, 0)}",
            f"Amarillo: desde {format_pct(thresholds.yellow, 0)}",
            f"Rojo: bajo {format_pct(thresholds.yellow, 0)}",
        )
        for label, text in zip(self.threshold_labels, texts, strict=True):
            label.setText(text)

    def refresh(self) -> None:
        """Muestra la selección actual; si el período no está calculado, lo evalúa en segundo plano."""
        if self._updating:
            return
        period = self.period()
        if self.services.monitor.is_evaluated(period):
            self._show()
        else:
            self._evaluate(period)

    def _evaluate(self, period: Period) -> None:
        if self._evaluating is not None:
            # Al terminar la evaluación en curso se vuelve a mirar la selección vigente.
            return
        self._evaluating = period
        self.statusBar().showMessage(f"Calculando indicadores de {period.label}...")
        worker = Worker(self.services.monitor.evaluate, period, with_progress=True)
        dialog = TaskProgressDialog(self, "Calculando indicadores", f"Evaluando {period.label}...", worker.cancel)
        self.tasks.start(
            worker,
            lambda _result: self._on_evaluated(dialog),
            lambda message: self._on_evaluation_failed(dialog, message),
            dialog.update_progress,
            lambda message: self._on_evaluation_cancelled(dialog, message),
        )

    def _on_evaluated(self, dialog: TaskProgressDialog) -> None:
        dialog.finish()
        self._evaluating = None
        self.refresh()

    def _on_evaluation_failed(self, dialog: TaskProgressDialog, message: str) -> None:
        dialog.finish()
        failed = self._evaluating
        self._evaluating = None
        if failed != self.period():
            self.refresh()
            return
        self.statusBar().showMessage("No se pudo evaluar el período elegido.")
        for view in self.views:
            view.show_unavailable(message)
        show_error(self, message, "No se pudo evaluar el período")

    def _on_evaluation_cancelled(self, dialog: TaskProgressDialog, message: str) -> None:
        dialog.finish()
        self._evaluating = None
        self.statusBar().showMessage(message)
        shown = self._shown_period
        if shown is not None and shown != self.period():
            self._updating = True
            try:
                self.year_combo.setCurrentIndex(self.year_combo.findData(shown.year))
                self._fill_indicators()
                self.month_combo.setCurrentIndex(shown.month - 1)
            finally:
                self._updating = False
            self.refresh()

    def _show(self) -> None:
        try:
            self._show_context(self.context())
        except AppError as error:
            log.warning("No se pudo mostrar la selección: %s", error.user_message)
            self.statusBar().showMessage(error.user_message)
            show_error(self, error.user_message)

    def _show_context(self, context: ViewContext) -> None:
        monitor = self.services.monitor
        summary = monitor.summary(context.period, context.site_code, context.program_code)
        programs = {p.code: p for p in self.services.catalog.programs()}
        for kpi in kpi_values(summary, context.thresholds, {code: p.name for code, p in programs.items()}):
            self.kpis.set_value(kpi.key, kpi.value, kpi.caption, kpi.tone, kpi.tooltip)
        index_program = programs.get(summary.index.program_code) if summary.index else None
        program = programs.get(context.program_code) if context.program_code else None
        self.scope_title_label.setText(scope_title(context.site_name, context.period))
        self.scope_detail_label.setText(scope_detail(context.site_code, program, index_program))
        quality = monitor.data_quality(context.period, context.site_code, context.program_code)
        self.quality_label.setText(quality_text(quality))
        last = monitor.last_month_with_data(context.period.year)
        self.last_data_label.setText(last_data_text(context.period, last))
        # Un corte posterior al último mes con datos se destaca: esos meses cuentan como no informados.
        self.last_data_label.setStyleSheet("" if last and last >= context.period.month else f"color: {WARNING};")
        self._shown_period = context.period
        self._context = context
        self._dirty = set(range(self.tabs.count()))
        self._render_current()
        self.statusBar().showMessage(f"Período evaluado: {context.period.label}.")

    def _render_current(self) -> None:
        index = self.tabs.currentIndex()
        if self._context is None or index not in self._dirty:
            return
        self._dirty.discard(index)
        self.views[index].render(self._context)

    def _navigate(self, indicator_code: str, site: object, open_progress: bool) -> None:
        """Lleva el panel lateral al indicador (y a la sede) elegidos en un gráfico o una tabla."""
        self._updating = True
        try:
            if site != KEEP_SITE:
                self.site_combo.setCurrentIndex(max(0, self.site_combo.findData(site or "")))
            if self.indicator_combo.findData(indicator_code) < 0:
                self.program_combo.setCurrentIndex(0)
                self._fill_indicators()
            self.indicator_combo.setCurrentIndex(max(0, self.indicator_combo.findData(indicator_code)))
        finally:
            self._updating = False
        self.refresh()
        if open_progress:
            self.tabs.setCurrentWidget(self.progress_tab)

    def _set_busy(self, busy: bool) -> None:
        for button in self._action_buttons:
            button.setEnabled(not busy)
        self.data_tab.set_actions_enabled(not busy)

    def _run_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        title: str,
        label: str,
        on_done: Callable[[Any], None],
        cancellable: bool = True,
        **kwargs: Any,
    ) -> Worker:
        """Ejecuta una acción del usuario en segundo plano con ventana de avance."""
        worker = Worker(fn, *args, with_progress=cancellable, **kwargs)
        dialog = TaskProgressDialog(self, title, label, worker.cancel if cancellable else None)
        self._set_busy(True)
        self.statusBar().showMessage(label)

        def finished(result: Any) -> None:
            dialog.finish()
            self._set_busy(False)
            on_done(result)

        def failed(message: str) -> None:
            dialog.finish()
            self._set_busy(False)
            self.statusBar().showMessage("La tarea no se completó.")
            show_error(self, message)

        def cancelled(message: str) -> None:
            dialog.finish()
            self._set_busy(False)
            self.statusBar().showMessage(message)

        self.tasks.start(worker, finished, failed, dialog.update_progress, cancelled)
        return worker

    def import_data(self) -> None:
        path_text, _ = QFileDialog.getOpenFileName(
            self, "Importar observaciones", str(self.export_dir), "Planillas (*.xlsx *.xlsm *.csv)"
        )
        if path_text:
            self.start_import(Path(path_text))

    def start_import(self, path: Path) -> Worker:
        """Valida el archivo en segundo plano y luego muestra la revisión antes de importar."""
        return self._run_task(
            self.services.data.validate_file,
            path,
            title="Importar datos",
            label=f"Revisando {path.name}...",
            on_done=lambda report: self._review_import(path, report),
            cancellable=False,
        )

    def _review_import(self, path: Path, report: ImportReport) -> None:
        if report.total_rows == 0:
            show_info(self, report.message, "Importar datos")
            return
        dialog = ImportPreviewDialog(self, path.name, report)
        if not run_modal(dialog) or dialog.choice is None:
            self.statusBar().showMessage("Importación cancelada; no se modificó la base.")
            return
        self.run_import(path, dialog.choice == ImportPreviewDialog.IMPORT_VALID)

    def run_import(self, path: Path, accept_valid_only: bool) -> Worker:
        """Importa el archivo en segundo plano (una sola transacción) con avance y cancelación."""
        return self._run_task(
            self.services.data.import_file,
            path,
            accept_valid_only=accept_valid_only,
            title="Importar datos",
            label=f"Importando {path.name}...",
            on_done=self._on_imported,
        )

    def _on_imported(self, report: ImportReport) -> None:
        self.statusBar().showMessage(report.message)
        show_info(self, report.message, "Importación terminada")
        if report.applied:
            self.refresh()

    def export_report(self, extension: str) -> None:
        period = self.period()
        suggested = self.export_dir / self.services.reports.default_filename(period, extension)
        filters = "Libro Excel (*.xlsx)" if extension == "xlsx" else "Documento PDF (*.pdf)"
        path_text, _ = QFileDialog.getSaveFileName(self, "Exportar reporte", str(suggested), filters)
        if path_text:
            self.run_report(period, Path(path_text), extension)

    def run_report(self, period: Period, path: Path, extension: str) -> Worker:
        """Genera el reporte ejecutivo en segundo plano con avance y cancelación."""
        method = self.services.reports.export_excel if extension == "xlsx" else self.services.reports.export_pdf
        return self._run_task(
            method,
            period,
            path,
            title="Exportar reporte",
            label=f"Generando el reporte de {period.label}...",
            on_done=lambda saved: self._on_saved(saved, "Reporte exportado"),
        )

    def export_template(self) -> None:
        suggested = self.export_dir / "plantilla_importacion.xlsx"
        path_text, _ = QFileDialog.getSaveFileName(
            self, "Descargar plantilla de importación", str(suggested), "Libro Excel (*.xlsx)"
        )
        if path_text:
            self._run_task(
                self.services.data.export_template,
                Path(path_text),
                title="Plantilla de importación",
                label="Generando la plantilla...",
                on_done=lambda saved: self._on_saved(saved, "Plantilla generada"),
                cancellable=False,
            )

    def export_observations(self) -> None:
        year = self.period().year
        suggested = self.export_dir / f"observaciones_{year}.xlsx"
        path_text, _ = QFileDialog.getSaveFileName(
            self, "Exportar observaciones", str(suggested), "Libro Excel (*.xlsx)"
        )
        if path_text:
            self._run_task(
                self.services.data.export_observations,
                Path(path_text),
                year,
                title="Exportar observaciones",
                label=f"Exportando las observaciones de {year}...",
                on_done=lambda saved: self._on_saved(saved, "Observaciones exportadas"),
                cancellable=False,
            )

    def _on_saved(self, path: Path, title: str) -> None:
        self.statusBar().showMessage(f"Archivo guardado en {path}")
        show_saved_file(self, path, title)

    def observation_dialog(self, record: ObservationRow | None) -> ObservationDialog:
        """Diálogo de registro o corrección de un dato, con la fila elegida o la selección actual."""
        catalog = self.services.catalog
        context = self.context()
        if record is not None:
            initial = ObservationTarget(record.site_code, record.indicator_code, record.year, record.month)
        else:
            site = context.site_code or catalog.sites()[0].code
            indicator = context.indicator_code or catalog.indicators(None, context.period.year)[0].code
            initial = ObservationTarget(site, indicator, context.period.year, context.period.month)
        return ObservationDialog(
            self,
            catalog.sites(),
            catalog.years(),
            lambda year: catalog.indicators(None, year),
            self._load_observation,
            self._save_observation,
            initial,
        )

    def edit_observation(self, record: ObservationRow | None = None) -> None:
        dialog = self.observation_dialog(record)
        if run_modal(dialog):
            target = dialog.target()
            when = period_label(target.year, target.month)
            self.statusBar().showMessage(f"Dato guardado: {target.indicator_code}, {target.site_code}, {when}.")
            self.refresh()

    def _load_observation(self, target: ObservationTarget) -> ObservationRow | None:
        rows = self.services.data.observations(target.year, target.month, target.site_code, target.indicator_code)
        return rows[0] if rows else None

    def _save_observation(self, target: ObservationTarget, numerator: str, denominator: str, reported: bool) -> None:
        self.services.data.save_observation(
            target.site_code, target.indicator_code, target.year, target.month, numerator, denominator, reported
        )

    def settings_dialog(self) -> SettingsDialog:
        settings = self.services.catalog.settings()
        return SettingsDialog(self, settings.thresholds, settings.prevalence, self._save_settings)

    def _save_settings(self, thresholds: Thresholds, prevalence: float) -> None:
        self.services.monitor.update_settings(thresholds, prevalence)

    def edit_settings(self) -> None:
        if run_modal(self.settings_dialog()):
            self.settings_saved()

    def settings_saved(self) -> None:
        """Actualiza la leyenda y recalcula después de guardar los parámetros."""
        self._update_threshold_legend()
        self.statusBar().showMessage("Parámetros guardados; se recalculan los indicadores.")
        self.refresh()

    def wait_until_idle(self, timeout_ms: int = 60_000) -> bool:
        """Espera a que terminen las tareas en segundo plano (autoprueba, capturas y tests)."""
        return self.tasks.wait_idle(timeout_ms)

    def autotest(self) -> list[str]:
        """Recorre todas las pestañas con dos selecciones, fuerza el dibujo y valida que haya datos."""
        if not self.wait_until_idle():
            return ["El cálculo de indicadores no terminó a tiempo."]
        if self._shown_period is None:
            return ["No se pudo evaluar el período inicial."]
        problems = [
            f"La tarjeta '{key}' de la franja de totales no tiene valor."
            for key in self.kpis.card_keys()
            if self.kpis.card(key).value_text in ("", "-")
        ]
        original = (self.site_combo.currentIndex(), self.program_combo.currentIndex(), self.tabs.currentIndex())
        problems += self._check_tabs()
        # Segunda pasada con una sede y el último programa (indicadores evaluados por cortes).
        self.site_combo.setCurrentIndex(1)
        self.program_combo.setCurrentIndex(self.program_combo.count() - 1)
        self.wait_until_idle()
        problems += self._check_tabs()
        self.site_combo.setCurrentIndex(original[0])
        self.program_combo.setCurrentIndex(original[1])
        self.wait_until_idle()
        self.tabs.setCurrentIndex(original[2])
        return problems

    def _check_tabs(self) -> list[str]:
        scope = f"{self.site_combo.currentText()}, {self.program_combo.currentText()}"
        found = []
        for index, view in enumerate(self.views):
            self.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            found.extend(f"{scope}: {problem}" for problem in view.problems())
        return found
