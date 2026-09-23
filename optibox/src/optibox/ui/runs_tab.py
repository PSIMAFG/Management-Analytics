"""Pestaña de corridas: comparación de escenarios, estado, cobertura, horas y tiempos."""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import Any

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from optibox.domain.run import RunDetail, RunSummary
from optibox.ui.charts import draw_runs
from optibox.ui.formatting import format_date, format_datetime, format_decimal, format_int, format_pct
from optibox.ui.presentation import hours_text, status_label
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import (
    ChartCanvas,
    Column,
    RecordTableModel,
    hint_label,
    make_table_view,
    section_title,
    selected_source_row,
)

CHART_RUNS = 8


def _points(value: float | None) -> str:
    return "-" if value is None else f"{format_decimal(value * 100, 1)} pp"


def run_rows(runs: Sequence[RunSummary]) -> list[dict[str, Any]]:
    """Filas de la tabla de corridas con la mejora del optimizador sobre la heurística."""
    rows: list[dict[str, Any]] = []
    for run in runs:
        optimized, greedy = run.coverage_pct, run.greedy_coverage_pct
        rows.append(
            {
                "id": run.id,
                "created_at": run.created_at,
                "week_start": run.week_start,
                "scenario": run.scenario_name,
                "status": status_label(run.status),
                "coverage": optimized,
                "greedy": greedy,
                "gain": optimized - greedy if optimized is not None and greedy is not None else None,
                "hours": run.session_minutes,
                "time": f"{format_decimal(run.total_seconds, 1)} de {format_decimal(run.time_limit_s, 0)}",
            }
        )
    return rows


def mix_rows(detail: RunDetail) -> list[dict[str, Any]]:
    """Mezcla de atención de la corrida frente a la banda mínima y máxima del escenario."""
    return [
        {
            "name": row.name,
            "hours": row.minutes,
            "share": row.share,
            "band": f"{format_pct(row.min_share, 0) if row.min_share is not None else '0 %'} a "
            f"{format_pct(row.max_share, 0) if row.max_share is not None else '100 %'}",
            "deviation": row.deviation_min,
        }
        for row in detail.metrics.mix
    ]


class RunsTab(TabPage):
    title = "Corridas"
    show_requested = Signal(int)
    delete_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runs: tuple[RunSummary, ...] = ()
        self.current_id: int | None = None
        self.model = RecordTableModel(
            [
                Column("id", "Corrida", format_int, numeric=True),
                Column("created_at", "Creada", format_datetime),
                Column("week_start", "Semana", format_date),
                Column("scenario", "Escenario"),
                Column("status", "Estado"),
                Column("coverage", "Cobertura", format_pct, numeric=True),
                Column("greedy", "Heurística", format_pct, numeric=True, tooltip="Cobertura de la heurística voraz."),
                Column("gain", "Mejora", _points, numeric=True, tooltip="Puntos porcentuales sobre la heurística."),
                Column("hours", "Horas", hours_text, numeric=True, tooltip="Horas de atención asignadas."),
                Column("time", "Tiempo (s)", numeric=True, tooltip="Tiempo total de la corrida y límite indicado."),
            ]
        )
        self.table = make_table_view(self.model, self, stretch_column=3)
        self.table.doubleClicked.connect(self._on_double_click)
        self.show_button = QPushButton("Mostrar corrida seleccionada")
        self.show_button.clicked.connect(self._emit_show)
        self.delete_button = QPushButton("Eliminar corrida")
        self.delete_button.clicked.connect(self._emit_delete)
        buttons = QHBoxLayout()
        buttons.addWidget(section_title("Historial de corridas"))
        buttons.addStretch(1)
        buttons.addWidget(self.show_button)
        buttons.addWidget(self.delete_button)
        top, top_layout = vbox(self)
        top_layout.addLayout(buttons)
        top_layout.addWidget(self.table, 1)

        self.chart = ChartCanvas(self, min_height=220)
        self.mix_title = section_title("Mezcla de atención de la corrida mostrada")
        self.mix_model = RecordTableModel(
            [
                Column("name", "Tipo de atención"),
                Column("hours", "Horas", hours_text, numeric=True),
                Column("share", "Participación", format_pct, numeric=True),
                Column("band", "Banda", tooltip="Participación mínima y máxima que pide el escenario."),
                Column(
                    "deviation",
                    "Desvío (min)",
                    format_int,
                    numeric=True,
                    tooltip="Minutos fuera de la banda del escenario (0 = dentro de la banda).",
                ),
            ]
        )
        self.mix_table = make_table_view(self.mix_model, self, stretch_column=0)
        mix, mix_layout = vbox(self)
        mix_layout.addWidget(self.mix_title)
        mix_layout.addWidget(
            hint_label(
                "Participación en los minutos de atención frente a las cotas del escenario. La fase B penaliza el "
                "desvío, así que escenarios distintos producen mezclas distintas cuando hay holgura."
            )
        )
        mix_layout.addWidget(self.mix_table, 1)
        bottom = splitter(Qt.Orientation.Horizontal, [self.chart, mix], [9, 11])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter(Qt.Orientation.Vertical, [top, bottom], [2, 3]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.chart,)

    def set_runs(self, runs: Sequence[RunSummary]) -> None:
        self.runs = tuple(runs)
        self.model.set_rows(run_rows(self.runs))
        self.delete_button.setEnabled(bool(self.runs))
        self.show_button.setEnabled(bool(self.runs))
        self._draw()

    def set_detail(self, detail: RunDetail | None) -> None:
        self.current_id = detail.summary.id if detail is not None else None
        if detail is None:
            self.mix_title.setText("Mezcla de atención de la corrida mostrada")
            self.mix_model.set_rows([])
        else:
            suffix = "" if detail.metrics.mix else ": el escenario no define cotas de mezcla"
            self.mix_title.setText(
                f"Mezcla de atención de la corrida {detail.summary.id} ({detail.summary.scenario_name}){suffix}"
            )
            self.mix_model.set_rows(mix_rows(detail))
        self._draw()
        self._select_current()

    def _draw(self) -> None:
        if not self.runs:
            self.chart.show_message("Aún no hay corridas guardadas.")
            return
        recent = self.runs[:CHART_RUNS]
        self.chart.render(partial(draw_runs, runs=recent, current_id=self.current_id))

    def _select_current(self) -> None:
        if self.current_id is None:
            return
        proxy = self.table.model()
        for row in range(proxy.rowCount()):
            if proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) == self.current_id:
                self.table.selectRow(row)
                return

    def selected_run_id(self) -> int | None:
        row = selected_source_row(self.table)
        return None if row is None else int(self.model.row(row)["id"])

    def _emit_show(self) -> None:
        run_id = self.selected_run_id()
        if run_id is not None:
            self.show_requested.emit(run_id)

    def _emit_delete(self) -> None:
        run_id = self.selected_run_id()
        if run_id is not None:
            self.delete_requested.emit(run_id)

    def _on_double_click(self, index: QModelIndex) -> None:
        if index.isValid():
            self._emit_show()

    def self_check(self) -> list[str]:
        problems = check_charts([("comparación de corridas", self.chart)])
        if self.model.rowCount() == 0:
            problems.append("el historial de corridas está vacío")
        return problems
