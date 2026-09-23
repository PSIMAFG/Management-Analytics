"""Pestaña de utilización: barras diarias con la acumulada, uso por sala y tabla por sala y día."""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from optibox.domain.run import RunDetail
from optibox.domain.timegrid import WEEKDAY_SHORT
from optibox.ui.charts import draw_daily_utilization, draw_room_utilization
from optibox.ui.formatting import format_int, format_pct
from optibox.ui.presentation import hours_text
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import ChartCanvas, Column, RecordTableModel, hint_label, make_table_view, section_title

UTILIZATION_HINT = (
    "Utilización = minutos de sesión / minutos en que la sala puede usarse (horario del centro sin almuerzo, "
    "feriados, preparación ni cierre). La acumulada del día d suma sesiones y capacidad desde el lunes hasta d."
)


def room_rows(detail: RunDetail) -> list[dict[str, Any]]:
    """Filas de la tabla por sala: totales de la semana y utilización de cada día."""
    by_room_day = {(row.room, row.day): row.pct for row in detail.metrics.room_days}
    rows: list[dict[str, Any]] = []
    for room in detail.metrics.rooms:
        row: dict[str, Any] = {
            "code": room.code,
            "name": room.name,
            "sessions": room.sessions,
            "session_min": room.session_min,
            "capacity_min": room.capacity_min,
            "pct": room.pct,
        }
        for day in detail.instance.days:
            row[f"day{day.index}"] = by_room_day.get((room.code, day.index))
        rows.append(row)
    return rows


class UtilizationTab(TabPage):
    title = "Utilización de salas"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.daily_chart = ChartCanvas(self, min_height=240)
        self.rooms_chart = ChartCanvas(self, min_height=240)
        columns = [
            Column("name", "Sala"),
            Column("sessions", "Sesiones", format_int, numeric=True),
            Column("session_min", "Horas de sesión", hours_text, numeric=True),
            Column("capacity_min", "Horas disponibles", hours_text, numeric=True),
            Column("pct", "Semana", format_pct, numeric=True, tooltip="Utilización de la semana completa."),
        ]
        columns += [Column(f"day{index}", short, format_pct, numeric=True) for index, short in enumerate(WEEKDAY_SHORT)]
        self.model = RecordTableModel(columns)
        self.table = make_table_view(self.model, self, stretch_column=0)
        top = splitter(Qt.Orientation.Horizontal, [self.daily_chart, self.rooms_chart], [3, 2])
        bottom, bottom_layout = vbox(self)
        bottom_layout.addWidget(section_title("Utilización por sala y día"))
        bottom_layout.addWidget(hint_label(UTILIZATION_HINT))
        bottom_layout.addWidget(self.table, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter(Qt.Orientation.Vertical, [top, bottom], [3, 2]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.daily_chart, self.rooms_chart)

    def set_detail(self, detail: RunDetail | None) -> None:
        if detail is None:
            self.model.set_rows([])
            self.daily_chart.show_message("Aún no hay corridas. Elija una semana y presione Optimizar.")
            self.rooms_chart.show_message("Aún no hay corridas. Elija una semana y presione Optimizar.")
            return
        if not detail.metrics.rooms:
            self.model.set_rows([])
            self.daily_chart.show_message("Sin datos de utilización.")
            self.rooms_chart.show_message("Sin salas de atención activas.")
            return
        self.model.set_rows(room_rows(detail))
        self.daily_chart.render(partial(draw_daily_utilization, daily=detail.metrics.daily, days=detail.instance.days))
        self.rooms_chart.render(partial(draw_room_utilization, rooms=detail.metrics.rooms))

    def self_check(self) -> list[str]:
        problems = check_charts([("utilización diaria", self.daily_chart), ("utilización por sala", self.rooms_chart)])
        if self.model.rowCount() == 0:
            problems.append("la tabla de utilización por sala está vacía")
        return problems
