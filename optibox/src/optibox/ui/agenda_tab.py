"""Pestaña de agenda semanal por persona o por sala."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QComboBox, QHBoxLayout, QLabel, QRadioButton, QVBoxLayout, QWidget

from optibox.domain.agenda import AgendaEntry
from optibox.domain.run import RunDetail
from optibox.services.planning_service import PlanningService
from optibox.ui.charts import KIND_LABELS, draw_agenda, occupancy_colors
from optibox.ui.formatting import format_count, format_hours, format_pct
from optibox.ui.presentation import admin_room_people, band_of, day_label, occupancy_bands
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import (
    ChartCanvas,
    Column,
    RecordTableModel,
    hint_label,
    make_table_view,
    section_title,
    wrap_rows,
)

STAFF_MODE = "staff"
ROOM_MODE = "room"


def agenda_rows(entries: tuple[AgendaEntry, ...]) -> list[dict[str, Any]]:
    """Filas de la tabla de la agenda, ordenadas por día y hora."""
    return [
        {
            "order": (entry.day, entry.start, entry.lane),
            "day": day_label(entry.day),
            "time": entry.time_label,
            "kind": KIND_LABELS[entry.kind],
            "label": entry.label,
        }
        for entry in sorted(entries, key=lambda e: (e.day, e.start, e.lane))
    ]


def default_target(detail: RunDetail, mode: str) -> str | None:
    """Persona o sala que se muestra primero: la que tiene más sesiones en la corrida."""
    counts: dict[str, int] = {}
    for session in detail.plan.sessions:
        key = session.staff if mode == STAFF_MODE else session.room
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return min(counts, key=lambda code: (-counts[code], code))


class AgendaTab(TabPage):
    title = "Agenda semanal"

    def __init__(self, planning: PlanningService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.planning = planning
        self.detail: RunDetail | None = None
        self.entries: tuple[AgendaEntry, ...] = ()

        self.staff_radio = QRadioButton("Persona")
        self.room_radio = QRadioButton("Sala")
        self.staff_radio.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.staff_radio)
        self.mode_group.addButton(self.room_radio)
        self.staff_radio.toggled.connect(self._on_mode_changed)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumWidth(300)
        self.target_combo.currentIndexChanged.connect(self._draw)
        self.summary = hint_label()
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Ver agenda de"))
        controls.addWidget(self.staff_radio)
        controls.addWidget(self.room_radio)
        controls.addWidget(self.target_combo)
        controls.addSpacing(12)
        controls.addWidget(self.summary, 1)

        self.chart = ChartCanvas(self, min_height=380)
        self.model = RecordTableModel(
            [
                Column("day", "Día"),
                Column("time", "Horario"),
                Column("label", "Detalle"),
            ]
        )
        self.table = make_table_view(self.model, self, sortable=False)
        wrap_rows(self.table)
        right, right_layout = vbox(self)
        right_layout.addWidget(section_title("Entradas de la agenda"))
        right_layout.addWidget(self.table, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(controls)
        layout.addWidget(splitter(Qt.Orientation.Horizontal, [self.chart, right], [7, 3]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.chart,)

    @property
    def mode(self) -> str:
        return STAFF_MODE if self.staff_radio.isChecked() else ROOM_MODE

    def set_mode(self, mode: str) -> None:
        (self.staff_radio if mode == STAFF_MODE else self.room_radio).setChecked(True)

    def set_detail(self, detail: RunDetail | None) -> None:
        self.detail = detail
        self._fill_targets()

    def _on_mode_changed(self) -> None:
        self._fill_targets()

    def _fill_targets(self) -> None:
        current = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        detail = self.detail
        if detail is not None:
            if self.mode == STAFF_MODE:
                for week in detail.instance.staff:
                    self.target_combo.addItem(f"{week.code} {week.staff.name} ({week.role.name})", week.code)
            else:
                for room in detail.instance.rooms:
                    if room.active:
                        self.target_combo.addItem(f"{room.code} {room.name}", room.code)
        index = self.target_combo.findData(current)
        if index < 0 and detail is not None:
            index = self.target_combo.findData(default_target(detail, self.mode))
        self.target_combo.setCurrentIndex(max(0, index) if self.target_combo.count() else -1)
        self.target_combo.blockSignals(False)
        self._draw()

    def _draw(self) -> None:
        detail = self.detail
        code = self.target_combo.currentData()
        if detail is None or code is None:
            self.entries = ()
            self.model.set_rows([])
            self.summary.setText("")
            self.chart.show_message("Seleccione una corrida para ver la agenda.")
            return
        code = str(code)
        if self.mode == STAFF_MODE:
            self.entries = self.planning.staff_agenda(detail, code)
            title = f"Agenda de {self.target_combo.currentText()}"
        else:
            self.entries = self.planning.room_agenda(detail, code)
            title = f"Agenda de la sala {self.target_combo.currentText()}"
        self.summary.setText(self._summary_text(detail, code))
        self.model.set_rows(agenda_rows(self.entries))
        names = {service.code: service.name for service in detail.instance.service_types}
        fill_of, legend = self._occupancy_colors(detail, code)
        self.chart.render(
            partial(
                draw_agenda,
                entries=self.entries,
                days=detail.instance.days,
                title=title,
                names=names,
                show_staff=self.mode == ROOM_MODE,
                fill_of=fill_of,
                legend=legend,
            )
        )

    def _occupancy_colors(
        self, detail: RunDetail, code: str
    ) -> tuple[Callable[[AgendaEntry], str | None] | None, list[tuple[str, str]] | None]:
        """En la sala administrativa cada tramo se colorea según cuántas personas la ocupan."""
        room = detail.instance.room_by_code.get(code)
        if self.mode != ROOM_MODE or room is None or not room.is_admin_room:
            return None, None
        bands = occupancy_bands(room.soft_capacity, room.simultaneous_hard or room.soft_capacity)
        colors = dict(zip(bands, occupancy_colors(bands), strict=True))
        people = admin_room_people(detail)

        def fill_of(entry: AgendaEntry) -> str | None:
            band = band_of(bands, people.get((entry.day, entry.start), 0))
            return colors[band] if band is not None else None

        # Se muestra la escala completa, aunque algún rango no ocurra, para que se vea dónde empieza el exceso.
        return fill_of, [(colors[band], band.label) for band in bands]

    def _summary_text(self, detail: RunDetail, code: str) -> str:
        if self.mode == STAFF_MODE:
            load = next((row for row in detail.metrics.staff_load if row.code == code), None)
            if load is None:
                return "Sin contrato vigente en la semana."
            if not load.delivers_services:
                return (
                    f"Contrato {format_hours(load.contract_min)}: cargo no asistencial, sin sesiones; "
                    f"{format_hours(load.non_service_min)} de tareas no asistenciales."
                )
            return (
                f"Contrato {format_hours(load.contract_min)}: {format_count(load.sessions, 'sesión', 'sesiones')} "
                f"({format_hours(load.session_min)}), {format_hours(load.admin_min)} adm.; "
                f"uso {format_pct(load.usage_pct)}."
            )
        room = detail.instance.room_by_code.get(code)
        if room is not None and room.is_admin_room:
            peak = max((slot.people for slot in detail.metrics.admin_room), default=0)
            return (
                f"Capacidad {room.simultaneous_hard} personas, recomendada {room.soft_capacity}; "
                f"máximo en la semana: {peak}."
            )
        use = next((row for row in detail.metrics.rooms if row.code == code), None)
        if use is None:
            return ""
        return (
            f"{format_count(use.sessions, 'sesión', 'sesiones')}, "
            f"{format_hours(use.session_min)} de {format_hours(use.capacity_min)} "
            f"disponibles (utilización {format_pct(use.pct)})."
        )

    def self_check(self) -> list[str]:
        problems = check_charts([("agenda", self.chart)])
        if not self.entries:
            problems.append(f"la agenda de {self.target_combo.currentText() or 'la selección'} está vacía")
        return problems
