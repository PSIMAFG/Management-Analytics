"""Pestaña de rendimiento de horas: horas ociosas del personal y de las salas."""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from optibox.domain.metrics import RoomUse, StaffHours
from optibox.domain.run import RunDetail
from optibox.ui.charts import draw_room_hours, draw_staff_hours
from optibox.ui.formatting import format_pct
from optibox.ui.presentation import hours_text, staff_label
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import ChartCanvas, Column, RecordTableModel, hint_label, make_table_view, section_title


def staff_hours_row(row: StaffHours) -> dict[str, Any]:
    """Fila de la tabla de rendimiento de horas por persona."""
    return {
        "label": staff_label(row.code, row.name),
        "role": row.role,
        "scheduled_min": row.scheduled_min,
        "contracted_min": row.contracted_min,
        "leave_min": row.leave_min,
        "session_min": row.session_min,
        "admin_min": row.admin_min,
        "meeting_min": row.meeting_min,
        "idle_min": row.idle_min,
        "productive_pct": row.productive_pct,
        "clinical_pct": row.clinical_pct,
        "idle_pct": row.idle_pct,
    }


def room_hours_row(room: RoomUse) -> dict[str, Any]:
    """Fila de la tabla de rendimiento de horas por sala."""
    return {
        "name": room.name,
        "capacity_min": room.capacity_min,
        "session_min": room.session_min,
        "idle_min": room.idle_min,
        "pct": room.pct,
    }


STAFF_HOURS_HINT = (
    "Tiempo contratado disponible = mínimo entre la jornada programada y el contrato (el exceso de disponibilidad "
    "no es ocioso). Permisos son ausencias aprobadas: no son ociosos ni productivos, se descuentan aparte. "
    "Ociosas = tiempo contratado disponible menos permisos menos productivas (atención, administrativo y "
    "reuniones), nunca negativo. % productivo clínico considera solo atenciones."
)
ROOM_HOURS_HINT = (
    "Horas ociosas de una sala = horas abiertas (horario del centro sin almuerzo ni feriados) menos horas "
    "ocupadas por atenciones."
)


class HoursTab(TabPage):
    title = "Rendimiento de horas"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.staff_chart = ChartCanvas(self, min_height=260)
        self.room_chart = ChartCanvas(self, min_height=220)
        self.staff_model = RecordTableModel(
            [
                Column("label", "Persona"),
                Column("role", "Cargo"),
                Column("scheduled_min", "Jornada", hours_text, numeric=True, tooltip="Jornada programada."),
                Column(
                    "contracted_min",
                    "Contratado disp.",
                    hours_text,
                    numeric=True,
                    tooltip="Mínimo entre la jornada programada y el contrato.",
                ),
                Column("leave_min", "Permisos", hours_text, numeric=True, tooltip="Ausencias aprobadas."),
                Column("session_min", "Atención", hours_text, numeric=True),
                Column("admin_min", "Adm.", hours_text, numeric=True),
                Column("meeting_min", "Reuniones", hours_text, numeric=True),
                Column("idle_min", "Ociosas", hours_text, numeric=True),
                Column("productive_pct", "% Productivo", format_pct, numeric=True),
                Column("clinical_pct", "% Prod. clínico", format_pct, numeric=True, tooltip="Solo atenciones."),
                Column("idle_pct", "% Ocioso", format_pct, numeric=True),
            ]
        )
        self.room_model = RecordTableModel(
            [
                Column("name", "Sala"),
                Column("capacity_min", "Horas abiertas", hours_text, numeric=True),
                Column("session_min", "Horas ocupadas", hours_text, numeric=True),
                Column("idle_min", "Horas ociosas", hours_text, numeric=True),
                Column("pct", "Ocupación", format_pct, numeric=True),
            ]
        )
        self.staff_table = make_table_view(self.staff_model, self, stretch_column=0)
        self.room_table = make_table_view(self.room_model, self, stretch_column=0)

        charts = splitter(Qt.Orientation.Horizontal, [self.staff_chart, self.room_chart], [3, 2])

        staff_box, staff_layout = vbox(self)
        staff_layout.addWidget(section_title("Horas por persona"))
        staff_layout.addWidget(hint_label(STAFF_HOURS_HINT))
        staff_layout.addWidget(self.staff_table, 1)

        room_box, room_layout = vbox(self)
        room_layout.addWidget(section_title("Horas por sala"))
        room_layout.addWidget(hint_label(ROOM_HOURS_HINT))
        room_layout.addWidget(self.room_table, 1)

        tables = splitter(Qt.Orientation.Horizontal, [staff_box, room_box], [3, 2])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter(Qt.Orientation.Vertical, [charts, tables], [3, 2]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.staff_chart, self.room_chart)

    def set_detail(self, detail: RunDetail | None) -> None:
        if detail is None:
            self.staff_model.set_rows([])
            self.room_model.set_rows([])
            self.staff_chart.show_message("Aún no hay corridas. Elija una semana y presione Optimizar.")
            self.room_chart.show_message("Aún no hay corridas. Elija una semana y presione Optimizar.")
            return
        staff_hours = detail.metrics.staff_hours
        rooms = detail.metrics.rooms
        self.staff_model.set_rows([staff_hours_row(row) for row in staff_hours])
        self.room_model.set_rows([room_hours_row(room) for room in rooms])
        if staff_hours:
            self.staff_chart.render(partial(draw_staff_hours, rows=staff_hours))
        else:
            self.staff_chart.show_message("No hay personal con contrato vigente en la semana.")
        if rooms:
            self.room_chart.render(partial(draw_room_hours, rooms=rooms))
        else:
            self.room_chart.show_message("Sin salas de atención activas.")

    def self_check(self) -> list[str]:
        problems = check_charts([("horas por persona", self.staff_chart), ("horas por sala", self.room_chart)])
        if self.staff_model.rowCount() == 0:
            problems.append("la tabla de horas por persona está vacía")
        if self.room_model.rowCount() == 0:
            problems.append("la tabla de horas por sala está vacía")
        return problems
