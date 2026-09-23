"""Pestaña Presupuesto y ejecución: asignado, planificado y ejecutado por ítem presupuestario y por mes."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget

from staffing_simulator.domain.budget import ItemReport
from staffing_simulator.domain.models import MONTH_LABELS
from staffing_simulator.ui.charts import draw_budget
from staffing_simulator.ui.formatting import format_clp, format_pct, format_signed_clp, format_signed_pct
from staffing_simulator.ui.presenters import (
    coverage_text,
    item_chart_bars,
    item_monthly_rows,
    item_report_rows,
    monthly_series,
    program_report_rows,
    tone_for_balance,
)
from staffing_simulator.ui.style import TONE_COLORS
from staffing_simulator.ui.widgets import (
    ChartCanvas,
    Column,
    RecordTableModel,
    make_table_view,
    muted_label,
    set_stretch_column,
)

ALL_ITEMS = "Todos los ítems"


def _balance_color(value: Any) -> str | None:
    tone = tone_for_balance(value)
    return None if tone is None else TONE_COLORS[tone]


ITEM_COLUMNS = (
    Column("item", "Ítem"),
    Column("assigned", "Asignado", format_clp, numeric=True),
    Column("planned", "Planificado", format_clp, numeric=True),
    Column("balance", "Saldo", format_signed_clp, numeric=True, tone=_balance_color),
    Column("used_share", "% usado", format_pct, numeric=True),
    Column("executed_to_date", "Ejecutado a la fecha", format_clp, numeric=True),
)
PROGRAM_COLUMNS = (
    Column("program", "Programa"),
    Column("assigned", "Asignado", format_clp, numeric=True),
    Column("planned", "Planificado", format_clp, numeric=True),
    Column("balance", "Saldo", format_signed_clp, numeric=True, tone=_balance_color),
    Column("used_share", "% usado", format_pct, numeric=True),
    Column("executed_to_date", "Ejecutado a la fecha", format_clp, numeric=True),
)
MONTH_COLUMNS = (
    Column("month", "Mes"),
    Column("planned", "Planificado", format_clp, numeric=True),
    Column("executed", "Ejecutado", format_clp, numeric=True),
    Column("difference", "Diferencia", format_signed_clp, numeric=True),
    Column("difference_share", "Variación", format_signed_pct, numeric=True),
)


class BudgetTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report: ItemReport | None = None
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 6)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("Ejecución de"))
        self.item_combo = QComboBox()
        self.item_combo.setMinimumWidth(220)
        self.item_combo.currentIndexChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self.item_combo)
        toolbar.addSpacing(12)
        toolbar.addWidget(QLabel("Tabla"))
        self.table_combo = QComboBox()
        self.table_combo.addItem("Por programa", "program")
        self.table_combo.addItem("Por ítem", "item")
        self.table_combo.addItem("Por mes", "month")
        self.table_combo.currentIndexChanged.connect(self._fill_table)
        toolbar.addWidget(self.table_combo)
        toolbar.addSpacing(12)
        self.coverage_label = muted_label("", wrap=False)
        toolbar.addWidget(self.coverage_label, 1)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.chart = ChartCanvas(splitter)
        self.model = RecordTableModel(PROGRAM_COLUMNS, self)
        self.view = make_table_view(self.model, self, sortable=False, stretch_column=0)
        splitter.addWidget(self.chart)
        splitter.addWidget(self.view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([470, 210])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

    def item_id(self) -> int | None:
        value = self.item_combo.currentData()
        return None if value is None else int(value)

    def show_data(self, report: ItemReport) -> None:
        self._report = report
        current = self.item_id()
        self._loading = True
        try:
            self.item_combo.clear()
            self.item_combo.addItem(ALL_ITEMS, None)
            for line in report.lines:
                self.item_combo.addItem(line.item.label, line.item.id)
            for index in range(self.item_combo.count()):
                if self.item_combo.itemData(index) == current:
                    self.item_combo.setCurrentIndex(index)
        finally:
            self._loading = False
        text = coverage_text(report)
        self.coverage_label.setText(text)
        self.coverage_label.setToolTip(text)
        self._fill_table()
        self._draw()

    def clear(self, message: str) -> None:
        self._report = None
        self.model.set_rows([])
        self.coverage_label.setText("")
        self.chart.show_message(message)

    def _on_filter_changed(self) -> None:
        if self._loading:
            return
        self._fill_table()
        self._draw()

    def _draw(self) -> None:
        report = self._report
        if report is None:
            return
        if not report.lines:
            self.chart.show_message(
                f"No hay ítems presupuestarios cargados en {report.year}. Cárguelos en Estructura del programa."
            )
            return
        by_program = self.item_id() is None
        bars = item_chart_bars(report, by_program=by_program)
        planned, executed = monthly_series(report, self.item_id())
        scope = self.item_combo.currentText()
        self.chart.plot(
            lambda figure, hover: draw_budget(
                figure,
                programs=bars,
                months=MONTH_LABELS,
                projected=planned,
                executed=executed,
                scope=scope,
                hover=hover,
            )
        )

    def _fill_table(self) -> None:
        report = self._report
        if report is None:
            self.model.set_rows([])
            return
        choice = self.table_combo.currentData()
        if choice == "month":
            self.model.set_columns(MONTH_COLUMNS, item_monthly_rows(report, self.item_id()))
        elif choice == "item":
            self.model.set_columns(ITEM_COLUMNS, item_report_rows(report))
        else:
            self.model.set_columns(PROGRAM_COLUMNS, program_report_rows(report))
        set_stretch_column(self.view, 0)
