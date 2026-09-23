"""Ventana principal: panel lateral, franja de totales y pestañas.

El panel lateral procesa una carpeta (en segundo plano, con progreso y
cancelación), fija los filtros que comparten todas las vistas y exporta los
informes. La franja superior muestra siempre los totales de los filtros
elegidos. Las pestañas son Revisión, Registro, Gráficos y Resumen.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from receipt_reader import APP_TITLE
from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import PeriodAxis, ReceiptStatus
from receipt_reader.domain.records import ReceiptRow
from receipt_reader.domain.reporting import ReceiptFilter, SummaryTable
from receipt_reader.domain.rut import format_rut
from receipt_reader.errors import AppError
from receipt_reader.services.app_services import AppServices
from receipt_reader.services.processing import BatchResult
from receipt_reader.services.reports import ExportResult, MonthlySeries, Overview
from receipt_reader.ui.charts import (
    EMPTY_HOURLY,
    EMPTY_MONTHLY,
    EMPTY_STATUS,
    draw_hourly_rates,
    draw_monthly_gross,
    draw_status_counts,
    hourly_groups,
    program_styles,
)
from receipt_reader.ui.dialogs import ask_confirmation, notify, open_path, show_error
from receipt_reader.ui.formatting import (
    format_clp,
    format_date,
    format_datetime,
    format_int,
    format_pct,
    format_quantity,
    month_label,
    plural,
)
from receipt_reader.ui.review_panel import ReviewPanel
from receipt_reader.ui.settings_dialog import ParametersDialog
from receipt_reader.ui.style import BAD, GOOD, TEXT_MUTED
from receipt_reader.ui.summary import MEASURES, SummaryMatrixModel, build_summary_matrix, ordered_programs
from receipt_reader.ui.widgets import (
    ChartCanvas,
    Column,
    KpiStrip,
    RecordTableModel,
    SearchProxyModel,
    StatusLabel,
    fit_columns_once,
    make_table_view,
    panel,
    section_title,
    source_row_of,
)
from receipt_reader.ui.workers import TaskRunner, Worker

log = logging.getLogger(__name__)

SIDE_PANEL_WIDTH = 290
VALID = frozenset({ReceiptStatus.APPROVED, ReceiptStatus.CORRECTED})
TO_REVIEW = frozenset({ReceiptStatus.PENDING, ReceiptStatus.ERROR})
STATUS_FILTERS: tuple[tuple[str, str, frozenset[ReceiptStatus] | None], ...] = (
    ("all", "Todos los estados", None),
    ("valid", "Válidas (aprobadas y corregidas)", VALID),
    ("review", "Por revisar (pendientes y con error)", TO_REVIEW),
    ("approved", "Aprobadas", frozenset({ReceiptStatus.APPROVED})),
    ("corrected", "Corregidas", frozenset({ReceiptStatus.CORRECTED})),
    ("pending", "Pendientes", frozenset({ReceiptStatus.PENDING})),
    ("error", "Con error de lectura", frozenset({ReceiptStatus.ERROR})),
    ("discarded", "Descartadas", frozenset({ReceiptStatus.DISCARDED})),
)
STATUS_SETS = {key: statuses for key, _label, statuses in STATUS_FILTERS}
# Colores de texto de estado en las tablas (versiones oscuras para que se lean sobre blanco).
STATUS_TEXT_COLORS = {
    ReceiptStatus.APPROVED: GOOD,
    ReceiptStatus.CORRECTED: GOOD,
    ReceiptStatus.PENDING: "#8A5A00",
    ReceiptStatus.ERROR: BAD,
    ReceiptStatus.DISCARDED: TEXT_MUTED,
}
EXCEL_FILTER = "Planillas Excel (*.xlsx)"
FOLDER_HELP = (
    "Carpeta con boletas en PDF o imagen. Convención opcional: <año-mes de pago>/<código y programa>/<archivo>."
)


def remaining_text(elapsed_seconds: float, percent: int) -> str:
    """Tiempo que falta según el ritmo hasta ahora; vacío mientras no hay base suficiente para estimarlo."""
    if percent < 3 or percent >= 100 or elapsed_seconds < 5:
        return ""
    minutes = elapsed_seconds * (100 - percent) / percent / 60
    if minutes < 1:
        return "queda menos de un minuto"
    if minutes < 1.5:
        return "queda cerca de un minuto"
    if minutes < 90:
        return f"quedan unos {round(minutes)} minutos"
    return f"quedan unas {round(minutes / 60)} horas"


def period_text(value: Period | None) -> str:
    return month_label(value.year, value.month) if value is not None else "-"


def record_search_text(row: ReceiptRow) -> str:
    """Texto en que busca el cuadro de búsqueda del registro."""
    parts = (
        str(row.id),
        row.status.label,
        row.issuer_name or "",
        row.issuer_rut or "",
        str(row.folio or ""),
        row.program_name or "",
        row.relative_path,
        row.issues_text,
    )
    return " ".join(parts)


def record_columns(short_name: Callable[[str], str]) -> list[Column]:
    """Columnas de la tabla Registro. `short_name` traduce el nombre del programa a su nombre corto."""
    return [
        Column("id", "N°", str, numeric=True),
        Column("status", "Estado", lambda status: status.label, color=lambda status: STATUS_TEXT_COLORS.get(status)),
        Column("issuer_name", "Prestador"),
        Column("issuer_rut", "RUT emisor", format_rut),
        Column("folio", "Folio", str, numeric=True),
        Column("issue_date", "Emisión", format_date),
        Column("service_period", "Servicio", period_text),
        Column("payment_period", "Pago", period_text),
        Column("program_name", "Programa", short_name),
        Column("gross", "Bruto", format_clp, numeric=True),
        Column("retention", "Retención", format_clp, numeric=True),
        Column("net", "Líquido", format_clp, numeric=True),
        Column("hours", "Horas", format_quantity, numeric=True),
        Column("hourly_rate", "Valor hora", format_clp, numeric=True),
        Column("text_kind", "Lectura", lambda kind: kind.label),
        Column("ocr_confidence", "Confianza OCR", lambda value: format_pct(value, 0), numeric=True),
        Column("issues_text", "Incidencias"),
        Column("relative_path", "Archivo"),
    ]


def kpi_values(overview: Overview) -> dict[str, tuple[str, str, str]]:
    """Valor, línea de contexto y tono de cada tarjeta de totales."""
    total = overview.total_receipts
    valid = overview.valid
    pending = overview.counts.get(ReceiptStatus.PENDING, 0)
    errors = overview.counts.get(ReceiptStatus.ERROR, 0)
    discarded = overview.discarded
    share = f"{format_pct(overview.approved / total, 0)} del total" if total else "sin boletas registradas"
    retention_share = (
        f"{format_pct(valid.retention / valid.gross, 1)} del bruto" if valid.gross else "sin boletas válidas"
    )
    return {
        "receipts": (
            format_int(total),
            f"incluye {plural(discarded, 'descartada')}" if discarded else "registradas",
            "neutral",
        ),
        "approved": (format_int(overview.approved), share, "good" if overview.approved else "neutral"),
        "pending": (
            format_int(overview.pending),
            f"{plural(pending, 'pendiente')}, {errors} con error",
            "warning" if overview.pending else "neutral",
        ),
        "gross": (format_clp(valid.gross), f"{plural(valid.count, 'boleta válida', 'boletas válidas')}", "neutral"),
        "retention": (format_clp(valid.retention), retention_share, "neutral"),
        "net": (format_clp(valid.net), "a pagar a los prestadores", "neutral"),
    }


class MainWindow(QMainWindow):
    """Ventana única de la aplicación."""

    def __init__(self, services: AppServices) -> None:
        super().__init__()
        self.services = services
        self.tasks = TaskRunner(self)
        # La biblioteca de PDF no admite uso concurrente: las vistas previas van de a una.
        self.preview_tasks = TaskRunner(self, max_threads=1)
        self._worker: Worker | None = None
        self._process_started = 0.0
        self._exporting = False
        self._unattended = False
        self._charts_dirty = True
        self._records_fitted = False
        self._rows: list[ReceiptRow] = []
        self._overview: Overview | None = None
        self._summary: SummaryTable | None = None
        self._monthly: MonthlySeries | None = None
        self._load_catalog()
        self.setWindowTitle(APP_TITLE)
        self.resize(1366, 860)
        self.setMinimumSize(1180, 720)

        central = QWidget(objectName="central")
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        root.addWidget(self._build_side_panel(), 0)
        root.addLayout(self._build_content(), 1)
        self.setCentralWidget(central)
        self.batch_label = QLabel("")
        self.statusBar().addPermanentWidget(self.batch_label)
        self.statusBar().showMessage("Listo")
        refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        refresh_shortcut.activated.connect(self.reload_all)
        self._reload_periods()
        self.refresh()

    # Catálogo

    def _load_catalog(self) -> None:
        catalog = self.services.catalog
        self._programs = catalog.programs()
        self._reference_rates = catalog.reference_rates()
        self._styles = program_styles(self._programs)
        self._short_names = {program.name: program.short_name for program in self._programs}

    def short_name(self, program_name: str) -> str:
        return self._short_names.get(program_name, program_name)

    # Construcción

    def _build_side_panel(self) -> QWidget:
        side = panel(self)
        side.setFixedWidth(SIDE_PANEL_WIDTH)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(1, 1, 1, 1)
        scroll = QScrollArea(objectName="plain")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("plainViewport")
        content = QWidget(objectName="plainViewport")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        layout.addWidget(section_title("Procesamiento"))
        layout.addWidget(QLabel("Carpeta de entrada", objectName="muted"))
        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        self.folder_edit = QLineEdit()
        # La ruta completa va en la ayuda: el campo es angosto y muestra solo el final.
        self.folder_edit.textChanged.connect(lambda text: self.folder_edit.setToolTip(f"{text}\n\n{FOLDER_HELP}"))
        self.folder_edit.setText(str(self.services.samples_dir))
        self.folder_edit.setCursorPosition(len(self.folder_edit.text()))
        self.browse_button = QToolButton()
        self.browse_button.setText("Elegir...")
        self.browse_button.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(self.browse_button)
        layout.addLayout(folder_row)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.process_button = QPushButton("Procesar carpeta", objectName="primary")
        self.process_button.setToolTip("Lee, valida y registra todos los archivos de la carpeta (y sus subcarpetas)")
        self.process_button.clicked.connect(self.process_folder)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setToolTip("Detiene el procesamiento y conserva lo ya registrado")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_processing)
        buttons.addWidget(self.process_button, 3)
        buttons.addWidget(self.cancel_button, 2)
        layout.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p %")
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.progress_label = StatusLabel("Listo para procesar.")
        self.progress_label.setMinimumHeight(2 * self.progress_label.fontMetrics().lineSpacing() + 4)
        layout.addWidget(self.progress_label)
        layout.addSpacing(10)

        layout.addWidget(section_title("Filtros"))
        self.axis_combo = self._combo(layout, "Eje del período")
        for axis in PeriodAxis:
            self.axis_combo.addItem(axis.label, axis.value)
        self.axis_combo.setToolTip("Período con que se agrupan los totales, los gráficos, el resumen y el Excel")
        self.program_combo = self._combo(layout, "Programa")
        self._fill_program_combo()
        self.status_combo = self._combo(layout, "Estado")
        for key, label, _statuses in STATUS_FILTERS:
            self.status_combo.addItem(label, key)
        self.period_combo = self._combo(layout, "Período")
        self.clear_filters_button = QPushButton("Quitar filtros", objectName="link")
        self.clear_filters_button.clicked.connect(self.clear_filters)
        layout.addWidget(self.clear_filters_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.axis_combo.currentIndexChanged.connect(self._on_axis_changed)
        for combo in (self.program_combo, self.status_combo, self.period_combo):
            combo.currentIndexChanged.connect(self.refresh)
        layout.addSpacing(10)

        layout.addWidget(section_title("Informes"))
        self.individual_check = QCheckBox("Incluir un informe por prestador")
        self.individual_check.setToolTip("Además del Excel principal, crea un archivo por prestador en una subcarpeta")
        layout.addWidget(self.individual_check)
        self.export_button = QPushButton("Exportar Excel...")
        self.export_button.setToolTip(
            "Hojas Base, Resumen, Pendientes y una por programa, con los filtros de programa y período"
        )
        self.export_button.clicked.connect(self.export_workbook)
        layout.addWidget(self.export_button)
        self.open_exports_button = QPushButton("Abrir carpeta de exportaciones", objectName="link")
        self.open_exports_button.clicked.connect(self.open_exports_folder)
        layout.addWidget(self.open_exports_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(10)

        layout.addWidget(section_title("Configuración"))
        self.parameters_button = QPushButton("Parámetros y catálogos...")
        self.parameters_button.setToolTip(
            "Organización receptora, ventana de fechas, rangos, valores hora de referencia y prestadores"
        )
        self.parameters_button.clicked.connect(self.open_parameters)
        layout.addWidget(self.parameters_button)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        return side

    @staticmethod
    def _combo(layout: QVBoxLayout, caption: str) -> QComboBox:
        layout.addWidget(QLabel(caption, objectName="muted"))
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
        layout.addWidget(combo)
        return combo

    def _fill_program_combo(self) -> None:
        current = self.program_combo.currentData()
        self.program_combo.blockSignals(True)
        self.program_combo.clear()
        self.program_combo.addItem("Todos los programas", None)
        for program in sorted(self._programs, key=lambda item: item.folder_code):
            self.program_combo.addItem(f"{program.folder_code} {program.short_name}", program.id)
        index = self.program_combo.findData(current) if current is not None else 0
        self.program_combo.setCurrentIndex(max(index, 0))
        self.program_combo.blockSignals(False)

    def _build_content(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(10)
        self.kpis = KpiStrip(self)
        for key, title in (
            ("receipts", "Boletas"),
            ("approved", "Aprobadas"),
            ("pending", "Por revisar"),
            ("gross", "Bruto total"),
            ("retention", "Retención total"),
            ("net", "Líquido total"),
        ):
            self.kpis.add_card(key, title)
        layout.addWidget(self.kpis)
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(False)
        self.review_panel = ReviewPanel(self.services, self.preview_tasks, self)
        self.review_panel.data_changed.connect(self._on_review_changed)
        self.review_panel.message.connect(self.show_message)
        self.tabs.addTab(self.review_panel, "Revisión")
        self.tabs.addTab(self._build_records_tab(), "Registro")
        self.tabs.addTab(self._build_charts_tab(), "Gráficos")
        self.tabs.addTab(self._build_summary_tab(), "Resumen")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        return layout

    def _build_records_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        self.records_search = QLineEdit()
        self.records_search.setPlaceholderText("Buscar por nombre, RUT, folio, programa, estado o archivo")
        self.records_search.setClearButtonEnabled(True)
        self.records_search.setMaximumWidth(460)
        self.records_search.textChanged.connect(self._on_records_search)
        bar.addWidget(self.records_search, 1)
        self.records_count = QLabel("", objectName="muted")
        bar.addWidget(self.records_count)
        bar.addStretch(1)
        self.open_in_review_button = QPushButton("Ver en revisión")
        self.open_in_review_button.setToolTip("Abre la boleta seleccionada en la pestaña Revisión (doble clic)")
        self.open_in_review_button.clicked.connect(self.open_selected_in_review)
        bar.addWidget(self.open_in_review_button)
        layout.addLayout(bar)
        self.records_model = RecordTableModel(record_columns(self.short_name), self)
        self.records_view = make_table_view(self.records_model, page, search_text=record_search_text, fit_columns=False)
        self.records_view.doubleClicked.connect(lambda _index: self.open_selected_in_review())
        layout.addWidget(self.records_view, 1)
        return page

    def _build_charts_tab(self) -> QWidget:
        page = QWidget()
        self.charts_page = page
        grid = QGridLayout(page)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setSpacing(6)
        self.monthly_chart = ChartCanvas(page)
        self.status_chart = ChartCanvas(page)
        self.hourly_chart = ChartCanvas(page)
        grid.addWidget(self.monthly_chart, 0, 0, 1, 2)
        grid.addWidget(self.status_chart, 1, 0)
        grid.addWidget(self.hourly_chart, 1, 1)
        grid.setRowStretch(0, 11)
        grid.setRowStretch(1, 10)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 3)
        return page

    def _build_summary_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Medida"))
        self.measure_combo = QComboBox()
        for key, label in MEASURES:
            self.measure_combo.addItem(label, key)
        self.measure_combo.currentIndexChanged.connect(self._update_summary)
        bar.addWidget(self.measure_combo)
        bar.addSpacing(12)
        self.summary_caption = QLabel("", objectName="muted")
        self.summary_caption.setWordWrap(True)
        bar.addWidget(self.summary_caption, 1)
        layout.addLayout(bar)
        self.summary_model = SummaryMatrixModel(self)
        view = QTableView(page)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setModel(self.summary_model)
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(28)
        header = view.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(90)
        self.summary_view = view
        layout.addWidget(view, 100)
        self.summary_empty = QLabel(
            "No hay boletas aprobadas o corregidas para los filtros elegidos.", objectName="muted"
        )
        self.summary_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary_empty.hide()
        layout.addWidget(self.summary_empty)
        layout.addSpacing(8)
        self.program_totals_title = section_title("Totales por programa")
        layout.addWidget(self.program_totals_title)
        self.program_totals_model = RecordTableModel(
            [
                Column("program", "Programa"),
                Column("count", "Boletas", format_int, numeric=True),
                Column("gross", "Bruto", format_clp, numeric=True),
                Column("retention", "Retención", format_clp, numeric=True),
                Column("net", "Líquido", format_clp, numeric=True),
                Column("share", "Participación en el bruto", lambda value: format_pct(value, 1), numeric=True),
            ],
            self,
            emphasize=lambda row: bool(row.get("total")),
        )
        totals_view = QTableView(page)
        totals_view.setModel(self.program_totals_model)
        totals_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        totals_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        totals_view.verticalHeader().setVisible(False)
        totals_view.verticalHeader().setDefaultSectionSize(28)
        totals_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        totals_header = totals_view.horizontalHeader()
        totals_header.setHighlightSections(False)
        totals_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.program_totals_view = totals_view
        layout.addWidget(totals_view)
        layout.addStretch(1)
        return page

    @staticmethod
    def _fit_table_height(view: QTableView, maximum: int | None = None) -> None:
        """Alto justo para mostrar todas las filas (con un máximo opcional; más allá, la tabla se desplaza)."""
        rows = view.model().rowCount()
        height = view.horizontalHeader().sizeHint().height() + rows * view.verticalHeader().defaultSectionSize() + 4
        if view.horizontalScrollBar().isVisible():
            height += view.horizontalScrollBar().sizeHint().height()
        view.setMaximumHeight(height if maximum is None else min(height, maximum))
        view.setMinimumHeight(min(height, 120))

    # Filtros

    def _axis(self) -> PeriodAxis:
        # Qt devuelve como texto lo que se guardó en el combo: se reconvierte al enum.
        value = self.axis_combo.currentData()
        return PeriodAxis(value) if value else PeriodAxis.SERVICE

    def _period(self) -> Period | None:
        value = self.period_combo.currentData()
        return Period.parse(str(value)) if value else None

    def current_filter(self) -> ReceiptFilter:
        key = self.status_combo.currentData()
        return ReceiptFilter(
            program_id=self.program_combo.currentData(),
            statuses=STATUS_SETS.get(str(key)) if key else None,
            period=self._period(),
            axis=self._axis(),
        )

    def _reload_periods(self) -> None:
        current = self.period_combo.currentData()
        self.period_combo.blockSignals(True)
        self.period_combo.clear()
        self.period_combo.addItem("Todos los períodos", None)
        try:
            periods = self.services.reports.periods(self._axis())
        except AppError as error:
            log.warning("No se pudieron leer los períodos: %s", error.user_message)
            periods = []
        for period in sorted(periods, reverse=True):
            self.period_combo.addItem(period.label(), period.iso())
        index = self.period_combo.findData(current) if current else 0
        self.period_combo.setCurrentIndex(max(index, 0))
        self.period_combo.blockSignals(False)

    def _on_axis_changed(self) -> None:
        self._reload_periods()
        self.refresh()

    def clear_filters(self) -> None:
        for combo in (self.axis_combo, self.program_combo, self.status_combo, self.period_combo):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self._reload_periods()
        self.refresh()

    def reload_all(self) -> None:
        self._reload_periods()
        self.refresh()
        self.show_message("Datos actualizados.")

    # Refresco

    def refresh(self) -> None:
        """Vuelve a consultar los totales y las vistas con los filtros actuales."""
        filt = self.current_filter()
        reports = self.services.reports
        try:
            overview = reports.overview(filt)
            rows = reports.rows(filt)
            summary = reports.summary(filt)
            monthly = reports.monthly_gross_by_program(filt)
            queue_filter = ReceiptFilter(program_id=filt.program_id, period=filt.period, axis=filt.axis)
            to_review = overview.pending if filt.statuses is None else reports.overview(queue_filter).pending
        except AppError as error:
            self._error(error.user_message)
            return
        except Exception:
            log.exception("Error inesperado al actualizar la ventana")
            self._error("No se pudieron leer los datos. El detalle quedó registrado en el log.")
            return
        self._overview = overview
        self._rows = rows
        self._summary = summary
        self._monthly = monthly
        for key, (value, caption, tone) in kpi_values(overview).items():
            self.kpis.set_value(key, value, caption, tone)
        self.records_model.set_rows(rows)
        if not self._records_fitted and rows:
            fit_columns_once(self.records_view, max_width=260)
            self._records_fitted = True
        self._update_records_count()
        self._update_summary()
        self._charts_dirty = True
        if self.tabs.currentWidget() is self.charts_page:
            self.draw_charts()
        self.review_panel.set_base_filter(queue_filter)
        self.tabs.setTabText(0, f"Revisión ({format_int(to_review)})" if to_review else "Revisión")
        self._update_batch_label()

    def _on_review_changed(self) -> None:
        self._reload_periods()
        self.refresh()

    def _update_batch_label(self) -> None:
        try:
            batch = self.services.processing.last_batch()
        except AppError:
            batch = None
        if batch is None:
            self.batch_label.setText("Sin procesamientos registrados")
            return
        files = plural(batch.files_processed, "archivo")
        self.batch_label.setText(f"Último procesamiento: {format_datetime(batch.started_at)} ({files})")
        self.batch_label.setToolTip(batch.root_path)

    def _update_records_count(self) -> None:
        total = self.records_model.rowCount()
        visible = self.records_view.model().rowCount()
        text = plural(total, "boleta")
        self.records_count.setText(f"{format_int(visible)} de {text}" if visible != total else text)
        self.open_in_review_button.setEnabled(visible > 0)

    def _on_records_search(self, text: str) -> None:
        proxy = self.records_view.model()
        if isinstance(proxy, SearchProxyModel):
            proxy.set_search(text)
        self._update_records_count()

    def _update_summary(self) -> None:
        summary = self._summary
        axis = self._axis()
        measure = str(self.measure_combo.currentData() or "gross")
        order = [program.id for program in sorted(self._programs, key=lambda item: item.folder_code)]
        matrix = build_summary_matrix(summary, measure, self._short_names, order) if summary is not None else None
        self.summary_model.set_matrix(matrix)
        empty = matrix is None or matrix.is_empty
        self.summary_view.setVisible(not empty)
        self.summary_empty.setVisible(empty)
        self._fit_table_height(self.summary_view)
        self.program_totals_model.set_rows(self._program_total_rows(order))
        self.program_totals_title.setVisible(not empty)
        self.program_totals_view.setVisible(not empty)
        self._fit_table_height(self.program_totals_view)
        unit = "Cantidad de boletas" if measure == "count" else "Montos en pesos"
        self.summary_caption.setText(
            f"Boletas aprobadas o corregidas, por {axis.label.lower()} y programa. {unit}; totales calculados "
            "en la base de datos."
        )

    def _program_total_rows(self, order: list[int]) -> list[dict[str, object]]:
        summary = self._summary
        if summary is None or not summary.program_totals:
            return []
        grand = summary.grand_total
        rows: list[dict[str, object]] = [
            {
                "program": self.short_name(item.program_name),
                "count": item.count,
                "gross": item.gross,
                "retention": item.retention,
                "net": item.net,
                "share": item.gross / grand.gross if grand.gross else None,
            }
            for item in ordered_programs(summary, order)
        ]
        rows.append(
            {
                "program": "Total",
                "count": grand.count,
                "gross": grand.gross,
                "retention": grand.retention,
                "net": grand.net,
                "share": 1.0 if grand.gross else None,
                "total": True,
            }
        )
        return rows

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.charts_page and self._charts_dirty:
            self.draw_charts()

    # Gráficos

    def draw_charts(self) -> None:
        """Dibuja los tres gráficos con los datos del último refresco."""
        self._charts_dirty = False
        monthly = self._monthly
        if monthly is None or not monthly.periods:
            self.monthly_chart.show_message(EMPTY_MONTHLY)
        else:
            figure = self.monthly_chart.clear()
            draw_monthly_gross(
                figure,
                monthly.periods,
                monthly.series,
                self._styles,
                axis=self._axis(),
                without_period=monthly.without_period,
            )
            self.monthly_chart.redraw()
        counts = self._overview.counts if self._overview is not None else {}
        if not any(counts.values()):
            self.status_chart.show_message(EMPTY_STATUS)
        else:
            draw_status_counts(self.status_chart.clear(), counts)
            self.status_chart.redraw()
        groups = hourly_groups(self._rows, self._programs, self._reference_rates)
        if not groups:
            self.hourly_chart.show_message(EMPTY_HOURLY)
        else:
            draw_hourly_rates(self.hourly_chart.clear(), groups)
            self.hourly_chart.redraw()

    # Procesamiento

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta de boletas", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(str(Path(folder)))

    def process_folder(self) -> None:
        if self._worker is not None:
            return
        text = self.folder_edit.text().strip()
        if not text:
            self._error("Indique la carpeta que contiene las boletas.")
            return
        folder = Path(text).expanduser()
        if not folder.is_dir():
            self._error(f"La carpeta no existe o no se puede abrir: {folder}")
            return
        self.preview_tasks.wait_all(15_000)
        self._set_processing(True)
        self._process_started = time.monotonic()
        self.progress.setFormat("%p %")
        self.progress.setValue(0)
        self.progress_label.set_full_text("Buscando archivos...")
        self.show_message(f"Procesando la carpeta {folder.name}...")
        worker = Worker(self.services.processing.process_folder, folder, with_progress=True)
        self._worker = worker
        self.tasks.start(worker, self._on_processed, self._on_process_failed, self._on_progress)

    def _set_processing(self, running: bool) -> None:
        self.progress.setTextVisible(True)
        self.process_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.folder_edit.setEnabled(not running)
        self.browse_button.setEnabled(not running)
        self.parameters_button.setEnabled(not running)
        self.review_panel.set_processing(running)

    def cancel_processing(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.progress_label.set_full_text("Cancelando: se conserva lo ya procesado...")

    def _on_progress(self, percent: int, message: str) -> None:
        value = max(0, min(100, percent))
        self.progress.setValue(value)
        remaining = remaining_text(time.monotonic() - self._process_started, value)
        self.progress.setFormat(f"%p %, {remaining}" if remaining else "%p %")
        self.progress_label.set_full_text(message)

    def _on_processed(self, result: BatchResult) -> None:
        self._worker = None
        self._set_processing(False)
        self.progress.setFormat("%p %")
        if not result.cancelled:
            self.progress.setValue(100)
        message = result.message()
        self.progress_label.set_full_text(message)
        self.show_message(message)
        self._reload_periods()
        self.refresh()
        if self._unattended:
            return
        pending = sum(result.status_counts.get(status, 0) for status in TO_REVIEW)
        actions = [("Ir a la cola de revisión", self.show_review)] if pending else []
        detail = ""
        if result.unreadable_files:
            detail = "Archivos que no se pudieron leer: " + ", ".join(result.unreadable_files)
        title = "Procesamiento cancelado" if result.cancelled else "Procesamiento terminado"
        notify(self, title, message, actions, detail=detail)

    def _on_process_failed(self, message: str) -> None:
        self._worker = None
        self._set_processing(False)
        self.progress.setFormat("%p %")
        self.progress_label.set_full_text("El procesamiento no se completó.")
        self.refresh()
        self._error(message)

    def show_review(self) -> None:
        self.tabs.setCurrentWidget(self.review_panel)

    # Registro

    def open_selected_in_review(self) -> None:
        row_index = source_row_of(self.records_view)
        if row_index is None:
            self.show_message("Seleccione una boleta del registro.")
            return
        row: ReceiptRow = self.records_model.row(row_index)
        self.tabs.setCurrentWidget(self.review_panel)
        if not self.review_panel.select_receipt(row.id):
            self.show_message(f"La boleta N° {row.id} no está disponible con los filtros actuales.")

    # Informes

    def export_workbook(self) -> None:
        if self._exporting:
            return
        folder = self.services.export_dir
        default = folder / f"boletas_honorarios_{date.today():%Y-%m-%d}.xlsx"
        target, _selected = QFileDialog.getSaveFileName(self, "Guardar informe Excel", str(default), EXCEL_FILTER)
        if not target:
            return
        path = Path(target)
        if path.suffix.lower() != ".xlsx":
            path = path.with_suffix(".xlsx")
        self.start_export(path)

    def start_export(self, path: Path) -> None:
        individual = path.parent if self.individual_check.isChecked() else None
        self._exporting = True
        self.export_button.setEnabled(False)
        self.show_message("Generando el informe Excel...")
        worker = Worker(
            self.services.reports.export_workbook, path, self.current_filter(), individual_folder=individual
        )
        self.tasks.start(worker, self._on_exported, self._on_export_failed)

    def _on_exported(self, result: ExportResult) -> None:
        self._exporting = False
        self.export_button.setEnabled(True)
        text = (
            f"Informe guardado en {result.workbook_path.name}: {plural(result.sheet_count, 'hoja')}, "
            f"{plural(result.receipt_count, 'boleta')}."
        )
        details: list[str] = []
        if result.saved_with_other_name:
            details.append(
                f"El archivo {result.requested_path.name} estaba abierto en otra aplicación, así que el informe "
                f"se guardó como {result.workbook_path.name}."
            )
        if result.individual_paths:
            folder = result.individual_paths[0].parent
            details.append(f"Además se crearon {len(result.individual_paths)} informes por prestador en {folder}.")
        self.show_message(text)
        if self._unattended:
            return
        workbook = result.workbook_path
        notify(
            self,
            "Informe Excel",
            text,
            [("Abrir informe", lambda: open_path(workbook)), ("Abrir carpeta", lambda: open_path(workbook.parent))],
            detail=" ".join(details),
        )

    def _on_export_failed(self, message: str) -> None:
        self._exporting = False
        self.export_button.setEnabled(True)
        self._error(message)

    def open_exports_folder(self) -> None:
        folder = self.services.export_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._error(f"No se pudo crear la carpeta {folder}.")
            return
        open_path(folder)

    # Parámetros

    def open_parameters(self) -> None:
        dialog = ParametersDialog(self.services, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda _result: self._after_parameters(dialog.data_changed))
        dialog.open()

    def _after_parameters(self, changed: bool) -> None:
        if not changed:
            return
        self._load_catalog()
        self._fill_program_combo()
        self.review_panel.reload_catalog()
        self._reload_periods()
        self.refresh()
        self.show_message("Parámetros actualizados y boletas revalidadas.")

    # Mensajes

    def show_message(self, text: str) -> None:
        self.statusBar().showMessage(text, 15_000)

    def _error(self, message: str) -> None:
        """Informa un error con un diálogo; en la autoprueba solo lo registra (no hay quien lo cierre)."""
        log.warning("%s", message)
        self.show_message(message)
        if not self._unattended:
            show_error(self, message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            if not self._unattended and not ask_confirmation(
                self,
                "Hay un procesamiento en curso. Si sale ahora se conserva lo ya registrado y se cancela el resto. "
                "¿Desea salir?",
                "Procesamiento en curso",
            ):
                event.ignore()
                return
            self._worker.cancel_event.set()
        if not self._unattended and self.review_panel.form.is_dirty():
            detail = self.review_panel.detail
            number = detail.record.id if detail else ""
            if not ask_confirmation(
                self, f"La boleta N° {number} tiene cambios sin guardar. ¿Desea salir sin guardarlos?", "Salir"
            ):
                event.ignore()
                return
        self.tasks.wait_all(30_000)
        self.preview_tasks.wait_all(10_000)
        super().closeEvent(event)

    # Autoprueba

    def wait_idle(self, timeout_ms: int = 60_000) -> None:
        """Espera las tareas en segundo plano y entrega sus resultados a la ventana."""
        for _ in range(3):
            self.tasks.wait_all(timeout_ms)
            self.preview_tasks.wait_all(timeout_ms)
            for _ in range(5):
                QApplication.processEvents()

    def set_unattended(self, unattended: bool = True) -> None:
        """Modo sin diálogos modales (autoprueba y capturas)."""
        self._unattended = unattended
        self.review_panel.unattended = unattended

    def autotest(self, *, check_ocr: bool = True) -> list[str]:
        """Recorre las pestañas, dibuja los gráficos y revisa que haya datos; devuelve los problemas."""
        self.set_unattended(True)
        problems: list[str] = []
        self.wait_idle()
        for index in range(self.tabs.count()):
            self.tabs.setCurrentIndex(index)
            QApplication.processEvents()
        for key in self.kpis.card_keys():
            if self.kpis.card(key).value_text() in ("", "-"):
                problems.append(f"La tarjeta de totales '{key}' no tiene valor.")
        problems += self._check_charts()
        problems += self._check_tables()
        self.tabs.setCurrentWidget(self.review_panel)
        QApplication.processEvents()
        problems += self.review_panel.autotest(self.wait_idle)
        problems += self._check_dialogs()
        problems += self._check_export()
        if check_ocr:
            problems += self.services.processing.check_ocr(self.services.samples_dir)
        return problems

    def _check_charts(self) -> list[str]:
        problems: list[str] = []
        self.draw_charts()
        charts: tuple[tuple[str, ChartCanvas, str], ...] = (
            ("bruto mensual por programa", self.monthly_chart, "patches"),
            ("boletas por estado", self.status_chart, "patches"),
            ("valor hora por programa", self.hourly_chart, "collections"),
        )
        for name, chart, artists in charts:
            chart.canvas.draw()
            if chart.message is not None:
                problems.append(f"El gráfico de {name} no tiene datos: {chart.message}")
                continue
            axes = chart.figure.axes
            if not axes or not any(getattr(ax, artists) for ax in axes):
                problems.append(f"El gráfico de {name} no dibujó datos.")
        return problems

    def _check_tables(self) -> list[str]:
        problems: list[str] = []
        if self.records_model.rowCount() == 0:
            problems.append("La tabla del registro está vacía.")
        else:
            first: ReceiptRow = self.records_model.row(0)
            self.records_search.setText(first.issuer_name or str(first.id))
            if self.records_view.model().rowCount() == 0:
                problems.append("La búsqueda del registro no encontró una boleta existente.")
            self.records_search.clear()
        for index in range(self.measure_combo.count()):
            self.measure_combo.setCurrentIndex(index)
            matrix = self.summary_model.matrix()
            if matrix is None or matrix.is_empty or self.summary_model.rowCount() == 0:
                problems.append(f"La tabla de resumen está vacía ({self.measure_combo.itemText(index)}).")
        self.measure_combo.setCurrentIndex(0)
        matrix = self.summary_model.matrix()
        if matrix is not None and self._overview is not None and matrix.grand_total != self._overview.valid.gross:
            problems.append("El total del resumen no coincide con el bruto total de la franja superior.")
        return problems

    def _check_dialogs(self) -> list[str]:
        problems: list[str] = []
        dialog = ParametersDialog(self.services, self)
        dialog.unattended = True
        try:
            if dialog.programs_model.rowCount() == 0:
                problems.append("El diálogo de parámetros no muestra los programas.")
            if dialog.rates_model.rowCount() == 0:
                problems.append("El diálogo de parámetros no muestra las tasas de retención.")
            dialog.form_settings()
        except (AppError, ValueError) as error:
            problems.append(f"Los parámetros guardados no son válidos en el formulario: {error}")
        finally:
            dialog.deleteLater()
        return problems

    def _check_export(self) -> list[str]:
        with tempfile.TemporaryDirectory() as folder:
            try:
                result = self.services.reports.export_workbook(Path(folder) / "autoprueba.xlsx", self.current_filter())
            except AppError as error:
                return [f"No se pudo exportar el Excel: {error.user_message}"]
            if not result.workbook_path.exists():
                return ["La exportación a Excel no creó el archivo."]
        return []
