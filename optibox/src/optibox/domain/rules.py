"""Etapa 1: cadena de reglas que filtra y prioriza candidatos.

Cada candidato (persona, tipo de atención, sala, día, inicio) recorre una
cadena ordenada de reglas; la primera que falla registra su código y un motivo
en español. Las reglas son objetos pequeños con `code`, `description`,
`scope` y `check`.

El `scope` declara de qué dimensiones depende la regla. El generador lo usa
para evaluar cada regla una sola vez por combinación relevante (por ejemplo,
el contrato una vez por persona) y contar en bloque los candidatos excluidos,
sin perder la semántica de "primera regla que falla".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

from optibox.domain.instance import PlanningInstance
from optibox.domain.models import frozen_mapping
from optibox.domain.plan import Candidate
from optibox.domain.timegrid import SLOT_MINUTES, block_of

PRIORITY_SCORE = {1: 300, 2: 200, 3: 100}
NO_FAILURE = 10**6


class Scope(IntEnum):
    """Dimensiones de las que depende una regla (de la más externa a la más interna)."""

    STAFF = 1
    STAFF_SERVICE = 2
    STAFF_TIME = 3
    ROOM = 4
    TIME = 5
    DEMAND = 6


class Rule(ABC):
    """Regla de la etapa 1. `check` devuelve el motivo de exclusión o None si pasa."""

    code: ClassVar[str]
    description: ClassVar[str]
    scope: ClassVar[Scope]

    @abstractmethod
    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None: ...


class ContractRule(Rule):
    code = "contrato"
    description = "Contrato vigente en la semana"
    scope = Scope.STAFF

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        staff = instance.staff_by_code[candidate.staff]
        if staff.contract is None:
            return "No tiene un contrato vigente en la semana."
        if staff.assignable_minutes <= 0:
            return "Los bloqueos consumen todo el contrato de la semana."
        return None


class CompetenceRule(Rule):
    code = "competencia"
    description = "Competencia para el tipo de atención"
    scope = Scope.STAFF_SERVICE

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        staff = instance.staff_by_code[candidate.staff]
        if not staff.role.delivers_services:
            return "Su cargo no realiza atenciones."
        if candidate.service not in staff.role.services:
            return "Su cargo no realiza este tipo de atención."
        skills = staff.staff.skills
        if skills and candidate.service not in skills:
            return "No está habilitada individualmente para este tipo de atención."
        return None


class AvailabilityRule(Rule):
    """La sesión cabe en una ventana de disponibilidad de un día que el contrato cubre."""

    code = "disponibilidad"
    description = "Disponibilidad horaria de la persona"
    scope = Scope.STAFF_TIME

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        day = instance.staff_by_code[candidate.staff].days[candidate.day]
        if not day.in_contract:
            return "No tiene un contrato vigente ese día."
        mask = candidate.mask
        if mask == 0 or mask & ~day.available:
            return "La sesión no cabe completa en una ventana de disponibilidad."
        return None


class AbsenceRule(Rule):
    code = "ausencia"
    description = "Ausencias aprobadas y feriados"
    scope = Scope.STAFF_TIME

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        if not instance.days[candidate.day].is_open:
            return "El día es feriado."
        if candidate.mask & instance.staff_by_code[candidate.staff].days[candidate.day].absent:
            return "Tiene una ausencia aprobada en ese horario."
        return None


class BlockingRule(Rule):
    code = "bloqueo"
    description = "Bloqueos y reuniones"
    scope = Scope.STAFF_TIME

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        if candidate.mask & instance.staff_by_code[candidate.staff].days[candidate.day].blocked:
            return "Se cruza con un bloqueo o reunión."
        return None


class RoomCompatibilityRule(Rule):
    code = "sala_compatible"
    description = "Sala activa y compatible con el tipo de atención"
    scope = Scope.ROOM

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        room = instance.room_by_code[candidate.room]
        service = instance.service_by_code[candidate.service]
        if not room.active:
            return "La sala no está activa."
        if room.is_admin_room:
            return "La sala administrativa no aloja atenciones."
        if room.code not in service.rooms:
            return "La sala no está habilitada para este tipo de atención."
        if service.requires_group_room and not room.allows_group:
            return "El tipo de atención requiere una sala que admita grupos."
        if service.requires_evaluation_room and not room.allows_evaluation:
            return "El tipo de atención requiere una sala que admita evaluaciones."
        staff = instance.staff_by_code[candidate.staff]
        if room.reserved_role is not None and room.reserved_role != staff.role.code:
            return "La sala está reservada para otro cargo."
        return None


class RoomPermissionRule(Rule):
    code = "sala_permitida"
    description = "Sala permitida para la persona"
    scope = Scope.ROOM

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        staff = instance.staff_by_code[candidate.staff].staff
        if candidate.room in staff.forbidden_rooms:
            return "La sala está prohibida para la persona."
        if staff.has_room_matrix and candidate.room not in staff.allowed_rooms:
            return "La sala no está en la lista de salas permitidas de la persona."
        return None


class ScheduleFitRule(Rule):
    code = "horario"
    description = "Cabe en el horario del centro sin cruzar almuerzo ni cierre"
    scope = Scope.TIME

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        if candidate.start % SLOT_MINUTES or candidate.duration % SLOT_MINUTES:
            return "La sesión no está alineada a la grilla de 15 minutos."
        if not instance.days[candidate.day].hours.contains(candidate.start, candidate.end):
            return "La sesión cruza el almuerzo o el cierre del centro."
        return None


class DemandRule(Rule):
    code = "demanda"
    description = "Demanda del tipo de atención en el bloque de inicio"
    scope = Scope.DEMAND

    def check(self, instance: PlanningInstance, candidate: Candidate) -> str | None:
        item = instance.demand_by_key.get((candidate.day, block_of(candidate.start), candidate.service))
        if item is None or item.sessions <= 0:
            return "No hay demanda de este tipo de atención en el bloque."
        return None


DEFAULT_RULES: tuple[Rule, ...] = (
    ContractRule(),
    CompetenceRule(),
    AvailabilityRule(),
    AbsenceRule(),
    BlockingRule(),
    RoomCompatibilityRule(),
    RoomPermissionRule(),
    ScheduleFitRule(),
    DemandRule(),
)

RULE_DESCRIPTIONS = {rule.code: rule.description for rule in DEFAULT_RULES}


class RuleChain:
    """Cadena ordenada de reglas.

    Exige que las reglas de persona precedan a las de persona y tipo, y que
    estas precedan al resto: así el generador puede descartar en bloque.
    """

    def __init__(self, rules: Sequence[Rule] = DEFAULT_RULES) -> None:
        self.rules = tuple(rules)
        outer = [rule.scope for rule in self.rules if rule.scope <= Scope.STAFF_SERVICE]
        prefix = [rule.scope for rule in self.rules[: len(outer)]]
        if outer != prefix or outer != sorted(outer):
            raise ValueError("Las reglas de persona y de persona-tipo deben ir primero y en ese orden.")
        codes = [rule.code for rule in self.rules]
        if len(codes) != len(set(codes)):
            raise ValueError("Los códigos de regla deben ser únicos.")

    def first_failure(self, instance: PlanningInstance, candidate: Candidate) -> tuple[Rule, str] | None:
        for rule in self.rules:
            reason = rule.check(instance, candidate)
            if reason is not None:
                return rule, reason
        return None


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: int


@dataclass(frozen=True)
class DemandDiagnosis:
    """Candidatos generados para un (día, bloque, tipo) con demanda y exclusiones por regla.

    `rule_counts` cuenta las combinaciones excluidas por cada regla entre las
    personas que pasan las reglas de persona y de tipo; `dominant_rule` es la
    que más excluyó (o, si nadie es competente, la regla de persona o de tipo
    que falló para más personas).
    """

    key: tuple[int, int, str]
    candidates: int
    rule_counts: Mapping[str, int] = field(default_factory=dict)
    dominant_rule: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_counts", frozen_mapping(self.rule_counts))


@dataclass(frozen=True)
class Stage1Result:
    """Candidatos priorizados, conteo de exclusiones por regla y diagnóstico por demanda."""

    candidates: tuple[ScoredCandidate, ...]
    exclusions: Mapping[str, int]
    evaluated: int
    diagnosis: Mapping[tuple[int, int, str], DemandDiagnosis]

    def __post_init__(self) -> None:
        object.__setattr__(self, "exclusions", frozen_mapping(self.exclusions))
        object.__setattr__(self, "diagnosis", frozen_mapping(self.diagnosis))


def candidate_score(instance: PlanningInstance, candidate: Candidate) -> int:
    """Puntaje de prioridad: prioridad de la demanda, luego del tipo, luego ranking de sala preferida."""
    item = instance.demand_by_key[(candidate.day, block_of(candidate.start), candidate.service)]
    service = instance.service_by_code[candidate.service]
    rank = instance.staff_by_code[candidate.staff].staff.preference_rank(candidate.room)
    room_bonus = max(0, 6 - rank) if rank is not None else 0
    return PRIORITY_SCORE[item.priority] + (4 - service.priority) * 10 + room_bonus


def generate_candidates(instance: PlanningInstance, chain: RuleChain | None = None) -> Stage1Result:
    """Genera y prioriza candidatos aplicando la cadena de reglas.

    El universo evaluado es persona x tipo x sala x día x slot abierto. Las
    reglas se evalúan en el nivel que indica su scope y los descartes se
    cuentan en bloque para la regla que falla primero en el orden de la cadena.
    """
    chain = chain or RuleChain()
    rules = chain.rules
    index = {rule.code: i for i, rule in enumerate(rules)}
    staff_rules = [(i, r) for i, r in enumerate(rules) if r.scope is Scope.STAFF]
    service_rules = [(i, r) for i, r in enumerate(rules) if r.scope is Scope.STAFF_SERVICE]
    room_rules = [(i, r) for i, r in enumerate(rules) if r.scope is Scope.ROOM]
    inner_rules = [(i, r) for i, r in enumerate(rules) if r.scope in (Scope.STAFF_TIME, Scope.TIME, Scope.DEMAND)]

    starts = [day.hours.slot_starts() for day in instance.days]
    total_starts = sum(len(s) for s in starts)
    rooms = instance.rooms
    services = instance.service_types
    per_service_universe = total_starts * len(rooms)

    counts = [0] * len(rules)
    key_counts: dict[tuple[int, int, str], list[int]] = defaultdict(lambda: [0] * len(rules))
    outer_counts: dict[str, Counter[int]] = defaultdict(Counter)
    scored: list[ScoredCandidate] = []
    candidates_per_key: Counter[tuple[int, int, str]] = Counter()

    def first(checks: list[tuple[int, Rule]], candidate: Candidate) -> int:
        for i, rule in checks:
            if rule.check(instance, candidate) is not None:
                return i
        return NO_FAILURE

    probe_room = rooms[0].code if rooms else ""
    for staff in instance.staff:
        probe = Candidate(staff.code, services[0].code if services else "", probe_room, 0, 0, SLOT_MINUTES)
        failed = first(staff_rules, probe)
        if failed != NO_FAILURE:
            counts[failed] += per_service_universe * len(services)
            for service in services:
                outer_counts[service.code][failed] += 1
            continue
        for service in services:
            duration = service.duration_min
            probe = Candidate(staff.code, service.code, probe_room, 0, 0, duration)
            failed = first(service_rules, probe)
            if failed != NO_FAILURE:
                counts[failed] += per_service_universe
                outer_counts[service.code][failed] += 1
                continue
            room_groups: Counter[int] = Counter()
            ok_rooms: list[str] = []
            for room in rooms:
                room_failed = first(room_rules, Candidate(staff.code, service.code, room.code, 0, 0, duration))
                room_groups[room_failed] += 1
                if room_failed == NO_FAILURE:
                    ok_rooms.append(room.code)
            for day in instance.days:
                for start in starts[day.index]:
                    probe = Candidate(staff.code, service.code, probe_room, day.index, start, duration)
                    inner_failed = first(inner_rules, probe)
                    key = (day.index, block_of(start), service.code)
                    tracked = key in instance.demand_by_key
                    for room_failed, amount in room_groups.items():
                        failed = min(inner_failed, room_failed)
                        if failed == NO_FAILURE:
                            continue
                        counts[failed] += amount
                        if tracked:
                            key_counts[key][failed] += amount
                    if inner_failed != NO_FAILURE:
                        continue
                    for room_code in ok_rooms:
                        candidate = Candidate(staff.code, service.code, room_code, day.index, start, duration)
                        scored.append(ScoredCandidate(candidate, candidate_score(instance, candidate)))
                        candidates_per_key[key] += 1

    scored.sort(
        key=lambda sc: (
            -sc.score,
            sc.candidate.day,
            sc.candidate.start,
            sc.candidate.staff,
            sc.candidate.room,
            sc.candidate.service,
        )
    )
    diagnosis: dict[tuple[int, int, str], DemandDiagnosis] = {}
    for item in instance.demand:
        key = item.key
        rule_counts = {rules[i].code: n for i, n in enumerate(key_counts[key]) if n} if key in key_counts else {}
        found = candidates_per_key.get(key, 0)
        dominant: str | None = None
        if rule_counts:
            # Entre las personas competentes, la regla que más combinaciones excluyó (a igualdad, la primera).
            dominant = max(rule_counts, key=lambda code: (rule_counts[code], -index[code]))
        elif outer_counts[key[2]]:
            top = max(outer_counts[key[2]].items(), key=lambda kv: (kv[1], -kv[0]))[0]
            dominant = rules[top].code
        elif found == 0:
            dominant = "competencia" if "competencia" in index else rules[0].code
        diagnosis[key] = DemandDiagnosis(key=key, candidates=found, rule_counts=rule_counts, dominant_rule=dominant)

    evaluated = per_service_universe * len(services) * len(instance.staff)
    return Stage1Result(
        candidates=tuple(scored),
        exclusions={rule.code: counts[i] for i, rule in enumerate(rules)},
        evaluated=evaluated,
        diagnosis=diagnosis,
    )
