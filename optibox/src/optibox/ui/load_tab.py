"""Pestaña de carga por persona: barras apiladas frente al contrato y tabla con el detalle."""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from optibox.domain.metrics import StaffLoad
from optibox.domain.run import RunDetail
from optibox.ui.charts import draw_staff_load
from optibox.ui.formatting import format_int, format_pct
from optibox.ui.presentation import hours_text, staff_label
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import ChartCanvas, Column, RecordTableModel, hint_label, make_table_view, section_title


def load_row(load: StaffLoad) -> dict[str, Any]:
    """Fila de la tabla de carga con la etiqueta 'código nombre' y los minutos por parte."""
    return {
        "label": staff_label(load.code, load.name),
        "role": load.role,
        "contract_min": load.contract_min,
        "session_min": load.session_min,
        "admin_min": load.admin_min,
        "admin_required_min": load.admin_required_min,
        "meeting_min": load.meeting_min,
        "non_service_min": load.non_service_min,
        "free_min": load.free_min,
        "usage_pct": load.usage_pct,
        "sessions": load.sessions,
    }


LOAD_HINT = (
    "Administrativo incluye los tramos asignados en la sala administrativa y los bloqueos que cuentan como "
    "administrativos. Reuniones y bloqueos son tiempo de trabajo que consume contrato. Libre es el contrato no usado."
)


class LoadTab(TabPage):
    title = "Carga por persona"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chart = ChartCanvas(self, min_height=260)
        self.model = RecordTableModel(
            [
                Column("label", "Persona"),
                Column("contract_min", "Contrato", hours_text, numeric=True),
                Column("session_min", "Atención", hours_text, numeric=True),
                Column("admin_min", "Adm.", hours_text, numeric=True, tooltip="Administrativo colocado."),
                Column(
                    "admin_required_min",
                    "Adm. exigido",
                    hours_text,
                    numeric=True,
                    tooltip="Administrativo que exigen las sesiones asignadas (se coloca el mismo día).",
                ),
                Column("meeting_min", "Reuniones", hours_text, numeric=True),
                Column("non_service_min", "No asist.", hours_text, numeric=True, tooltip="Tareas no asistenciales."),
                Column("free_min", "Libre", hours_text, numeric=True),
                Column("usage_pct", "Uso", format_pct, numeric=True, tooltip="Uso del contrato."),
                Column("sessions", "Sesiones", format_int, numeric=True),
            ]
        )
        self.table = make_table_view(self.model, self, stretch_column=0)
        bottom, bottom_layout = vbox(self)
        bottom_layout.addWidget(section_title("Detalle por persona (horas en la semana)"))
        bottom_layout.addWidget(hint_label(LOAD_HINT))
        bottom_layout.addWidget(self.table, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter(Qt.Orientation.Vertical, [self.chart, bottom], [3, 2]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.chart,)

    def set_detail(self, detail: RunDetail | None) -> None:
        if detail is None:
            self.model.set_rows([])
            self.chart.show_message("Aún no hay corridas. Elija una semana y presione Optimizar.")
            return
        loads = detail.metrics.staff_load
        self.model.set_rows([load_row(load) for load in loads])
        if not loads:
            self.chart.show_message("No hay personal con contrato vigente en la semana.")
            return
        self.chart.render(partial(draw_staff_load, loads=loads))

    def self_check(self) -> list[str]:
        problems = check_charts([("carga por persona", self.chart)])
        if self.model.rowCount() == 0:
            problems.append("la tabla de carga por persona está vacía")
        return problems
