"""Motor de evaluación: un único punto donde se calculan valor, meta, cumplimiento,
semáforo, proyección, cortes y red para cada indicador.

Funciones puras: reciben el catálogo, las observaciones y el período, y devuelven
resultados inmutables. No leen archivos ni la fecha del sistema.

Faltante no es cero: en los indicadores que crecen con el tiempo, la meta a la
fecha se prorratea por los meses que la sede informó (no por el mes calendario),
y la referencia del año anterior se anualiza con los meses informados de ese año.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from kpi_monitor.domain.aggregation import (
    Aggregate,
    MonthlyContribution,
    SiteSeries,
    aggregate_network,
    aggregate_site,
    monthly_contribution,
)
from kpi_monitor.domain.alerts import Alert, build_alerts
from kpi_monitor.domain.catalog import Catalog, critical_catalog_problems
from kpi_monitor.domain.compliance import classify, compute_compliance
from kpi_monitor.domain.enums import Aggregation, DenominatorType, GoalRuleType, ProjectionMethod, Status
from kpi_monitor.domain.goals import GoalResolution, goal_to_date, resolve_goal
from kpi_monitor.domain.index import WeightedIndex, weighted_index
from kpi_monitor.domain.models import GoalRule, Indicator, MonitorSettings, Observation, Period, Thresholds
from kpi_monitor.domain.progress import CancelCheck, ProgressFn
from kpi_monitor.domain.projection import (
    Draws,
    ProjectionBasis,
    build_draws,
    combine,
    derive_seed,
    summarize,
    with_static_denominator,
)
from kpi_monitor.domain.results import (
    NETWORK_LABEL,
    CutResult,
    IndicatorEvaluation,
    IndicatorResult,
    MonthlyPoint,
    Projection,
    StatusCounts,
)
from kpi_monitor.domain.text import join_names, month_name
from kpi_monitor.errors import OperationCancelledError, ValidationError


@dataclass(frozen=True)
class EngineConfig:
    """Parámetros del motor tomados de la configuración."""

    thresholds: Thresholds
    prevalence: float
    draws: int
    seed: int
    min_months: int

    @classmethod
    def from_settings(cls, settings: MonitorSettings) -> EngineConfig:
        return cls(
            thresholds=settings.thresholds,
            prevalence=settings.prevalence,
            draws=settings.bootstrap_draws,
            seed=settings.bootstrap_seed,
            min_months=settings.min_months_projection,
        )


@dataclass(frozen=True)
class _Level:
    """Datos de un nivel (sede o red) necesarios para armar su resultado."""

    site_code: str | None
    site_name: str
    applicable: bool
    aggregate: Aggregate
    monthly_aggregates: tuple[Aggregate, ...]
    contributions: tuple[MonthlyContribution, ...]
    goal: GoalResolution
    previous_same_period: float | None
    sites_included: tuple[str, ...]
    note: str = ""


def _goal_months(aggregate: Aggregate, calendar_month: int) -> float:
    """Meses con que se prorratea la meta a la fecha: los informados; sin ninguno, el mes calendario."""
    months = aggregate.months_equivalent
    return months if months > 0 else float(calendar_month)


def _annualized(indicator: Indicator, aggregate: Aggregate) -> float | None:
    """Valor del año completo; si el valor crece con el tiempo, se anualiza con los meses informados.

    Así un mes no informado del año no se lee como un mes con cero actividad.
    """
    value = aggregate.value
    if value is None or not indicator.grows_with_time:
        return value
    months = aggregate.months_equivalent
    return value * 12 / months if months > 0 else None


def _pending_note(indicator: Indicator) -> str:
    first = indicator.first_stock_month
    if first is None:
        return "Aún no corresponde informar datos del indicador en el año."
    return f"Aún no corresponde el primer corte del año ({month_name(first)}); el indicador se mide desde ese mes."


def _status_and_note(
    indicator: Indicator,
    rule: GoalRule | None,
    goal: GoalResolution,
    aggregate: Aggregate,
    applicable: bool,
    compliance: float | None,
    thresholds: Thresholds,
    network: bool,
    month: int,
) -> tuple[Status, str]:
    """Estado del semáforo al mes indicado, con la explicación de los estados especiales."""
    if not applicable:
        return Status.NOT_APPLICABLE, "La sede no tiene meta asignada para este indicador en el año."
    if rule is None:
        return Status.UNDETERMINED, "El indicador no tiene regla de meta para el año."
    if rule.rule_type is GoalRuleType.BASELINE:
        return Status.BASELINE, goal.reason
    if aggregate.numerator is None:
        if not indicator.expects_data_until(month):
            return Status.PENDING, _pending_note(indicator)
        if network:
            return Status.NO_DATA, "Ninguna sede tiene datos del indicador a la fecha."
        if indicator.aggregation is Aggregation.STOCK:
            return Status.NO_DATA, "No hay corte de stock informado en el año."
        return Status.NO_DATA, "La sede no ha informado datos del indicador en el año."
    if aggregate.denominator is None or aggregate.denominator <= 0:
        if indicator.denominator_type is DenominatorType.POPULATION:
            return Status.NO_DATA, "La sede no tiene población de referencia; el valor no se publica."
        return Status.NO_DATA, "Hay actividad informada pero falta el denominador."
    if not goal.determinable:
        return Status.UNDETERMINED, goal.reason
    status = classify(compliance, thresholds)
    assert status is not None
    return status, ""


def _series_by_site(
    catalog: Catalog, indicator: Indicator, observations: Iterable[Observation]
) -> dict[str, SiteSeries]:
    by_site: dict[str, dict[tuple[int, int], Observation]] = defaultdict(dict)
    for obs in observations:
        by_site[obs.site_code][(obs.year, obs.month)] = obs
    return {
        site.code: SiteSeries(
            site_code=site.code,
            observations=by_site.get(site.code, {}),
            targets=catalog.targets_for(indicator.code, site.code),
            populations=catalog.populations_for(site.code),
        )
        for site in catalog.ordered_sites()
    }


def _site_level(
    indicator: Indicator,
    rule: GoalRule | None,
    series: SiteSeries,
    site_name: str,
    period: Period,
    prevalence: float,
) -> _Level:
    year, month = period.year, period.month
    applicable = not (indicator.denominator_type is DenominatorType.FIXED_SITE and series.targets.get(year) is None)
    aggregate = aggregate_site(indicator, series, year, month, prevalence)
    monthly = tuple(aggregate_site(indicator, series, year, m, prevalence) for m in range(1, month + 1))
    contributions = tuple(monthly_contribution(indicator, series, year, m, prevalence) for m in range(1, 13))
    reference = _annualized(indicator, aggregate_site(indicator, series, year - 1, 12, prevalence))
    previous = aggregate_site(indicator, series, year - 1, month, prevalence).value
    return _Level(
        site_code=series.site_code,
        site_name=site_name,
        applicable=applicable,
        aggregate=aggregate,
        monthly_aggregates=monthly,
        contributions=contributions,
        goal=resolve_goal(rule, reference),
        previous_same_period=previous,
        sites_included=(series.site_code,) if aggregate.has_data else (),
    )


def _network_members(rule: GoalRule | None, sites: Sequence[_Level]) -> tuple[list[int], list[int]]:
    """Sedes que forman la red y sedes con dato que quedan fuera.

    En las metas de aumento sobre el año anterior, la referencia y el acumulado de
    la red se arman con el mismo conjunto de sedes: las que tienen dato y meta
    determinable. Una sede con referencia cero o faltante (meta no determinable)
    no entra, para que no vuelva a aparecer en la red el verde falso que se evita
    en la sede. Si ninguna sede tiene meta determinable, la red usa las sedes con
    dato y su meta tampoco es determinable.
    """
    applicable = [i for i, level in enumerate(sites) if level.applicable]
    if rule is None or not rule.rule_type.needs_reference:
        return applicable, []
    with_data = [i for i in applicable if sites[i].aggregate.has_data]
    valid = [i for i in with_data if sites[i].goal.determinable]
    if not valid:
        return with_data, []
    return valid, [i for i in with_data if i not in valid]


def _network_contribution(parts: Sequence[MonthlyContribution]) -> MonthlyContribution:
    """Aporte de la red en un mes: suma de las sedes que informaron el mes con un valor calculable."""
    valid = [p for p in parts if p.reported and p.value is not None and p.numerator is not None and p.denominator]
    if valid:
        numerator = sum(p.numerator or 0.0 for p in valid)
        denominator = sum(p.denominator or 0.0 for p in valid)
        return MonthlyContribution(numerator, denominator, True, numerator / denominator)
    reported = None if all(p.reported is None for p in parts) else False
    return MonthlyContribution(None, None, reported, None)


def _network_level(
    indicator: Indicator,
    rule: GoalRule | None,
    series: Sequence[SiteSeries],
    sites: Sequence[_Level],
    period: Period,
    prevalence: float,
) -> _Level:
    year, month = period.year, period.month
    members, excluded = _network_members(rule, sites)
    chosen = [sites[i] for i in members]
    chosen_series = [series[i] for i in members]
    aggregate, included = aggregate_network([s.aggregate for s in chosen])
    monthly = tuple(aggregate_network([s.monthly_aggregates[m - 1] for s in chosen])[0] for m in range(1, month + 1))
    contributions = tuple(_network_contribution([s.contributions[m - 1] for s in chosen]) for m in range(1, 13))
    reference = aggregate_network([aggregate_site(indicator, s, year - 1, 12, prevalence) for s in chosen_series])[0]
    previous = aggregate_network([aggregate_site(indicator, s, year - 1, month, prevalence) for s in chosen_series])[0]
    note = ""
    if excluded:
        names = join_names([sites[i].site_name for i in excluded])
        note = (
            f"La red no incluye a {names}: su meta no es determinable (valor del año anterior cero o faltante), "
            "así que la referencia y el acumulado de la red se calculan sin esas sedes."
        )
    return _Level(
        site_code=None,
        site_name=NETWORK_LABEL,
        applicable=True,
        aggregate=aggregate,
        monthly_aggregates=monthly,
        contributions=contributions,
        goal=resolve_goal(rule, _annualized(indicator, reference)),
        previous_same_period=previous.value,
        sites_included=tuple(chosen[i].site_code or "" for i in included),
        note=note,
    )


def _monthly_points(
    indicator: Indicator,
    rule: GoalRule | None,
    level: _Level,
    period: Period,
    thresholds: Thresholds,
) -> tuple[MonthlyPoint, ...]:
    """Serie de doce meses: acumulado y cumplimiento hasta el corte, meses pendientes después.

    Hasta el corte, la meta de cada mes se prorratea por los meses informados a esa
    fecha. Después del corte se usa el mes calendario, igual que la trayectoria
    proyectada, que ya estima los meses que faltan informar.
    """
    points: list[MonthlyPoint] = []
    annual = level.goal.value if level.goal.determinable else None
    for m in range(1, 13):
        contribution = level.contributions[m - 1]
        if m > period.month:
            target = goal_to_date(indicator, annual, m) if annual is not None else None
            points.append(
                MonthlyPoint(
                    m, indicator.expects_data_in(m), None, None, None, None, None, target, None, Status.PENDING
                )
            )
            continue
        aggregate = level.monthly_aggregates[m - 1]
        target = goal_to_date(indicator, annual, _goal_months(aggregate, m)) if annual is not None else None
        compliance = compute_compliance(indicator, rule, aggregate.value, target)
        status, _ = _status_and_note(
            indicator, rule, level.goal, aggregate, level.applicable, compliance, thresholds, level.site_code is None, m
        )
        points.append(
            MonthlyPoint(
                month=m,
                expected=indicator.expects_data_in(m),
                reported=contribution.reported,
                numerator=contribution.numerator,
                denominator=contribution.denominator,
                monthly_value=contribution.value,
                cumulative_value=aggregate.value,
                goal_to_date=target,
                compliance=compliance,
                status=status,
            )
        )
    return tuple(points)


def _cuts(indicator: Indicator, points: Sequence[MonthlyPoint], period: Period) -> tuple[CutResult, ...]:
    cuts = []
    for month in indicator.cut_months:
        if month > period.month:
            cuts.append(CutResult(month, Status.PENDING, None, None))
            continue
        point = points[month - 1]
        cuts.append(CutResult(month, point.status, point.cumulative_value, point.compliance))
    return tuple(cuts)


def _site_draws(
    indicator: Indicator, series: SiteSeries, aggregate: Aggregate, period: Period, config: EngineConfig
) -> Draws | None:
    """Simulaciones de cierre de una sede con numerador de flujo."""
    if indicator.aggregation is Aggregation.STOCK or not aggregate.has_data:
        return None
    rows = [
        obs
        for m in range(1, period.month + 1)
        if (obs := series.reported(period.year, m)) is not None and obs.numerator is not None
    ]
    if not rows:
        return None
    flow_den = indicator.has_flow_denominator
    basis = ProjectionBasis(
        month=period.month,
        numerators=tuple(float(obs.numerator or 0.0) for obs in rows),
        denominators=tuple(float(obs.denominator or 0.0) for obs in rows) if flow_den else None,
        num_acc=float(aggregate.numerator or 0.0),
        den_acc=float(aggregate.denominator or 0.0) if flow_den else 0.0,
    )
    seed = derive_seed(config.seed, indicator.code, series.site_code, period.year, period.month)
    draws = build_draws(basis, draws=config.draws, min_months=config.min_months, seed=seed)
    if draws is None or flow_den:
        return draws
    return with_static_denominator(draws, float(aggregate.denominator or 0.0))


def _static_projection(
    method: ProjectionMethod,
    indicator: Indicator,
    rule: GoalRule | None,
    level: _Level,
    thresholds: Thresholds,
    months_observed: int,
    horizon: int,
) -> Projection:
    """Proyección sin simulación: año cerrado o arrastre del último corte de stock.

    En un año cerrado con meses no informados, el valor se anualiza con los meses
    informados, igual que la meta a la fecha: el cumplimiento al cierre coincide
    con el cumplimiento a la fecha de diciembre.
    """
    value = _annualized(indicator, level.aggregate)
    annual = level.goal.value if level.goal.determinable else None
    compliance = compute_compliance(indicator, rule, value, annual)
    return Projection(
        method=method,
        value=value,
        p50=value,
        compliance=compliance,
        status=classify(compliance, thresholds),
        months_observed=months_observed,
        horizon=horizon,
    )


def _projection(
    indicator: Indicator,
    rule: GoalRule | None,
    level: _Level,
    draws: Draws | None,
    period: Period,
    config: EngineConfig,
) -> Projection:
    horizon = 12 - period.month
    months = level.aggregate.months_reported
    if not level.applicable or not level.aggregate.has_data:
        return Projection(ProjectionMethod.NONE, months_observed=months, horizon=horizon)
    if period.month == 12:
        return _static_projection(ProjectionMethod.CLOSED, indicator, rule, level, config.thresholds, months, 0)
    if indicator.aggregation is Aggregation.STOCK:
        return _static_projection(
            ProjectionMethod.CARRY_FORWARD, indicator, rule, level, config.thresholds, months, horizon
        )
    if draws is None:
        return Projection(ProjectionMethod.NONE, months_observed=months, horizon=horizon)
    return summarize(indicator, rule, level.goal, draws, config.thresholds)


def _result(
    indicator: Indicator,
    rule: GoalRule | None,
    level: _Level,
    projection: Projection,
    period: Period,
    thresholds: Thresholds,
) -> IndicatorResult:
    annual = level.goal.value if level.goal.determinable else None
    months = _goal_months(level.aggregate, period.month)
    target = goal_to_date(indicator, annual, months) if annual is not None else None
    compliance = compute_compliance(indicator, rule, level.aggregate.value, target) if level.applicable else None
    status, note = _status_and_note(
        indicator,
        rule,
        level.goal,
        level.aggregate,
        level.applicable,
        compliance,
        thresholds,
        level.site_code is None,
        period.month,
    )
    if not status.is_evaluated:
        compliance = None
    if level.note:
        note = f"{note} {level.note}".strip()
    points = _monthly_points(indicator, rule, level, period, thresholds)
    return IndicatorResult(
        indicator=indicator,
        period=period,
        site_code=level.site_code,
        site_name=level.site_name,
        numerator=level.aggregate.numerator,
        denominator=level.aggregate.denominator,
        months_reported=level.aggregate.months_reported,
        goal=level.goal,
        goal_to_date=target,
        compliance=compliance,
        status=status,
        weight=rule.weight if rule is not None else 0.0,
        projection=projection,
        monthly=points,
        cuts=_cuts(indicator, points, period),
        note=note,
        stock_month=level.aggregate.stock.month,
        stock_carried=level.aggregate.stock.carried,
        previous_same_period=level.previous_same_period,
        sites_included=level.sites_included,
        months_equivalent=months,
    )


def evaluate_indicator(
    indicator: Indicator,
    rule: GoalRule | None,
    series: Sequence[SiteSeries],
    site_names: Mapping[str, str],
    period: Period,
    config: EngineConfig,
) -> IndicatorEvaluation:
    """Evalúa un indicador en cada sede y en la red para el período."""
    levels = [_site_level(indicator, rule, s, site_names[s.site_code], period, config.prevalence) for s in series]
    network = _network_level(indicator, rule, series, levels, period, config.prevalence)

    site_results: list[IndicatorResult] = []
    included_draws: list[Draws] = []
    for level, site_series in zip(levels, series, strict=True):
        draws = _site_draws(indicator, site_series, level.aggregate, period, config) if level.applicable else None
        # La proyección de la red suma las simulaciones de las mismas sedes que forman su acumulado.
        if draws is not None and level.site_code in network.sites_included:
            included_draws.append(draws)
        projection = _projection(indicator, rule, level, draws, period, config)
        site_results.append(_result(indicator, rule, level, projection, period, config.thresholds))

    network_projection = _projection(indicator, rule, network, combine(included_draws), period, config)
    network_result = _result(indicator, rule, network, network_projection, period, config.thresholds)
    return IndicatorEvaluation(indicator=indicator, network=network_result, sites=tuple(site_results))


@dataclass(frozen=True)
class PeriodEvaluation:
    """Evaluación completa de un período: indicadores, índices y alertas."""

    period: Period
    catalog: Catalog
    thresholds: Thresholds
    evaluations: tuple[IndicatorEvaluation, ...]
    indexes: tuple[WeightedIndex, ...]
    alerts: tuple[Alert, ...]

    def evaluation(self, indicator_code: str) -> IndicatorEvaluation:
        for evaluation in self.evaluations:
            if evaluation.indicator.code == indicator_code:
                return evaluation
        raise ValidationError(f"El indicador {indicator_code} no está vigente en {self.period.year}.")

    def result(self, indicator_code: str, site_code: str | None) -> IndicatorResult:
        return self.evaluation(indicator_code).for_level(site_code)

    def results(self, site_code: str | None = None, program_code: str | None = None) -> list[IndicatorResult]:
        """Resultados de un nivel (None = red), opcionalmente filtrados por programa."""
        return [
            e.for_level(site_code)
            for e in self.evaluations
            if program_code is None or e.indicator.program_code == program_code
        ]

    def all_results(self) -> list[IndicatorResult]:
        return [r for e in self.evaluations for r in e.all_results]

    def index(self, program_code: str, site_code: str | None) -> WeightedIndex:
        for index in self.indexes:
            if index.program_code == program_code and index.site_code == site_code:
                return index
        raise ValidationError(f"El programa {program_code} no tiene indicadores vigentes en {self.period.year}.")

    def indexes_for(self, site_code: str | None) -> list[WeightedIndex]:
        return [i for i in self.indexes if i.site_code == site_code]

    def status_counts(self, site_code: str | None = None, program_code: str | None = None) -> StatusCounts:
        return StatusCounts.from_results(self.results(site_code, program_code))

    def alerts_for(
        self, program_code: str | None = None, site_codes: Collection[str | None] | None = None
    ) -> list[Alert]:
        """Alertas del período filtradas por programa y por niveles (None en la lista = red).

        Sin `site_codes` se devuelven las de todos los niveles. Las alertas que no
        apuntan a un indicador (sede sin reporte) se conservan al filtrar por programa.
        """
        chosen = []
        for alert in self.alerts:
            if site_codes is not None and alert.site_code not in site_codes:
                continue
            if (
                program_code is not None
                and alert.indicator_code is not None
                and self.catalog.indicator(alert.indicator_code).program_code != program_code
            ):
                continue
            chosen.append(alert)
        return chosen


def evaluate_period(
    catalog: Catalog,
    observations: Iterable[Observation],
    period: Period,
    config: EngineConfig,
    progress: ProgressFn | None = None,
    should_cancel: CancelCheck | None = None,
) -> PeriodEvaluation:
    """Evalúa todos los indicadores vigentes del período.

    Lanza ValidationError si el catálogo del año es incoherente (por ejemplo,
    pesos que no suman 100 %): es preferible no mostrar un índice a mostrar uno
    erróneo. Un indicador de meta fija por sede sin ninguna meta asignada ese
    año no cuenta como incoherencia aquí (ver `critical_catalog_problems`):
    queda "no aplica" en las sedes sin meta propia en vez de bloquear todo el
    período, lo que puede pasar legítimamente con datos reales incompletos.
    """
    problems = critical_catalog_problems(catalog, period.year)
    if problems:
        raise ValidationError("La configuración de indicadores tiene problemas:\n" + "\n".join(problems))
    indicators = catalog.active_indicators(period.year)
    if not indicators:
        raise ValidationError(f"No hay indicadores vigentes en {period.year}.")
    by_indicator: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        by_indicator[obs.indicator_code].append(obs)
    site_names = {s.code: s.name for s in catalog.sites}

    evaluations: list[IndicatorEvaluation] = []
    for position, indicator in enumerate(indicators, start=1):
        if should_cancel is not None and should_cancel():
            raise OperationCancelledError("El cálculo fue cancelado.")
        series = list(_series_by_site(catalog, indicator, by_indicator.get(indicator.code, [])).values())
        rule = catalog.rule(indicator.code, period.year)
        evaluations.append(evaluate_indicator(indicator, rule, series, site_names, period, config))
        if progress is not None:
            progress(int(position * 100 / len(indicators)), f"Evaluando {indicator.short_name}")

    results = [r for e in evaluations for r in e.all_results]
    programs = [p for p in catalog.ordered_programs() if catalog.active_indicators(period.year, p.code)]
    levels: list[str | None] = [None, *(s.code for s in catalog.ordered_sites())]
    indexes = tuple(weighted_index(results, p.code, level) for p in programs for level in levels)
    alerts = tuple(build_alerts(evaluations, period))
    return PeriodEvaluation(
        period=period,
        catalog=catalog,
        thresholds=config.thresholds,
        evaluations=tuple(evaluations),
        indexes=indexes,
        alerts=alerts,
    )
