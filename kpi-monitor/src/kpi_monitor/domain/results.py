"""Resultados del motor: evaluación de cada indicador por sede y red, cortes, series y resumen.

Son objetos inmutables y tipados; la interfaz y los reportes solo los leen y
no recalculan nada.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from kpi_monitor.domain.enums import ProjectionMethod, Status
from kpi_monitor.domain.goals import GoalResolution
from kpi_monitor.domain.models import Indicator, Period

NETWORK_LABEL = "Red"


@dataclass(frozen=True)
class ProjectionPoint:
    """Valor acumulado proyectado a fin de un mes futuro, con su banda p10-p90."""

    month: int
    central: float | None
    p10: float | None
    p90: float | None


@dataclass(frozen=True)
class Projection:
    """Proyección única al cierre del año para un indicador en un nivel."""

    method: ProjectionMethod
    value: float | None = None
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None
    compliance: float | None = None
    status: Status | None = None
    probability: float | None = None
    months_observed: int = 0
    months_projected: int = 0
    horizon: int = 0
    current_rhythm: float | None = None
    required_rhythm: float | None = None
    draws: int = 0
    path: tuple[ProjectionPoint, ...] = ()

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class MonthlyPoint:
    """Situación de un mes: aporte del mes y acumulado a esa fecha."""

    month: int
    expected: bool
    reported: bool | None
    numerator: float | None
    denominator: float | None
    monthly_value: float | None
    cumulative_value: float | None
    goal_to_date: float | None
    compliance: float | None
    status: Status


@dataclass(frozen=True)
class CutResult:
    """Evaluación en un mes de corte oficial; los cortes futuros quedan pendientes."""

    month: int
    status: Status
    value: float | None
    compliance: float | None


@dataclass(frozen=True)
class IndicatorResult:
    """Evaluación de un indicador en una sede (o en la red) para un período."""

    indicator: Indicator
    period: Period
    site_code: str | None
    site_name: str
    numerator: float | None
    denominator: float | None
    months_reported: int
    goal: GoalResolution
    goal_to_date: float | None
    compliance: float | None
    status: Status
    weight: float
    projection: Projection
    monthly: tuple[MonthlyPoint, ...] = ()
    cuts: tuple[CutResult, ...] = ()
    note: str = ""
    stock_month: int | None = None
    stock_carried: bool = False
    previous_same_period: float | None = None
    sites_included: tuple[str, ...] = ()
    # Meses informados con que se prorratea la meta a la fecha (en la red, ponderados por denominador).
    months_equivalent: float = 0.0

    @property
    def is_network(self) -> bool:
        return self.site_code is None

    @property
    def value(self) -> float | None:
        if self.numerator is None or not self.denominator:
            return None
        return self.numerator / self.denominator

    @property
    def indicator_code(self) -> str:
        return self.indicator.code

    @property
    def program_code(self) -> str:
        return self.indicator.program_code

    @property
    def capped_compliance(self) -> float | None:
        """Cumplimiento con tope de 100 %, el que entra al índice ponderado."""
        return None if self.compliance is None else min(1.0, self.compliance)

    @property
    def capped_projected_compliance(self) -> float | None:
        """Cumplimiento proyectado al cierre con tope de 100 %, el que entra al índice proyectado."""
        projected = self.projection.compliance
        return None if projected is None else min(1.0, projected)

    @property
    def change_vs_previous_year(self) -> float | None:
        """Variación relativa frente al mismo período del año anterior (mismos meses, no el año completo).

        Por ejemplo 0,12 significa un valor 12 % mayor que el acumulado de enero al
        mismo mes del año anterior. None si falta alguno de los dos valores o el
        anterior es cero.
        """
        current = self.value
        previous = self.previous_same_period
        if current is None or previous is None or previous == 0:
            return None
        return current / previous - 1.0


@dataclass(frozen=True)
class IndicatorEvaluation:
    """Evaluación de un indicador en todas las sedes y en la red."""

    indicator: Indicator
    network: IndicatorResult
    sites: tuple[IndicatorResult, ...]

    def for_level(self, site_code: str | None) -> IndicatorResult:
        if site_code is None:
            return self.network
        for result in self.sites:
            if result.site_code == site_code:
                return result
        raise KeyError(site_code)

    @property
    def all_results(self) -> tuple[IndicatorResult, ...]:
        return (self.network, *self.sites)


@dataclass(frozen=True)
class StatusCounts:
    """Conteo de indicadores por estado del semáforo."""

    counts: dict[Status, int] = field(default_factory=dict)

    @classmethod
    def from_results(cls, results: Iterable[IndicatorResult]) -> StatusCounts:
        return cls(dict(Counter(r.status for r in results)))

    def get(self, status: Status) -> int:
        return self.counts.get(status, 0)

    @property
    def green(self) -> int:
        return self.get(Status.GREEN)

    @property
    def yellow(self) -> int:
        return self.get(Status.YELLOW)

    @property
    def red(self) -> int:
        return self.get(Status.RED)

    @property
    def no_data(self) -> int:
        """Indicadores sin evaluación de color: sin datos o con meta no determinable."""
        return self.get(Status.NO_DATA) + self.get(Status.UNDETERMINED)

    @property
    def total(self) -> int:
        return sum(self.counts.values())
