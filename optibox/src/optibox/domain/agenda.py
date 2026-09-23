"""Agenda semanal por persona y por sala, derivada del plan y de la instancia.

La agenda es una vista: se construye siempre desde las sesiones y los tramos
administrativos del plan (la sesión es la entidad de primera clase), más los
bloqueos, ausencias y feriados de la instancia. Las horas se derivan de los
minutos, nunca de índices de slot. Cuando dos entradas se solapan en la misma
vista (por ejemplo, varias personas en la sala administrativa) se reparten en
carriles para que ninguna se pierda.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from optibox.domain.instance import PlanningInstance
from optibox.domain.plan import Plan, admin_slots_per_day
from optibox.domain.timegrid import SLOT_MINUTES, format_range, mask_intervals, slot_mask_outward


class AgendaKind(StrEnum):
    """Tipo de entrada de la agenda."""

    SESSION = "sesion"
    ADMIN = "administrativo"
    BLOCKING = "bloqueo"
    ABSENCE = "ausencia"
    HOLIDAY = "feriado"


_KIND_ORDER = {kind: index for index, kind in enumerate(AgendaKind)}


@dataclass(frozen=True)
class AgendaEntry:
    """Tramo de la agenda: día (0 = lunes), minutos de inicio y fin, tipo y texto a mostrar.

    `color` es el color del tipo de atención (solo sesiones). `lane` es el
    carril dentro del día: 0 salvo que la entrada se solape con otra.
    """

    day: int
    start: int
    end: int
    kind: AgendaKind
    label: str
    color: str | None = None
    staff: str | None = None
    room: str | None = None
    service: str | None = None
    lane: int = 0

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def time_label(self) -> str:
        return format_range(self.start, self.end)


def _assign_lanes(entries: list[AgendaEntry]) -> tuple[AgendaEntry, ...]:
    """Ordena por día e inicio y asigna el menor carril libre a cada entrada."""
    ordered = sorted(entries, key=lambda e: (e.day, e.start, _KIND_ORDER[e.kind], e.end, e.label))
    result: list[AgendaEntry] = []
    lane_ends: dict[int, list[int]] = defaultdict(list)
    for entry in ordered:
        ends = lane_ends[entry.day]
        lane = next((index for index, end in enumerate(ends) if end <= entry.start), len(ends))
        if lane == len(ends):
            ends.append(entry.end)
        else:
            ends[lane] = entry.end
        result.append(
            AgendaEntry(
                entry.day,
                entry.start,
                entry.end,
                entry.kind,
                entry.label,
                entry.color,
                entry.staff,
                entry.room,
                entry.service,
                lane,
            )
        )
    return tuple(result)


def staff_agenda(instance: PlanningInstance, plan: Plan, staff_code: str) -> tuple[AgendaEntry, ...]:
    """Agenda semanal de una persona: feriados, ausencias, bloqueos, sesiones y administrativo."""
    week = instance.staff_by_code.get(staff_code)
    if week is None:
        return ()
    services = instance.service_by_code
    rooms = instance.room_by_code
    entries: list[AgendaEntry] = []
    for day in instance.days:
        masks = week.days[day.index]
        if not day.is_open:
            for start, end in day.hours.intervals:
                entries.append(AgendaEntry(day.index, start, end, AgendaKind.HOLIDAY, f"Feriado: {day.holiday}"))
            continue
        labelled = 0
        for absence in instance.absences:
            if absence.staff_code != staff_code or absence.day != day.date or not absence.blocks:
                continue
            if absence.start is None or absence.end is None:
                own = day.open_mask
            else:
                own = slot_mask_outward(absence.start, absence.end)
            # Cada tramo lleva el motivo de su ausencia; si dos ausencias se solapan, el tramo común va con la primera.
            own &= masks.absent & ~labelled
            labelled |= own
            label = f"Ausencia: {absence.kind}"
            for start, end in mask_intervals(own):
                entries.append(AgendaEntry(day.index, start, end, AgendaKind.ABSENCE, label, staff=staff_code))
        for blocking in instance.blockings:
            if not blocking.applies_on(day.index) or not blocking.applies_to(week.staff, week.role):
                continue
            mask = slot_mask_outward(blocking.start, blocking.end) & masks.present
            for start, end in mask_intervals(mask):
                entries.append(AgendaEntry(day.index, start, end, AgendaKind.BLOCKING, blocking.name, staff=staff_code))
    for session in plan.sessions:
        if session.staff != staff_code:
            continue
        service = services.get(session.service)
        room = rooms.get(session.room)
        entries.append(
            AgendaEntry(
                session.day,
                session.start,
                session.end,
                AgendaKind.SESSION,
                f"{service.name if service else session.service} - {room.name if room else session.room}",
                service.color if service else None,
                staff_code,
                session.room,
                session.service,
            )
        )
    admin_room = instance.admin_room.code if instance.admin_room else None
    for block in plan.admin:
        if block.staff == staff_code:
            label = "Administrativo"
            entries.append(
                AgendaEntry(block.day, block.start, block.end, AgendaKind.ADMIN, label, None, staff_code, admin_room)
            )
    return _assign_lanes(entries)


def room_agenda(instance: PlanningInstance, plan: Plan, room_code: str) -> tuple[AgendaEntry, ...]:
    """Agenda semanal de una sala.

    En las salas de atención cada sesión es una entrada. En la sala
    administrativa cada tramo con la misma ocupación es una entrada con el
    texto "n de H personas" (H es la capacidad simultánea dura).
    """
    room = instance.room_by_code.get(room_code)
    if room is None:
        return ()
    entries: list[AgendaEntry] = []
    for day in instance.days:
        if not day.is_open:
            for start, end in day.hours.intervals:
                entries.append(
                    AgendaEntry(day.index, start, end, AgendaKind.HOLIDAY, f"Feriado: {day.holiday}", room=room_code)
                )
    if room.is_admin_room:
        hard = room.simultaneous_hard or 0
        for day in instance.days:
            load = admin_slots_per_day(plan.admin, day.index)
            runs: list[list[int]] = []
            for start in sorted(load):
                people = load[start]
                if runs and runs[-1][1] == start and runs[-1][2] == people:
                    runs[-1][1] = start + SLOT_MINUTES
                elif people:
                    runs.append([start, start + SLOT_MINUTES, people])
            for start, end, people in runs:
                label = f"{people} de {hard} personas"
                entries.append(AgendaEntry(day.index, start, end, AgendaKind.ADMIN, label, room=room_code))
        return _assign_lanes(entries)
    services = instance.service_by_code
    for session in plan.sessions:
        if session.room != room_code:
            continue
        service = services.get(session.service)
        entries.append(
            AgendaEntry(
                session.day,
                session.start,
                session.end,
                AgendaKind.SESSION,
                f"{service.name if service else session.service} - {session.staff}",
                service.color if service else None,
                session.staff,
                room_code,
                session.service,
            )
        )
    return _assign_lanes(entries)
