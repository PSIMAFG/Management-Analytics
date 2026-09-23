"""Pestaña Desglose: costo anual por tipo de contrato, programa, cargo y sede."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from staffing_simulator.domain.projection import Dimension, Projection
from staffing_simulator.ui.charts import BarItem, BarPanel, draw_breakdown
from staffing_simulator.ui.formatting import format_clp, format_int, format_pct
from staffing_simulator.ui.presenters import breakdown_table, empty_projection_message
from staffing_simulator.ui.style import INK, NEUTRAL
from staffing_simulator.ui.widgets import ChartCanvas, Column, RecordTableModel, make_table_view

PANELS: tuple[tuple[Dimension, str], ...] = (
    (Dimension.CONTRACT_TYPE, "Por tipo de contrato"),
    (Dimension.PROGRAM, "Por programa"),
    (Dimension.JOB_ROLE, "Por cargo"),
    (Dimension.SITE, "Por sede"),
)

BREAKDOWN_COLUMNS = (
    Column("label", "Elemento"),
    Column("positions", "Posiciones", format_int, numeric=True),
    Column("total", "Costo anual", format_clp, numeric=True),
    Column("share", "Del total", format_pct, numeric=True),
)


class BreakdownTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.chart = ChartCanvas(splitter)
        table_panel = QWidget(splitter)
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(6, 6, 6, 6)
        table_layout.addWidget(QLabel("Detalle por dimensión", objectName="sectionTitle"))
        self.model = RecordTableModel(BREAKDOWN_COLUMNS, self)
        self.view = make_table_view(self.model, self, sortable=False, stretch_column=0)
        table_layout.addWidget(self.view, 1)
        splitter.addWidget(self.chart)
        splitter.addWidget(table_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([560, 470])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)

    def show_projection(self, projection: Projection, contract_colors: Mapping[int, str]) -> None:
        self.model.set_rows(breakdown_table(projection))
        if not projection.lines or projection.total_cost == 0:
            self.chart.show_message(empty_projection_message(projection))
            return
        panels = []
        for dimension, title in PANELS:
            items = [
                BarItem(
                    row.label,
                    row.total,
                    None if row.share is None else float(row.share),
                    contract_colors.get(row.key, NEUTRAL) if dimension is Dimension.CONTRACT_TYPE else INK,
                )
                for row in projection.breakdown(dimension)
            ]
            panels.append(BarPanel(title, items))
        self.chart.plot(lambda figure, hover: draw_breakdown(figure, panels, hover))

    def clear(self, message: str) -> None:
        self.model.set_rows([])
        self.chart.show_message(message)
