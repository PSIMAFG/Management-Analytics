"""Corridas de optimización: resumen persistido y detalle completo para mostrar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from optibox.domain.diagnosis import UnmetDemand
from optibox.domain.instance import PlanningInstance
from optibox.domain.metrics import PlanMetrics, ratio
from optibox.domain.plan import Plan


@dataclass(frozen=True)
class RuleExclusion:
    """Candidatos excluidos por una regla de la etapa 1 (primera regla que falla)."""

    position: int
    code: str
    description: str
    excluded: int


@dataclass(frozen=True)
class RunSummary:
    """Fila del historial de corridas."""

    id: int
    created_at: datetime
    week_start: date
    scenario_name: str
    status: str
    time_limit_s: float
    seed: int
    workers: int
    demand_sessions: int
    covered_sessions: int
    greedy_covered_sessions: int
    phase_a_status: str
    phase_a_value: int | None
    phase_a_bound: int | None
    phase_a_seconds: float
    phase_b_status: str | None
    phase_b_value: int | None
    phase_b_seconds: float | None
    greedy_value: int
    candidates: int
    variables: int
    build_seconds: float
    total_seconds: float
    session_minutes: int
    room_utilization: float | None

    @property
    def coverage_pct(self) -> float | None:
        return ratio(self.covered_sessions, self.demand_sessions)

    @property
    def greedy_coverage_pct(self) -> float | None:
        return ratio(self.greedy_covered_sessions, self.demand_sessions)

    @property
    def assigned_hours(self) -> float:
        return self.session_minutes / 60

    @property
    def label(self) -> str:
        """Texto corto para listas: fecha de la corrida, semana y escenario."""
        return f"#{self.id} {self.week_start.strftime('%d-%m-%Y')} - {self.scenario_name}"


@dataclass(frozen=True)
class RunDetail:
    """Todo lo que la interfaz necesita para mostrar una corrida."""

    summary: RunSummary
    instance: PlanningInstance
    plan: Plan
    metrics: PlanMetrics
    unmet: tuple[UnmetDemand, ...]
    exclusions: tuple[RuleExclusion, ...]
