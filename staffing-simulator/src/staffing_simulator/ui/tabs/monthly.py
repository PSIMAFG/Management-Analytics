"""Pestaña Costo mensual: barras apiladas por tipo de contrato, acumulado del año y tabla por mes."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from staffing_simulator.domain.models import MONTH_LABELS
from staffing_simulator.domain.projection import Dimension, Projection
from staffing_simulator.ui.charts import Series, draw_monthly
from staffing_simulator.ui.formatting import format_clp, format_pct
from staffing_simulator.ui.presenters import empty_projection_message, monthly_rows
from staffing_simulator.ui.style import NEUTRAL
from staffing_simulator.ui.widgets import ChartCanvas, Column, RecordTableModel, make_table_view, muted_label

MONTHLY_COLUMNS = (
    Column("month", "Mes"),
    Column("cost", "Costo del mes", format_clp, numeric=True),
    Column("cumulative", "Acumulado", format_clp, numeric=True),
    Column("budget_share", "Avance", format_pct, numeric=True),
)


class MonthlyTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.chart = ChartCanvas(splitter)
        table_panel = QWidget(splitter)
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(6, 6, 6, 6)
        table_layout.addWidget(QLabel("Detalle por mes", objectName="sectionTitle"))
        self.model = RecordTableModel(MONTHLY_COLUMNS, self)
        self.view = make_table_view(self.model, self, sortable=False, stretch_column=0)
        table_layout.addWidget(self.view, 1)
        self.note_label = muted_label(
            "Avance: costo acumulado como porcentaje del presupuesto anual de todos los programas."
        )
        table_layout.addWidget(self.note_label)
        splitter.addWidget(self.chart)
        splitter.addWidget(table_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([650, 380])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)

    def show_projection(self, projection: Projection, budget_total: int, colors: Mapping[int, str]) -> None:
        rows = projection.breakdown(Dimension.CONTRACT_TYPE)
        if not projection.lines or projection.total_cost == 0:
            self.show_message(empty_projection_message(projection))
            self.model.set_rows(monthly_rows(projection, budget_total))
            return
        ordered = sorted(rows, key=lambda row: row.key)
        series = [Series(row.label, colors.get(row.key, NEUTRAL), row.monthly) for row in ordered]
        cumulative = projection.cumulative()
        self.chart.plot(
            lambda figure, hover: draw_monthly(
                figure,
                months=MONTH_LABELS,
                series=series,
                cumulative=cumulative,
                budget_total=budget_total or None,
                hover=hover,
            )
        )
        self.model.set_rows(monthly_rows(projection, budget_total))

    def show_message(self, message: str) -> None:
        self.chart.show_message(message)

    def clear(self, message: str) -> None:
        self.chart.show_message(message)
        self.model.set_rows([])
