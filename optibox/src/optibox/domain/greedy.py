"""Heurística voraz sobre los candidatos priorizados.

Recorre los candidatos de mayor a menor puntaje y coloca cada uno si respeta
todas las restricciones duras del modelo: persona y sala libres, contrato
(sesiones, administrativo y bloqueos), topes por tipo, demanda del bloque y
administrativo asociado el mismo día. Sirve como solución inicial (hint) del
modelo CP-SAT y como línea base para comparar.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from optibox.domain.instance import PlanningInstance
from optibox.domain.models import PRIORITY_WEIGHTS
from optibox.domain.plan import Candidate, Plan, Session, admin_blocks_from_slots
from optibox.domain.rules import Stage1Result
from optibox.domain.timegrid import SLOT_MINUTES, block_of, mask_slots


class CapacityLedger:
    """Libro único de capacidad: ocupación de personas, salas y sala administrativa, contrato y topes.

    Tanto la heurística como el diagnóstico de turnos sin cubrir usan este
    libro, de modo que ninguna estructura intermedia queda desincronizada.
    """

    def __init__(self, instance: PlanningInstance) -> None:
        self.instance = instance
        self.staff_busy: dict[tuple[str, int], int] = defaultdict(int)
        self.room_busy: dict[tuple[str, int], int] = defaultdict(int)
        self.admin_load: dict[tuple[int, int], int] = defaultdict(int)
        self.admin_slots: dict[tuple[str, int], int] = defaultdict(int)
        self.admin_required: Counter[tuple[str, int]] = Counter()
        self.used: Counter[str] = Counter()
        self.coverage: Counter[tuple[int, int, str]] = Counter()
        self.week_total: Counter[str] = Counter()
        self.week_staff: Counter[tuple[str, str]] = Counter()
        self.day_total: Counter[tuple[str, int]] = Counter()
        self.day_staff: Counter[tuple[str, str, int]] = Counter()
        self.sessions: list[Session] = []
        admin_room = instance.admin_room
        self.admin_hard = admin_room.simultaneous_hard if admin_room else None
        self.admin_soft = admin_room.soft_capacity if admin_room else None

    @classmethod
    def from_plan(cls, instance: PlanningInstance, plan: Plan) -> CapacityLedger:
        ledger = cls(instance)
        for session in plan.sessions:
            ledger._book_session(session)
        for block in plan.admin:
            for start in mask_slots(block.mask):
                ledger._book_admin(block.staff, block.day, start)
        return ledger

    def contract_left(self, staff: str) -> int:
        return self.instance.staff_by_code[staff].assignable_minutes - self.used[staff]

    def cap_reached(self, candidate: Candidate) -> bool:
        service = self.instance.service_by_code[candidate.service]
        checks = (
            (service.max_week_total, self.week_total[service.code]),
            (service.max_week_per_staff, self.week_staff[(service.code, candidate.staff)]),
            (service.max_day_total, self.day_total[(service.code, candidate.day)]),
            (service.max_day_per_staff, self.day_staff[(service.code, candidate.staff, candidate.day)]),
        )
        return any(cap is not None and count >= cap for cap, count in checks)

    def demand_left(self, candidate: Candidate) -> int:
        key = (candidate.day, block_of(candidate.start), candidate.service)
        item = self.instance.demand_by_key.get(key)
        return (item.sessions if item else 0) - self.coverage[key]

    def staff_free(self, candidate: Candidate) -> bool:
        return not candidate.mask & self.staff_busy[(candidate.staff, candidate.day)]

    def room_free(self, candidate: Candidate) -> bool:
        return not candidate.mask & self.room_busy[(candidate.room, candidate.day)]

    def free_admin_slots(self, staff: str, day: int, exclude: int = 0) -> list[int]:
        """Slots en que la persona podría hacer administrativo (libre y con cupo en la sala administrativa)."""
        workable = self.instance.staff_by_code[staff].days[day].workable
        free = workable & ~self.staff_busy[(staff, day)] & ~exclude
        return [
            start
            for start in mask_slots(free)
            if self.admin_hard is None or self.admin_load[(day, start)] < self.admin_hard
        ]

    def try_place(self, candidate: Candidate) -> bool:
        """Coloca la sesión y su administrativo asociado si todo cabe; si no, no cambia nada."""
        service = self.instance.service_by_code[candidate.service]
        if self.demand_left(candidate) <= 0 or self.cap_reached(candidate):
            return False
        if not self.staff_free(candidate) or not self.room_free(candidate):
            return False
        if self.contract_left(candidate.staff) < candidate.duration + service.admin_minutes:
            return False
        needed = service.admin_minutes // SLOT_MINUTES
        chosen: list[int] = []
        if needed:
            options = self.free_admin_slots(candidate.staff, candidate.day, exclude=candidate.mask)
            if len(options) < needed:
                return False
            chosen = self._pick_admin_slots(candidate, options, needed)
        room = self.instance.room_by_code[candidate.room]
        session = Session(
            staff=candidate.staff,
            service=candidate.service,
            room=candidate.room,
            day=candidate.day,
            start=candidate.start,
            duration=candidate.duration,
            participants=self.instance.participants(service, room),
        )
        self._book_session(session)
        for start in chosen:
            self._book_admin(candidate.staff, candidate.day, start)
        return True

    def _pick_admin_slots(self, candidate: Candidate, options: list[int], needed: int) -> list[int]:
        """Prefiere slots justo después de la sesión, luego antes, y la sala administrativa bajo su cupo blando."""

        def preference(start: int) -> tuple[int, int, int]:
            over_soft = int(self.admin_soft is not None and self.admin_load[(candidate.day, start)] >= self.admin_soft)
            after = start >= candidate.end
            distance = start - candidate.end if after else candidate.start - start
            return (over_soft, 0 if after else 1, distance)

        return sorted(options, key=preference)[:needed]

    def _book_session(self, session: Session) -> None:
        service = self.instance.service_by_code[session.service]
        mask = session.mask
        self.staff_busy[(session.staff, session.day)] |= mask
        self.room_busy[(session.room, session.day)] |= mask
        self.used[session.staff] += session.duration
        self.coverage[(session.day, block_of(session.start), session.service)] += 1
        self.week_total[session.service] += 1
        self.week_staff[(session.service, session.staff)] += 1
        self.day_total[(session.service, session.day)] += 1
        self.day_staff[(session.service, session.staff, session.day)] += 1
        self.admin_required[(session.staff, session.day)] += service.admin_minutes
        self.sessions.append(session)

    def _book_admin(self, staff: str, day: int, start: int) -> None:
        bit = 1 << (start // SLOT_MINUTES)
        self.staff_busy[(staff, day)] |= bit
        self.admin_slots[(staff, day)] |= bit
        self.admin_load[(day, start)] += 1
        self.used[staff] += SLOT_MINUTES

    def plan(self) -> Plan:
        admin = admin_blocks_from_slots({key: mask for key, mask in self.admin_slots.items() if mask})
        return Plan(sessions=tuple(self.sessions), admin=admin)


def weighted_coverage(instance: PlanningInstance, sessions: tuple[Session, ...]) -> int:
    """Valor de la fase A: sesiones que cubren demanda, ponderadas por la prioridad del bloque."""
    total = 0
    for session in sessions:
        item = instance.demand_by_key.get((session.day, block_of(session.start), session.service))
        if item is not None:
            total += PRIORITY_WEIGHTS[item.priority]
    return total


def greedy_plan(instance: PlanningInstance, stage1: Stage1Result) -> Plan:
    """Plan voraz: coloca candidatos en orden de puntaje mientras quepan."""
    ledger = CapacityLedger(instance)
    for scored in stage1.candidates:
        ledger.try_place(scored.candidate)
    return ledger.plan()
