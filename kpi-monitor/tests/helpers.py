"""Constructores compactos de casos de prueba."""

from __future__ import annotations

from collections.abc import Mapping

from kpi_monitor.domain.aggregation import SiteSeries
from kpi_monitor.domain.engine import EngineConfig, evaluate_indicator
from kpi_monitor.domain.enums import (
    Aggregation,
    DenominatorType,
    Direction,
    GoalRuleType,
    ProjectionMethod,
    Scale,
    Status,
)
from kpi_monitor.domain.goals import GoalResolution
from kpi_monitor.domain.models import (
    GoalBand,
    GoalRule,
    Indicator,
    IndicatorSheet,
    Observation,
    Period,
    Thresholds,
)
from kpi_monitor.domain.results import IndicatorEvaluation, IndicatorResult, Projection

CONFIG = EngineConfig(thresholds=Thresholds(), prevalence=0.22, draws=500, seed=7, min_months=3)
SHEET = IndicatorSheet("Qué mide", "Numerador", "Denominador", "Fuente", "Cero")
BANDS = (
    GoalBand(20.0, 1.0),
    GoalBand(30.0, 0.75),
    GoalBand(40.0, 0.5),
    GoalBand(50.0, 0.25),
    GoalBand(None, 0.0),
)
Cell = tuple[float | None, float | None] | None


def indicator(
    code: str = "T01",
    aggregation: Aggregation = Aggregation.FLOW,
    denominator: DenominatorType = DenominatorType.FLOW,
    direction: Direction = Direction.HIGHER_IS_BETTER,
    scale: Scale = Scale.PROPORTION,
    multiplier: float = 1.0,
    ceiling: float | None = None,
    stock_months: tuple[int, ...] = (),
    cut_months: tuple[int, ...] = (),
    program: str = "P1",
) -> Indicator:
    needs_stock = aggregation is Aggregation.STOCK or denominator in (DenominatorType.STOCK, DenominatorType.K_STOCK)
    return Indicator(
        code=code,
        program_code=program,
        name=f"Indicador {code}",
        short_name=code,
        aggregation=aggregation,
        denominator_type=denominator,
        direction=direction,
        scale=scale,
        sheet=SHEET,
        multiplier=multiplier,
        natural_ceiling=ceiling,
        stock_months=stock_months or ((3, 6, 9, 12) if needs_stock else ()),
        cut_months=cut_months,
    )


def rule(
    code: str = "T01",
    year: int = 2026,
    kind: GoalRuleType = GoalRuleType.ABSOLUTE,
    weight: float = 1.0,
    value: float | None = 0.8,
    factor: float | None = None,
    ceiling: float | None = None,
    bands: tuple[GoalBand, ...] = (),
) -> GoalRule:
    if kind is GoalRuleType.BANDS and not bands:
        bands = BANDS
    if kind is not GoalRuleType.ABSOLUTE:
        value = None
    return GoalRule(code, year, kind, weight, value=value, factor=factor, ceiling=ceiling, bands=bands)


def series(
    site: str = "A",
    rows: Mapping[tuple[int, int], Cell] | None = None,
    code: str = "T01",
    targets: Mapping[int, float] | None = None,
    populations: Mapping[int, int] | None = None,
) -> SiteSeries:
    """Serie de una sede. Una celda None es un mes marcado como no reportado."""
    observations = {}
    for (year, month), cell in (rows or {}).items():
        if cell is None:
            observations[(year, month)] = Observation(code, site, year, month, None, None, reported=False)
        else:
            observations[(year, month)] = Observation(code, site, year, month, cell[0], cell[1])
    return SiteSeries(site, observations, dict(targets or {}), dict(populations or {}))


def monthly(year: int, values: Mapping[int, Cell]) -> dict[tuple[int, int], Cell]:
    return {(year, month): cell for month, cell in values.items()}


def evaluate(
    ind: Indicator,
    goal_rule: GoalRule | None,
    site_series: list[SiteSeries],
    period: Period,
    config: EngineConfig = CONFIG,
) -> IndicatorEvaluation:
    names = {s.site_code: f"Sede {s.site_code}" for s in site_series}
    return evaluate_indicator(ind, goal_rule, site_series, names, period, config)


def result(
    status: Status,
    compliance: float | None,
    weight: float,
    program: str = "P1",
    site: str | None = None,
    projected: float | None = None,
    code: str = "T01",
) -> IndicatorResult:
    """Resultado mínimo para probar el índice ponderado."""
    return IndicatorResult(
        indicator=indicator(code=code, program=program),
        period=Period(2026, 8),
        site_code=site,
        site_name="Red" if site is None else site,
        numerator=None,
        denominator=None,
        months_reported=8,
        goal=GoalResolution(GoalRuleType.ABSOLUTE, 0.8, None, True),
        goal_to_date=0.8,
        compliance=compliance,
        status=status,
        weight=weight,
        projection=Projection(ProjectionMethod.BOOTSTRAP, compliance=projected),
    )
