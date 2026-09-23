"""Plan semanal: la sesión es la entidad de primera clase y los slots se derivan de ella."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from optibox.domain.timegrid import SLOT_MINUTES, mask_intervals, slot_mask


@dataclass(frozen=True, slots=True)
class Candidate:
    """Posible sesión: persona, tipo de atención, sala, día (0 = lunes) y minuto de inicio."""

    staff: str
    service: str
    room: str
    day: int
    start: int
    duration: int

    @property
    def end(self) -> int:
        return self.start + self.duration

    @property
    def mask(self) -> int:
        return slot_mask(self.start, self.end)


@dataclass(frozen=True, slots=True)
class Session:
    """Sesión asignada. `participants` es el cupo efectivo (atenciones que entrega)."""

    staff: str
    service: str
    room: str
    day: int
    start: int
    duration: int
    participants: int

    @property
    def end(self) -> int:
        return self.start + self.duration

    @property
    def mask(self) -> int:
        return slot_mask(self.start, self.end)

    @property
    def candidate(self) -> Candidate:
        return Candidate(self.staff, self.service, self.room, self.day, self.start, self.duration)


@dataclass(frozen=True, slots=True)
class AdminBlock:
    """Tramo continuo de trabajo administrativo de una persona en la sala administrativa."""

    staff: str
    day: int
    start: int
    end: int

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def mask(self) -> int:
        return slot_mask(self.start, self.end)


@dataclass(frozen=True)
class Plan:
    """Sesiones y tramos administrativos de una semana."""

    sessions: tuple[Session, ...] = ()
    admin: tuple[AdminBlock, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(sorted(self.sessions, key=_session_key)))
        object.__setattr__(self, "admin", tuple(sorted(self.admin, key=lambda a: (a.day, a.start, a.staff))))

    def admin_minutes(self, staff: str, day: int | None = None) -> int:
        return sum(a.duration for a in self.admin if a.staff == staff and (day is None or a.day == day))

    def session_minutes(self, staff: str | None = None) -> int:
        return sum(s.duration for s in self.sessions if staff is None or s.staff == staff)

    @property
    def attentions(self) -> int:
        """Atenciones entregadas: suma del cupo de cada sesión (no de sus slots)."""
        return sum(s.participants for s in self.sessions)


def _session_key(session: Session) -> tuple[int, int, str, str, str]:
    return (session.day, session.start, session.staff, session.room, session.service)


def admin_blocks_from_slots(slots: dict[tuple[str, int], int]) -> tuple[AdminBlock, ...]:
    """Convierte máscaras de slots administrativos por (persona, día) en tramos continuos."""
    blocks: list[AdminBlock] = []
    for (staff, day), mask in sorted(slots.items()):
        for start, end in mask_intervals(mask):
            blocks.append(AdminBlock(staff, day, start, end))
    return tuple(blocks)


def admin_masks(admin: tuple[AdminBlock, ...]) -> dict[tuple[str, int], int]:
    """Máscara de slots administrativos por (persona, día)."""
    masks: dict[tuple[str, int], int] = defaultdict(int)
    for block in admin:
        masks[(block.staff, block.day)] |= block.mask
    return dict(masks)


def admin_slots_per_day(admin: tuple[AdminBlock, ...], day: int) -> dict[int, int]:
    """Personas en la sala administrativa por minuto de inicio de slot."""
    load: dict[int, int] = defaultdict(int)
    for block in admin:
        if block.day != day:
            continue
        for start in range(block.start, block.end, SLOT_MINUTES):
            load[start] += 1
    return dict(load)
