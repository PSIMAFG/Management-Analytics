"""Pestañas de análisis de la ventana principal.

Cada pestaña recibe el contexto elegido en el panel lateral (período, sede,
programa e indicador), consulta los servicios y dibuja sus gráficos y tablas.
No calcula indicadores: lee los resultados del motor a través de los servicios.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from matplotlib.backend_bases import MouseEvent
from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableView,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from kpi_monitor.domain.alerts import Alert
from kpi_monitor.domain.enums import AlertKind, Severity, Status
from kpi_monitor.domain.models import Period, Thresholds
from kpi_monitor.domain.results import IndicatorResult
from kpi_monitor.domain.summary import StatusMatrix
from kpi_monitor.errors import AppError
from kpi_monitor.services import ObservationRow, Services
from kpi_monitor.ui.charts import (
    HeatmapLayout,
    ProgramSeries,
    draw_compliance,
    draw_heatmap,
    draw_index_history,
    draw_progress,
    draw_site_comparison,
    program_color,
    row_at,
)
from kpi_monitor.ui.formatting import MONTHS, format_int
from kpi_monitor.ui.presenters import (
    STATUS_SHORT,
    ObservationFilter,
    alert_rows,
    alerts_summary,
    compliance_text,
    filter_alerts,
    filter_observations,
    level_key,
    matrix_rows,
    matrix_tooltip,
    missing_rows,
    monthly_rows,
    observation_rows,
    order_observations,
    plural,
    result_details,
    result_row,
    sheet_html,
)
from kpi_monitor.ui.style import STATUS_COLORS
from kpi_monitor.ui.widgets import (
    ChartCanvas,
    Column,
    InfoPanel,
    RecordTableModel,
    StatusLegend,
    ViewSwitch,
    make_table_view,
    set_stretch_column,
    set_stretch_columns,
    source_row,
)

log = logging.getLogger(__name__)

# Marca para pedir que se conserve la sede elegida al navegar hacia un indicador.
KEEP_SITE = "__actual__"


@dataclass(frozen=True)
class ViewContext:
    """Selección del panel lateral con la que se dibujan las pestañas."""

    period: Period
    site_code: str | None
    site_name: str
    program_code: str | None
    indicator_code: str | None
    thresholds: Thresholds

    @property
    def scope(self) -> str:
        return f"{self.site_name}, {self.period.label}"


def cell_column(key: str, header: str, numeric: bool = False, center: bool = False) -> Column:
    """Columna cuyo valor es una celda de presentación (texto, orden, color y ayuda)."""
    return Column(
        key,
        header,
        formatter=lambda cell: cell.text,
        numeric=numeric,
        sort_key=lambda cell: cell.sort_value,
        background=lambda cell: cell.fill,
        tooltip=lambda cell: cell.tooltip or cell.text,
        center=center,
    )


def table_caption(text: str = "") -> QLabel:
    label = QLabel(text, objectName="tableCaption")
    label.setWordWrap(True)
    return label


def padded(widget: QWidget) -> QWidget:
    """Envuelve una tabla con un margen para que no toque los bordes de la pestaña."""
    block = QWidget()
    layout = QVBoxLayout(block)
    layout.setContentsMargins(8, 4, 8, 8)
    layout.addWidget(widget)
    return block


class AnalysisTab(QWidget):
    """Base de las pestañas: navegación hacia un indicador y chequeos de la autoprueba."""

    # Indicador, sede (o KEEP_SITE) y si se debe abrir el avance mensual.
    navigate = Signal(object, object, bool)

    title = ""

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.context: ViewContext | None = None

    def charts(self) -> list[tuple[str, ChartCanvas]]:
        return []

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return []

    def render(self, context: ViewContext) -> None:
        self.context = context
        try:
            self._render(context)
        except AppError as error:
            log.warning("No se pudo dibujar la pestaña %s: %s", self.title, error.user_message)
            self.show_unavailable(error.user_message)

    def _render(self, context: ViewContext) -> None:
        raise NotImplementedError

    def show_unavailable(self, message: str) -> None:
        for _name, chart in self.charts():
            chart.show_message(message)
        for _name, model in self.tables():
            model.set_rows([])

    def problems(self) -> list[str]:
        """Gráficos sin datos o tablas vacías (la autoprueba los informa como problemas)."""
        found = []
        for name, chart in self.charts():
            try:
                chart.draw_now()
            except Exception as error:  # la autoprueba debe informar cualquier falla de dibujo
                log.exception("Falla al dibujar el gráfico %s", name)
                found.append(f"El gráfico '{name}' de la pestaña '{self.title}' no se pudo dibujar: {error}")
                continue
            if not chart.has_data:
                found.append(f"El gráfico '{name}' de la pestaña '{self.title}' no tiene datos ({chart.message}).")
        found.extend(
            f"La tabla '{name}' de la pestaña '{self.title}' está vacía."
            for name, model in self.tables()
            if model.rowCount() == 0
        )
        return found


class ComplianceTab(AnalysisTab):
    """Cumplimiento a la fecha por indicador, evolución del índice ponderado y tabla de resultados."""

    title = "Cumplimiento vs meta"

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.chart = ChartCanvas(self, min_height=300)
        self.index_chart = ChartCanvas(self, min_height=300)
        self.model = RecordTableModel(
            [
                cell_column("indicator", "Indicador"),
                cell_column("value", "Valor", numeric=True),
                cell_column("goal_to_date", "Meta a\nla fecha", numeric=True),
                cell_column("annual_goal", "Meta anual"),
                cell_column("compliance", "Cumplimiento", numeric=True),
                cell_column("status", "Estado", center=True),
                cell_column("projection", "Proyección\nal cierre", numeric=True),
                cell_column("projected_compliance", "Cumplimiento\nal cierre", numeric=True),
                cell_column("probability", "Probabilidad\nde cumplir", numeric=True),
                cell_column("weight", "Peso", numeric=True),
            ]
        )
        self.view = make_table_view(self.model, self)
        self.view.doubleClicked.connect(self._on_table_double_click)
        self._codes: tuple[str, ...] = ()
        self._results: list[IndicatorResult] = []

        charts = QSplitter(Qt.Orientation.Horizontal)
        charts.addWidget(self.chart)
        charts.addWidget(self.index_chart)
        charts.setChildrenCollapsible(False)
        charts.setStretchFactor(0, 3)
        charts.setStretchFactor(1, 2)
        charts.setSizes([600, 400])
        self.switch = ViewSwitch([("Gráficos", charts), ("Tabla", padded(self.view))], self)
        self.caption = self.switch.caption
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.switch)
        self.chart.canvas.mpl_connect("button_press_event", self._on_chart_click)
        self.chart.canvas.mpl_connect("motion_notify_event", self._on_chart_hover)

    def charts(self) -> list[tuple[str, ChartCanvas]]:
        return [("Cumplimiento por indicador", self.chart), ("Índice ponderado mes a mes", self.index_chart)]

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return [("Resultados por indicador", self.model)]

    def show_unavailable(self, message: str) -> None:
        super().show_unavailable(message)
        self._codes = ()
        self._results = []
        self.caption.setText(message)

    def _render(self, context: ViewContext) -> None:
        monitor = self.services.monitor
        results = monitor.results(context.period, context.site_code, context.program_code)
        if results:
            self._codes = draw_compliance(self.chart.clear(), results, context.thresholds, context.scope)
            self._results = list(results)
            self.chart.redraw()
        else:
            self._codes = ()
            self.chart.show_message("No hay indicadores vigentes para los filtros elegidos.")
        self._draw_index(context)
        self.model.set_rows([result_row(r, order) for order, r in enumerate(results)])
        evaluated = sum(1 for r in results if r.status.is_evaluated)
        self.caption.setText(
            f"{context.scope}: {plural(len(results), 'indicador', 'indicadores')}, {evaluated} con semáforo. "
            "Doble clic en una barra o en una fila para ver su avance mensual."
        )

    def _draw_index(self, context: ViewContext) -> None:
        catalog = self.services.catalog
        monitor = self.services.monitor
        series = []
        for position, program in enumerate(catalog.programs()):
            if context.program_code is not None and program.code != context.program_code:
                continue
            if not catalog.indicators(program.code, context.period.year):
                continue
            values = monitor.index_history(context.period, program.code, context.site_code)
            index = monitor.index(context.period, program.code, context.site_code)
            if all(v is None for v in values):
                continue
            series.append(
                ProgramSeries(program.code, program.name, program_color(position), tuple(values), index.projected)
            )
        if not series:
            self.index_chart.show_message("No hay índice ponderado calculable para los filtros elegidos.")
            return
        draw_index_history(self.index_chart.clear(), series, context.period, context.thresholds)
        self.index_chart.redraw()

    def _on_chart_hover(self, event: MouseEvent) -> None:
        row = row_at(event.ydata, len(self._results)) if event.inaxes is not None else None
        self.chart.show_tooltip(None if row is None else matrix_tooltip(self._results[row]))

    def _on_chart_click(self, event: MouseEvent) -> None:
        if not self._codes or event.inaxes is None:
            return
        row = row_at(event.ydata, len(self._codes))
        if row is not None:
            self.navigate.emit(self._codes[row], KEEP_SITE, bool(event.dblclick))

    def _on_table_double_click(self, index: QModelIndex) -> None:
        row = source_row(self.view, index)
        if row is not None:
            self.navigate.emit(self.model.row(row)["code"], KEEP_SITE, True)


class ProgressTab(AnalysisTab):
    """Avance mensual y acumulado del indicador elegido, con meta, proyección y banda."""

    title = "Avance mensual y acumulado"

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.chart = ChartCanvas(self, min_height=280)
        self.model = RecordTableModel(
            [
                cell_column("month", "Mes"),
                cell_column("reported", "Informado", center=True),
                cell_column("numerator", "Numerador", numeric=True),
                cell_column("denominator", "Denominador", numeric=True),
                cell_column("monthly_value", "Valor\ndel mes", numeric=True),
                cell_column("cumulative", "Acumulado", numeric=True),
                cell_column("goal_to_date", "Meta a\nla fecha", numeric=True),
                cell_column("compliance", "Cumplimiento", numeric=True),
                cell_column("status", "Estado", center=True),
            ]
        )
        self.view = make_table_view(self.model, self, sortable=False)
        self.info = InfoPanel("Indicador", self)
        self.info.setFixedWidth(300)
        self.switch = ViewSwitch([("Gráfico", self.chart), ("Tabla mensual", padded(self.view))], self)
        self.switch.caption.setText("Los meses posteriores al corte quedan pendientes; un mes no informado no es cero.")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.switch, 1)
        info_column = QVBoxLayout()
        info_column.setContentsMargins(0, 8, 0, 0)
        info_column.addWidget(self.info)
        layout.addLayout(info_column)

    def charts(self) -> list[tuple[str, ChartCanvas]]:
        return [("Avance mensual", self.chart)]

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return [("Serie mensual", self.model)]

    def show_unavailable(self, message: str) -> None:
        super().show_unavailable(message)
        self.info.set_title("Indicador")
        self.info.chip.set_status(Status.PENDING, "")
        self.info.set_rows([], "")

    def _render(self, context: ViewContext) -> None:
        if context.indicator_code is None:
            self.show_unavailable("Elija un indicador en el panel lateral.")
            return
        result = self.services.monitor.result(context.period, context.indicator_code, context.site_code)
        ind = result.indicator
        self.info.set_title(f"{ind.code} {ind.name}")
        self.info.chip.set_status(
            result.status,
            f"{result.status.label}: {compliance_text(result)}"
            if result.compliance is not None
            else result.status.label,
        )
        self.info.set_rows(result_details(result), result.note or result.goal.reason)
        self.model.set_rows(monthly_rows(result))
        if result.status is Status.NOT_APPLICABLE:
            self.chart.show_message(f"{ind.short_name} no aplica a {result.site_name}: la sede no tiene meta asignada.")
            return
        if all(p.cumulative_value is None and p.monthly_value is None for p in result.monthly):
            self.chart.show_message(f"{result.site_name} no tiene datos de {ind.short_name} en {context.period.year}.")
            return
        draw_progress(self.chart.clear(), result)
        self.chart.redraw()


class ComparisonTab(AnalysisTab):
    """Comparación entre sedes del indicador elegido y mapa de calor indicador × sede."""

    title = "Comparación entre sedes"

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.chart = ChartCanvas(self, min_height=240)
        self.heatmap = ChartCanvas(self, min_height=360)
        self.model = RecordTableModel(
            [
                cell_column("level", "Nivel"),
                cell_column("value", "Valor", numeric=True),
                cell_column("goal_to_date", "Meta a\nla fecha", numeric=True),
                cell_column("compliance", "Cumplimiento", numeric=True),
                cell_column("status", "Estado", center=True),
                cell_column("projection", "Proyección\nal cierre", numeric=True),
                cell_column("probability", "Probabilidad\nde cumplir", numeric=True),
                cell_column("months", "Meses\ninformados", numeric=True),
            ]
        )
        self.view = make_table_view(self.model, self)
        self.view.doubleClicked.connect(self._on_table_double_click)
        self._layout: HeatmapLayout | None = None
        self._levels: tuple[str | None, ...] = ()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.chart)
        splitter.addWidget(self.heatmap)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 630])
        # Como en el cumplimiento: los gráficos lado a lado o la tabla de niveles a todo el ancho.
        self.switch = ViewSwitch([("Gráficos", splitter), ("Tabla", padded(self.view))], self)
        self.caption = self.switch.caption
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.switch)
        self.heatmap.canvas.mpl_connect("button_press_event", self._on_heatmap_click)
        self.heatmap.canvas.mpl_connect("motion_notify_event", self._on_heatmap_hover)
        self.chart.canvas.mpl_connect("button_press_event", self._on_chart_click)
        self.chart.canvas.mpl_connect("motion_notify_event", self._on_chart_hover)
        self._matrix: StatusMatrix | None = None
        self._site_results: list[IndicatorResult] = []

    def charts(self) -> list[tuple[str, ChartCanvas]]:
        return [("Cumplimiento por sede", self.chart), ("Mapa de calor", self.heatmap)]

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return [("Comparación por nivel", self.model)]

    def show_unavailable(self, message: str) -> None:
        super().show_unavailable(message)
        self._layout = None
        self._matrix = None
        self._levels = ()
        self._site_results = []
        self.caption.setText(message)

    def _render(self, context: ViewContext) -> None:
        monitor = self.services.monitor
        matrix = monitor.status_matrix(context.period, context.program_code)
        if matrix.indicators:
            self._layout = draw_heatmap(self.heatmap.clear(), matrix, context.indicator_code, context.site_code)
            self._matrix = matrix
            self.heatmap.redraw()
        else:
            self._layout = None
            self._matrix = None
            self.heatmap.show_message("No hay indicadores vigentes para el programa elegido.")
        if context.indicator_code is None:
            self._levels = ()
            self._site_results = []
            self.chart.show_message("Elija un indicador en el panel lateral.")
            self.model.set_rows([])
            self.caption.setText(f"{context.period.label.capitalize()}. Elija un indicador en el panel lateral.")
            return
        results = monitor.site_comparison(context.period, context.indicator_code)
        self._levels = draw_site_comparison(self.chart.clear(), results, context.thresholds, context.site_code)
        self._site_results = list(results)
        self.chart.redraw()
        self.model.set_rows([result_row(r, order) for order, r in enumerate(results)])
        self.caption.setText(
            f"{context.period.label.capitalize()}. La red suma las sedes con dato y no toma el estado de la peor "
            "sede. Doble clic en una barra, una celda o una fila para ver su avance mensual."
        )

    def _on_heatmap_hover(self, event: MouseEvent) -> None:
        cell = None
        if self._layout is not None and self._matrix is not None and event.inaxes is self.heatmap.figure.axes[0]:
            cell = self._layout.cell_at(event.xdata, event.ydata)
        self.heatmap.show_tooltip(None if cell is None else matrix_tooltip(self._matrix.cell(*cell)))

    def _on_chart_hover(self, event: MouseEvent) -> None:
        row = row_at(event.ydata, len(self._site_results)) if event.inaxes is not None else None
        self.chart.show_tooltip(None if row is None else matrix_tooltip(self._site_results[row]))

    def _on_heatmap_click(self, event: MouseEvent) -> None:
        if self._layout is None or event.inaxes is None or event.inaxes is not self.heatmap.figure.axes[0]:
            return
        cell = self._layout.cell_at(event.xdata, event.ydata)
        if cell is not None:
            self.navigate.emit(cell[0], cell[1], bool(event.dblclick))

    def _on_chart_click(self, event: MouseEvent) -> None:
        if not self._levels or event.inaxes is None or self.context is None:
            return
        row = row_at(event.ydata, len(self._levels))
        if row is not None and self.context.indicator_code is not None:
            self.navigate.emit(self.context.indicator_code, self._levels[row], bool(event.dblclick))

    def _on_table_double_click(self, index: QModelIndex) -> None:
        row = source_row(self.view, index)
        if row is not None:
            record = self.model.row(row)
            self.navigate.emit(record["code"], record["site_code"], True)


class MatrixTab(AnalysisTab):
    """Semáforo indicador × nivel (red y sedes) con el índice ponderado de cada programa."""

    title = "Semáforo"

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.model = RecordTableModel([], row_bold=lambda row: bool(row.get("bold")))
        self.view = make_table_view(self.model, self, sortable=False)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectItems)
        self.view.doubleClicked.connect(self._on_double_click)
        self.caption = table_caption()
        legend = StatusLegend(
            [(STATUS_COLORS[s], STATUS_SHORT[s]) for s in (Status.GREEN, Status.YELLOW, Status.RED)]
            + [
                (STATUS_COLORS[Status.NO_DATA], "Sin datos"),
                (STATUS_COLORS[Status.UNDETERMINED], "Meta no determinable"),
                (STATUS_COLORS[Status.BASELINE], "Línea base (sin semáforo)"),
                (STATUS_COLORS[Status.NOT_APPLICABLE], "No aplica a la sede"),
            ]
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self.caption)
        layout.addWidget(legend)
        layout.addWidget(self.view, 1)
        self._levels: list[str | None] = []

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return [("Matriz de semáforo", self.model)]

    def show_unavailable(self, message: str) -> None:
        super().show_unavailable(message)
        self._levels = []
        self.caption.setText(message)

    def _render(self, context: ViewContext) -> None:
        monitor = self.services.monitor
        matrix = monitor.status_matrix(context.period, context.program_code)
        self._levels = [code for code, _name in matrix.levels]
        columns = [cell_column("indicator", "Indicador"), cell_column("weight", "Peso", numeric=True)]
        columns += [cell_column(level_key(code), name, center=True) for code, name in matrix.levels]
        self.model.set_columns(columns)
        indexes = [i for code in self._levels for i in monitor.indexes(context.period, code)]
        self.model.set_rows(matrix_rows(matrix, indexes, self.services.catalog.programs(), context.thresholds))
        set_stretch_columns(self.view, range(2, len(columns)))
        self.caption.setText(
            f"Estado de cada indicador a {context.period.label}, con el cumplimiento a la fecha. Las últimas filas "
            "muestran el índice ponderado de cada programa. Doble clic en una celda para ver su avance mensual."
        )

    def _on_double_click(self, index: QModelIndex) -> None:
        row = source_row(self.view, index)
        if row is None:
            return
        record = self.model.row(row)
        code = record.get("code")
        column = index.column() - 2
        if code is None:
            return
        site = self._levels[column] if 0 <= column < len(self._levels) else KEEP_SITE
        self.navigate.emit(code, site, True)


class AlertsTab(AnalysisTab):
    """Alertas del período con filtros por gravedad, tipo y novedad."""

    title = "Alertas"

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.severity_combo = QComboBox()
        self.severity_combo.addItem("Todas las gravedades", None)
        for severity in Severity:
            self.severity_combo.addItem(severity.label, severity)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Todos los tipos", None)
        for kind in AlertKind:
            self.kind_combo.addItem(kind.label, kind)
        self.new_check = QCheckBox("Solo nuevas")
        self.indicator_check = QCheckBox("Solo el indicador elegido")
        for widget in (self.severity_combo, self.kind_combo):
            widget.currentIndexChanged.connect(self._apply_filters)
        for check in (self.new_check, self.indicator_check):
            check.toggled.connect(self._apply_filters)
        filters = QHBoxLayout()
        filters.setSpacing(10)
        for widget in (self.severity_combo, self.kind_combo, self.new_check, self.indicator_check):
            filters.addWidget(widget)
        filters.addStretch(1)

        self.model = RecordTableModel(
            [
                cell_column("severity", "Gravedad", center=True),
                cell_column("kind", "Tipo"),
                cell_column("level", "Nivel"),
                cell_column("indicator", "Indicador"),
                cell_column("new", "Nueva", center=True),
                cell_column("message", "Detalle"),
            ]
        )
        self.view = make_table_view(self.model, self)
        set_stretch_column(self.view, 5)
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.selectionModel().selectionChanged.connect(self._show_detail)
        self.caption = table_caption()
        self.detail = QLabel("Seleccione una alerta para ver el detalle completo.", objectName="hint")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(34)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(filters)
        layout.addWidget(self.caption)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.detail)
        self._alerts: list[Alert] = []

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return [("Alertas", self.model)]

    def show_unavailable(self, message: str) -> None:
        super().show_unavailable(message)
        self._alerts = []
        self.caption.setText(message)
        self.detail.setText("Seleccione una alerta para ver el detalle completo.")

    def _render(self, context: ViewContext) -> None:
        self._alerts = self.services.monitor.alerts(
            context.period, context.program_code, context.site_code, all_levels=context.site_code is None
        )
        self._apply_filters()

    def _apply_filters(self) -> None:
        if self.context is None:
            return
        indicator = self.context.indicator_code if self.indicator_check.isChecked() else None
        chosen = filter_alerts(
            self._alerts,
            self.severity_combo.currentData(),
            self.kind_combo.currentData(),
            self.new_check.isChecked(),
            indicator,
        )
        self.model.set_rows(alert_rows(chosen))
        level = "la red y todas las sedes" if self.context.site_code is None else self.context.site_name
        self.caption.setText(f"{alerts_summary(chosen)} Alcance: {level}, {self.context.period.label}.")
        self.detail.setText("Seleccione una alerta para ver el detalle completo.")

    def _show_detail(self) -> None:
        row = source_row(self.view)
        if row is None:
            return
        alert = self.model.row(row)["alert"]
        where = alert.site_name if alert.indicator_code is None else f"{alert.indicator_name}, {alert.site_name}"
        self.detail.setText(f"{alert.kind.label} ({alert.severity.label.lower()}) en {where}: {alert.message}")

    def _on_double_click(self, index: QModelIndex) -> None:
        row = source_row(self.view, index)
        if row is None:
            return
        alert = self.model.row(row)["alert"]
        if alert.indicator_code is not None:
            self.navigate.emit(alert.indicator_code, alert.site_code, True)

    def problems(self) -> list[str]:
        # Un período sin alertas es válido; solo se exige que la lista se haya calculado.
        return [] if self.context is not None else ["La pestaña de alertas no se actualizó."]


class SheetTab(AnalysisTab):
    """Ficha técnica del indicador: definición, reglas del año y metas y pesos de cada año."""

    title = "Ficha del indicador"

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.browser = QTextBrowser(self)
        self.browser.setOpenLinks(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(self.browser)

    def _render(self, context: ViewContext) -> None:
        if context.indicator_code is None:
            self.browser.setPlainText("Elija un indicador en el panel lateral.")
            return
        catalog = self.services.catalog
        views = [catalog.sheet(context.indicator_code, year) for year in catalog.years()]
        self.browser.setHtml(sheet_html(views, context.period.year))

    def show_unavailable(self, message: str) -> None:
        self.browser.setPlainText(message)

    def problems(self) -> list[str]:
        if not self.browser.toPlainText().strip():
            return ["La ficha del indicador está vacía."]
        return []


class DataTab(AnalysisTab):
    """Observaciones del año con filtros, edición de un dato y exportaciones."""

    title = "Datos"
    edit_requested = Signal(object)
    export_requested = Signal()
    template_requested = Signal()

    ALL_MONTHS = 0
    UP_TO_CUT = -1

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(services, parent)
        self.month_combo = QComboBox()
        self.month_combo.addItem("Hasta el mes de corte", self.UP_TO_CUT)
        self.month_combo.addItem("Todo el año", self.ALL_MONTHS)
        for number, name in enumerate(MONTHS, start=1):
            self.month_combo.addItem(f"Solo {name}", number)
        self.indicator_check = QCheckBox("Solo el indicador elegido")
        self.missing_check = QCheckBox("Solo meses no informados")
        self.month_combo.currentIndexChanged.connect(self._apply_filters)
        self.indicator_check.toggled.connect(self._apply_filters)
        self.missing_check.toggled.connect(self._apply_filters)
        self.edit_button = QPushButton("Registrar o corregir dato...")
        self.export_button = QPushButton("Exportar observaciones...")
        self.template_button = QPushButton("Descargar plantilla...")
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.selected_record()))
        self.export_button.clicked.connect(self.export_requested.emit)
        self.template_button.clicked.connect(self.template_requested.emit)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        for widget in (self.month_combo, self.indicator_check, self.missing_check):
            filters.addWidget(widget)
        filters.addStretch(1)
        self.caption = table_caption()
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.caption, 1)
        for button in (self.edit_button, self.export_button, self.template_button):
            actions.addWidget(button)

        self.model = RecordTableModel(
            [
                cell_column("site", "Sede"),
                cell_column("indicator", "Indicador"),
                cell_column("period", "Mes"),
                cell_column("numerator", "Numerador", numeric=True),
                cell_column("denominator", "Denominador", numeric=True),
                cell_column("reported", "Informado", center=True),
                cell_column("origin", "Origen"),
            ]
        )
        self.view = make_table_view(self.model, self)
        self.view.doubleClicked.connect(lambda _index: self.edit_requested.emit(self.selected_record()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(filters)
        layout.addLayout(actions)
        layout.addWidget(self.view, 1)
        self._rows: list[ObservationRow] = []

    def tables(self) -> list[tuple[str, RecordTableModel]]:
        return [("Observaciones", self.model)]

    def show_unavailable(self, message: str) -> None:
        super().show_unavailable(message)
        self._rows = []
        self.caption.setText(message)

    def selected_record(self) -> ObservationRow | None:
        row = source_row(self.view)
        return None if row is None else self.model.row(row)["record"]

    def set_actions_enabled(self, enabled: bool) -> None:
        for button in (self.edit_button, self.export_button, self.template_button):
            button.setEnabled(enabled)

    def _render(self, context: ViewContext) -> None:
        catalog = self.services.catalog
        rows = self.services.data.observations(context.period.year, None, context.site_code, None, context.program_code)
        quality = self.services.monitor.data_quality(context.period, context.site_code, context.program_code)
        program_by_indicator = {ind.code: ind.program_code for ind in catalog.indicators()}
        rows = rows + missing_rows(rows, quality, context.period.year, program_by_indicator)
        self._rows = order_observations(
            rows,
            {ind.code: position for position, ind in enumerate(catalog.indicators())},
            {site.code: position for position, site in enumerate(catalog.sites())},
        )
        self._apply_filters()

    def current_filter(self) -> ObservationFilter:
        assert self.context is not None
        choice = self.month_combo.currentData()
        return ObservationFilter(
            month=choice if choice > 0 else None,
            indicator_code=self.context.indicator_code if self.indicator_check.isChecked() else None,
            only_missing=self.missing_check.isChecked(),
            up_to_month=self.context.period.month if choice == self.UP_TO_CUT else None,
        )

    def _apply_filters(self) -> None:
        if self.context is None:
            return
        rows = filter_observations(self._rows, self.current_filter())
        self.model.set_rows(observation_rows(rows))
        missing = sum(1 for r in rows if not r.reported)
        where = "todas las sedes" if self.context.site_code is None else self.context.site_name
        self.caption.setText(
            f"{plural(len(rows), 'observación', 'observaciones')} de {where} en "
            f"{self.context.period.year}; {format_int(missing)} marcadas como no informadas. Un mes no informado "
            "no se cuenta como cero. Doble clic en una fila para corregirla."
        )


TAB_TYPES: Sequence[type[AnalysisTab]] = (
    ComplianceTab,
    ProgressTab,
    ComparisonTab,
    MatrixTab,
    AlertsTab,
    SheetTab,
    DataTab,
)
