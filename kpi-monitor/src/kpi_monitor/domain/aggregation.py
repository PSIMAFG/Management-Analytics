"""Acumulación de numeradores y denominadores dentro del año.

Reglas:
- El acumulado es suma de numeradores sobre suma de denominadores; nunca un promedio de porcentajes.
- Un stock (personas al corte) nunca se suma: se usa el último corte disponible del año.
- Un denominador de stock sin corte en el año arrastra el último corte del año anterior; nunca se
  rellena hacia atrás con un corte posterior.
- El multiplicador k se aplica una sola vez, aquí, sobre el stock crudo de personas.
- Un mes no reportado no aporta nada: faltante no es cero.
- La red es la suma de las sedes con dato (numeradores y denominadores).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kpi_monitor.domain.enums import Aggregation, DenominatorType
from kpi_monitor.domain.models import Indicator, Observation


@dataclass(frozen=True)
class SiteSeries:
    """Observaciones de un indicador en una sede, con sus metas fijas y poblaciones por año."""

    site_code: str
    observations: Mapping[tuple[int, int], Observation]
    targets: Mapping[int, float] = field(default_factory=dict)
    populations: Mapping[int, int] = field(default_factory=dict)

    def get(self, year: int, month: int) -> Observation | None:
        return self.observations.get((year, month))

    def reported(self, year: int, month: int) -> Observation | None:
        """Observación del mes solo si la sede la informó."""
        obs = self.observations.get((year, month))
        return obs if obs is not None and obs.reported else None


@dataclass(frozen=True)
class StockReading:
    """Valor de stock vigente y de qué corte proviene."""

    value: float | None
    month: int | None = None
    carried: bool = False


@dataclass(frozen=True)
class Aggregate:
    """Numerador y denominador efectivos a un mes de corte."""

    numerator: float | None
    denominator: float | None
    months_reported: int = 0
    stock: StockReading = field(default_factory=lambda: StockReading(None))
    weighted_months: float | None = None

    @property
    def months_equivalent(self) -> float:
        """Meses informados que respaldan el acumulado; con ellos se prorratea la meta a la fecha.

        En una sede son los meses informados. En la red es el promedio de los meses
        informados de cada sede ponderado por su denominador, de modo que la meta a
        la fecha de la red es la suma de las metas a la fecha de sus sedes.
        """
        return float(self.months_reported) if self.weighted_months is None else self.weighted_months

    @property
    def has_data(self) -> bool:
        """Hay numerador y un denominador positivo."""
        return self.numerator is not None and self.denominator is not None and self.denominator > 0

    @property
    def value(self) -> float | None:
        if not self.has_data:
            return None
        assert self.numerator is not None
        assert self.denominator is not None
        return self.numerator / self.denominator


def reported_months(series: SiteSeries, year: int, month: int) -> list[int]:
    """Meses del año, hasta el mes de corte, que la sede informó."""
    return [m for m in range(1, month + 1) if series.reported(year, m) is not None]


def numerator_stock(series: SiteSeries, year: int, month: int) -> StockReading:
    """Último stock informado en el numerador dentro del año (no se arrastra entre años)."""
    for m in range(month, 0, -1):
        obs = series.reported(year, m)
        if obs is not None and obs.numerator is not None:
            return StockReading(obs.numerator, m)
    return StockReading(None)


def denominator_stock(series: SiteSeries, year: int, month: int) -> StockReading:
    """Último stock de personas informado en el denominador hasta el mes de corte.

    Si el año aún no tiene corte, se arrastra el último corte del año anterior.
    Nunca se usa un corte posterior al mes evaluado.
    """
    for m in range(month, 0, -1):
        obs = series.reported(year, m)
        if obs is not None and obs.denominator is not None:
            return StockReading(obs.denominator, m)
    for m in range(12, 0, -1):
        obs = series.reported(year - 1, m)
        if obs is not None and obs.denominator is not None:
            return StockReading(obs.denominator, m, carried=True)
    return StockReading(None)


def effective_denominator(
    indicator: Indicator, series: SiteSeries, year: int, month: int, prevalence: float
) -> tuple[float | None, StockReading]:
    """Denominador efectivo para los tipos que no se acumulan (fijo, stock, k × stock, población)."""
    kind = indicator.denominator_type
    if kind is DenominatorType.FIXED_SITE:
        return series.targets.get(year), StockReading(None)
    if kind is DenominatorType.POPULATION:
        population = series.populations.get(year)
        return (prevalence * population if population else None), StockReading(None)
    if kind in (DenominatorType.STOCK, DenominatorType.K_STOCK):
        reading = denominator_stock(series, year, month)
        if reading.value is None:
            return None, reading
        factor = indicator.multiplier if kind is DenominatorType.K_STOCK else 1.0
        return factor * reading.value, reading
    raise ValueError(f"El denominador {kind} se acumula mes a mes")


def aggregate_site(indicator: Indicator, series: SiteSeries, year: int, month: int, prevalence: float) -> Aggregate:
    """Acumulado del año hasta `month` para una sede."""
    months = reported_months(series, year, month)
    stock = StockReading(None)
    if indicator.aggregation is Aggregation.STOCK:
        stock = numerator_stock(series, year, month)
        numerator = stock.value
    else:
        values = [obs.numerator for m in months if (obs := series.reported(year, m)) and obs.numerator is not None]
        numerator = float(sum(values)) if values else None

    if indicator.has_flow_denominator:
        dens = [obs.denominator for m in months if (obs := series.reported(year, m)) and obs.denominator is not None]
        denominator = float(sum(dens)) if dens else None
    else:
        denominator, den_stock = effective_denominator(indicator, series, year, month, prevalence)
        if indicator.has_stock_denominator:
            stock = den_stock
    return Aggregate(numerator, denominator, len(months), stock)


def aggregate_network(aggregates: Sequence[Aggregate]) -> tuple[Aggregate, tuple[int, ...]]:
    """Suma de las sedes con dato y los índices de las sedes incluidas.

    La red nunca toma el estado de la peor sede ni promedia porcentajes: es la
    razón de las sumas. Invariante: numerador y denominador de la red son la
    suma exacta de los de las sedes incluidas.
    """
    included = tuple(i for i, agg in enumerate(aggregates) if agg.has_data)
    if not included:
        months = max((agg.months_reported for agg in aggregates), default=0)
        return Aggregate(None, None, months), ()
    numerator = sum(aggregates[i].numerator or 0.0 for i in included)
    denominator = sum(aggregates[i].denominator or 0.0 for i in included)
    months = max(aggregates[i].months_reported for i in included)
    weighted = sum((aggregates[i].denominator or 0.0) * aggregates[i].months_equivalent for i in included) / denominator
    return Aggregate(numerator, denominator, months, weighted_months=weighted), included


@dataclass(frozen=True)
class MonthlyContribution:
    """Aporte de un mes: numerador, denominador y valor del mes en la escala del indicador."""

    numerator: float | None
    denominator: float | None
    reported: bool | None
    value: float | None


def monthly_contribution(
    indicator: Indicator, series: SiteSeries, year: int, month: int, prevalence: float
) -> MonthlyContribution:
    """Aporte del mes para graficar barras mensuales.

    En indicadores que crecen con el tiempo el aporte es numerador del mes
    sobre el denominador vigente (los aportes suman el acumulado). En razones
    flujo sobre flujo es la razón del mes. En stocks es el valor del corte.
    """
    obs = series.get(year, month)
    if obs is None:
        return MonthlyContribution(None, None, None, None)
    if not obs.reported:
        return MonthlyContribution(None, None, False, None)
    numerator = obs.numerator
    if indicator.has_flow_denominator:
        denominator = obs.denominator
    else:
        denominator, _ = effective_denominator(indicator, series, year, month, prevalence)
        if indicator.aggregation is Aggregation.STOCK and numerator is None:
            denominator = None
    value = numerator / denominator if numerator is not None and denominator else None
    return MonthlyContribution(numerator, denominator, True, value)
