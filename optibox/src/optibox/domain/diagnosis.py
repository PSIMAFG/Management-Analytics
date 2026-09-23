"""Causas de los turnos sin cubrir.

Para cada (día, bloque, tipo) con faltante se explica por qué no se asignó
más:

- Sin candidatos suficientes: la etapa 1 generó menos candidatos que los
  necesarios y todos quedaron asignados (o no generó ninguno); se informa la
  regla que más candidatos excluyó entre las personas competentes.
- Con candidatos, cada candidato no elegido se revisa contra el plan final.
  El administrativo adicional (el que supera lo que exigen las sesiones) es
  opcional y no limita la cobertura, así que se libera; el administrativo
  exigido puede moverse dentro del mismo día. Se toma la primera razón que
  impide agregar el candidato, en este orden: tope alcanzado, contrato
  agotado, personal ocupado, salas ocupadas, sin espacio para el
  administrativo asociado.
- Si algún candidato no elegido podía agregarse sin romper nada, el faltante
  no se debe a la capacidad: la causa es el piso de cobertura del escenario
  (cuando acepta menos cobertura a cambio de calidad) o que la búsqueda
  terminó por tiempo antes de alcanzar el óptimo. Si ninguno cabía, la causa
  es la razón más frecuente entre los candidatos no elegidos.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum

from optibox.domain.greedy import CapacityLedger
from optibox.domain.instance import PlanningInstance
from optibox.domain.metrics import CoverageRow, compute_metrics
from optibox.domain.plan import Candidate, Plan, admin_blocks_from_slots, admin_masks
from optibox.domain.rules import RULE_DESCRIPTIONS, DemandDiagnosis, Stage1Result
from optibox.domain.timegrid import SLOT_MINUTES, block_of, mask_slots


class UnmetCause(StrEnum):
    NO_CANDIDATES = "sin_candidatos"
    CAP = "tope"
    CONTRACT = "contrato"
    STAFF_BUSY = "personal_ocupado"
    ROOM_BUSY = "sala_ocupada"
    ADMIN = "administrativo"
    COVERAGE_FLOOR = "piso_cobertura"
    SEARCH_LIMIT = "limite_busqueda"


CAUSE_LABELS = {
    UnmetCause.NO_CANDIDATES: "Sin candidatos suficientes",
    UnmetCause.CAP: "Tope alcanzado",
    UnmetCause.CONTRACT: "Contrato agotado",
    UnmetCause.STAFF_BUSY: "Personal ocupado",
    UnmetCause.ROOM_BUSY: "Salas ocupadas",
    UnmetCause.ADMIN: "Sin espacio para el administrativo",
    UnmetCause.COVERAGE_FLOOR: "Piso de cobertura del escenario",
    UnmetCause.SEARCH_LIMIT: "La búsqueda no alcanzó el óptimo",
}

_ORDER = list(UnmetCause)


@dataclass(frozen=True)
class UnmetDemand:
    """Faltante de un (día, bloque, tipo) y su causa explicada."""

    day: int
    block: int
    service: str
    required: int
    covered: int
    cause: UnmetCause
    detail: str
    rule_code: str | None = None

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.covered)

    @property
    def cause_label(self) -> str:
        return CAUSE_LABELS[self.cause]


class PlanCapacity:
    """Capacidad que deja un plan para agregar una sesión más.

    Parte del plan con solo el administrativo exigido por sus sesiones: de
    cada persona y día se conservan tantos slots administrativos como piden
    sus sesiones y el adicional se libera. Como el administrativo exigido
    puede cambiar de hora dentro del día, una sesión nueva solo choca con las
    sesiones de la persona, y basta con que queden slots libres (con cupo en
    la sala administrativa) para el administrativo del día más el de la
    sesión nueva.
    """

    def __init__(self, instance: PlanningInstance, plan: Plan) -> None:
        self.instance = instance
        required: Counter[tuple[str, int]] = Counter()
        self.sessions_busy: dict[tuple[str, int], int] = defaultdict(int)
        for session in plan.sessions:
            required[(session.staff, session.day)] += instance.service_by_code[session.service].admin_minutes
            self.sessions_busy[(session.staff, session.day)] |= session.mask
        kept: dict[tuple[str, int], int] = {}
        for key, mask in admin_masks(plan.admin).items():
            for start in mask_slots(mask)[: required[key] // SLOT_MINUTES]:
                kept[key] = kept.get(key, 0) | 1 << (start // SLOT_MINUTES)
        self.ledger = CapacityLedger.from_plan(instance, Plan(plan.sessions, admin_blocks_from_slots(kept)))

    def blocking_reason(self, candidate: Candidate) -> UnmetCause | None:
        """Primera razón que impide agregar el candidato, o None si cabe."""
        ledger = self.ledger
        admin_minutes = self.instance.service_by_code[candidate.service].admin_minutes
        if ledger.cap_reached(candidate):
            return UnmetCause.CAP
        if ledger.contract_left(candidate.staff) < candidate.duration + admin_minutes:
            return UnmetCause.CONTRACT
        if candidate.mask & self.sessions_busy[(candidate.staff, candidate.day)]:
            return UnmetCause.STAFF_BUSY
        if not ledger.room_free(candidate):
            return UnmetCause.ROOM_BUSY
        if not self._admin_fits(candidate, admin_minutes):
            return UnmetCause.ADMIN
        return None

    def _admin_fits(self, candidate: Candidate, admin_minutes: int) -> bool:
        ledger = self.ledger
        key = (candidate.staff, candidate.day)
        needed = (ledger.admin_required[key] + admin_minutes) // SLOT_MINUTES
        if needed == 0:
            return True
        own = ledger.admin_slots[key]
        workable = self.instance.staff_by_code[candidate.staff].days[candidate.day].workable
        free = workable & ~self.sessions_busy[key] & ~candidate.mask
        hard = ledger.admin_hard
        usable = 0
        for start in mask_slots(free):
            others = ledger.admin_load[(candidate.day, start)] - (own >> (start // SLOT_MINUTES) & 1)
            if hard is None or others < hard:
                usable += 1
        return usable >= needed


def _no_candidates(row: CoverageRow, service: str, diagnosis: DemandDiagnosis | None) -> UnmetDemand:
    rule = diagnosis.dominant_rule if diagnosis is not None else None
    found = diagnosis.candidates if diagnosis is not None else 0
    reason = f" La regla que más excluye es: {RULE_DESCRIPTIONS.get(rule, rule).lower()}." if rule else ""
    if found == 0:
        detail = "Ninguna combinación de persona, sala y horario cumple las reglas." + reason
    elif found == 1:
        detail = "Solo hay 1 candidato y quedó asignado." + reason
    else:
        detail = f"Solo hay {found} candidatos y todos quedaron asignados." + reason
    return UnmetDemand(row.day, row.block, service, row.required, row.covered, UnmetCause.NO_CANDIDATES, detail, rule)


def _capacity_cause(
    instance: PlanningInstance, row: CoverageRow, service: str, pending: list[Candidate], capacity: PlanCapacity
) -> UnmetDemand:
    reasons: Counter[UnmetCause] = Counter()
    addable = 0
    for candidate in pending:
        reason = capacity.blocking_reason(candidate)
        if reason is None:
            addable += 1
        else:
            reasons[reason] += 1
    noun = "candidato no elegido" if len(pending) == 1 else "candidatos no elegidos"
    if addable:
        floor = instance.scenario.coverage_floor_pct
        verb = "cabía" if addable == 1 else "cabían"
        if floor < 100:
            cause = UnmetCause.COVERAGE_FLOOR
            why = f"el escenario acepta hasta {100 - floor} % menos de cobertura ponderada a cambio de calidad."
        else:
            cause = UnmetCause.SEARCH_LIMIT
            why = "la búsqueda terminó por tiempo antes de alcanzar el óptimo."
        detail = f"{addable} de {len(pending)} {noun} {verb} en el plan: {why}"
    else:
        cause = max(reasons, key=lambda c: (reasons[c], -_ORDER.index(c)))
        detail = f"{reasons[cause]} de {len(pending)} {noun}: {CAUSE_LABELS[cause].lower()}."
    return UnmetDemand(row.day, row.block, service, row.required, row.covered, cause, detail)


def diagnose_unmet(instance: PlanningInstance, plan: Plan, stage1: Stage1Result) -> tuple[UnmetDemand, ...]:
    """Lista de turnos sin cubrir con su causa, ordenada por día, bloque y tipo."""
    metrics = compute_metrics(instance, plan)
    capacity = PlanCapacity(instance, plan)
    chosen = {session.candidate for session in plan.sessions}
    by_key: dict[tuple[int, int, str], list[Candidate]] = defaultdict(list)
    for scored in stage1.candidates:
        cand = scored.candidate
        by_key[(cand.day, block_of(cand.start), cand.service)].append(cand)

    result: list[UnmetDemand] = []
    for row in metrics.coverage_by_block_service:
        if row.shortfall <= 0 or row.service is None:
            continue
        key = (row.day, row.block, row.service)
        pending = [cand for cand in by_key.get(key, []) if cand not in chosen]
        if pending:
            result.append(_capacity_cause(instance, row, row.service, pending, capacity))
        else:
            result.append(_no_candidates(row, row.service, stage1.diagnosis.get(key)))
    return tuple(result)
