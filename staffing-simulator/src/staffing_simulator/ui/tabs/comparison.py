"""Pestaña Comparación de escenarios: costo total, evolución mensual o acumulada y tabla de diferencias."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget

from staffing_simulator.domain.comparison import ScenarioComparison
from staffing_simulator.domain.models import MONTH_LABELS, Scenario
from staffing_simulator.ui.charts import DifferenceGroup, ScenarioBar, Series, draw_comparison
from staffing_simulator.ui.formatting import format_clp, format_signed_clp, format_signed_pct
from staffing_simulator.ui.presenters import COMPARISON_SECTIONS, ColumnSpec, comparison_table
from staffing_simulator.ui.style import NEUTRAL
from staffing_simulator.ui.widgets import (
    ChartCanvas,
    Column,
    RecordTableModel,
    make_table_view,
    muted_label,
    set_stretch_column,
)

FORMATTERS: dict[str, Callable[[Any], str]] = {
    "money": format_clp,
    "signed_money": format_signed_clp,
    "signed_pct": format_signed_pct,
}


def columns_from_specs(specs: Sequence[ColumnSpec]) -> list[Column]:
    return [
        Column(
            spec.key,
            spec.header,
            FORMATTERS.get(spec.kind, str),
            numeric=spec.kind != "text",
            header_tooltip=spec.tooltip,
        )
        for spec in specs
    ]


class ComparisonTab(QWidget):
    base_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._comparison: ScenarioComparison | None = None
        self._colors: Mapping[int, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 6)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("Escenario base"))
        self.base_combo = QComboBox()
        self.base_combo.setMinimumWidth(200)
        self.base_combo.currentIndexChanged.connect(self._on_base_changed)
        toolbar.addWidget(self.base_combo)
        toolbar.addSpacing(12)
        toolbar.addWidget(QLabel("Líneas"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Costo mensual", False)
        self.mode_combo.addItem("Costo acumulado", True)
        self.mode_combo.currentIndexChanged.connect(self._draw)
        toolbar.addWidget(self.mode_combo)
        toolbar.addSpacing(12)
        toolbar.addWidget(QLabel("Tabla"))
        self.section_combo = QComboBox()
        for key, label in COMPARISON_SECTIONS:
            self.section_combo.addItem(label, key)
        self.section_combo.currentIndexChanged.connect(self._fill_table)
        toolbar.addWidget(self.section_combo)
        toolbar.addStretch(1)
        self.status_label = muted_label("", wrap=False)
        toolbar.addWidget(self.status_label)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.chart = ChartCanvas(splitter)
        self.model = RecordTableModel([Column("label", "Concepto")], self)
        self.view = make_table_view(self.model, self, sortable=False, stretch_column=0)
        splitter.addWidget(self.chart)
        splitter.addWidget(self.view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 240])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        self._loading = False

    def set_candidates(self, scenarios: Sequence[Scenario], base_id: int | None) -> None:
        """Escenarios marcados para comparar (opciones de base) sin emitir señales."""
        self._loading = True
        try:
            self.base_combo.clear()
            for scenario in scenarios:
                self.base_combo.addItem(scenario.name, scenario.id)
            for index in range(self.base_combo.count()):
                if self.base_combo.itemData(index) == base_id:
                    self.base_combo.setCurrentIndex(index)
        finally:
            self._loading = False
        self.base_combo.setEnabled(self.base_combo.count() > 1)

    def base_id(self) -> int | None:
        value = self.base_combo.currentData()
        return None if value is None else int(value)

    def set_busy(self, busy: bool) -> None:
        self.status_label.setText("Calculando la comparación..." if busy else "")

    def show_comparison(self, comparison: ScenarioComparison, colors: Mapping[int, str]) -> None:
        self._comparison = comparison
        self._colors = colors
        self.set_busy(False)
        self._fill_table()
        self._draw()

    def show_message(self, message: str) -> None:
        self._comparison = None
        self.set_busy(False)
        self.model.set_columns([Column("label", "Concepto")], [])
        self.chart.show_message(message)

    def _draw(self) -> None:
        comparison = self._comparison
        if comparison is None:
            return
        cumulative = bool(self.mode_combo.currentData())
        bars = [
            ScenarioBar(
                name=item.name,
                color=self._colors.get(item.scenario_id, NEUTRAL),
                total=item.total_cost,
                difference=comparison.total.differences[index],
                share=None
                if comparison.total.percentages[index] is None
                else float(comparison.total.percentages[index]),
                is_base=index == comparison.base_index,
            )
            for index, item in enumerate(comparison.scenarios)
        ]
        lines = [
            Series(
                item.name,
                self._colors.get(item.scenario_id, NEUTRAL),
                item.cumulative if cumulative else item.monthly,
            )
            for item in comparison.scenarios
        ]
        others = [index for index in range(len(comparison.scenarios)) if index != comparison.base_index]
        differences = [
            DifferenceGroup(row.label, [row.differences[index] for index in others])
            for row in comparison.by_contract_type
        ]
        difference_series = [
            Series(
                comparison.scenarios[index].name,
                self._colors.get(comparison.scenarios[index].scenario_id, NEUTRAL),
                [],
            )
            for index in others
        ]
        self.chart.plot(
            lambda figure, hover: draw_comparison(
                figure,
                bars=bars,
                months=MONTH_LABELS,
                lines=lines,
                cumulative=cumulative,
                differences=differences,
                difference_series=difference_series,
                hover=hover,
            )
        )

    def _fill_table(self) -> None:
        if self._comparison is None:
            return
        specs, rows = comparison_table(self._comparison, str(self.section_combo.currentData()))
        self.model.set_columns(columns_from_specs(specs), rows)
        set_stretch_column(self.view, 0)

    def _on_base_changed(self) -> None:
        if not self._loading:
            self.base_changed.emit()
