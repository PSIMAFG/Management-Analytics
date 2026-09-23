"""Catálogo de ejemplo y generador sintético: estructura, calibración de casos de borde y determinismo."""

from __future__ import annotations

import random
from collections import Counter, defaultdict

import pytest

from kpi_monitor.data.catalog_seed import build_catalog
from kpi_monitor.data.synthetic import CURRENT_PERIOD, SEED, generate_observations
from kpi_monitor.domain.catalog import Catalog, validate_catalog
from kpi_monitor.domain.engine import PeriodEvaluation
from kpi_monitor.domain.enums import Aggregation, DenominatorType, GoalRuleType, Status
from kpi_monitor.domain.models import Observation


def test_catalog_has_three_programs_with_the_expected_sizes(catalog: Catalog) -> None:
    """El catálogo de ejemplo tiene 16, 5 y 3 indicadores en sus tres programas, cinco sedes genéricas y tres años."""
    sizes = Counter(ind.program_code for ind in catalog.indicators)
    assert sizes == {"CG": 16, "PC": 5, "IA": 3}
    assert len(catalog.sites) == 5
    assert all(site.name.startswith("Sede ") for site in catalog.sites)
    assert catalog.years == (2024, 2025, 2026)


def test_catalog_is_valid_and_weights_add_up_every_year(catalog: Catalog) -> None:
    """Regla: el catálogo de ejemplo es coherente y los pesos de cada programa suman 100 % en cada año."""
    assert validate_catalog(catalog) == []
    for year in catalog.years:
        for program in catalog.programs:
            weights = catalog.weights(year, program.code)
            if weights:
                assert sum(weights.values()) == pytest.approx(1.0)


def test_catalog_covers_every_goal_rule_type_in_the_current_year(catalog: Catalog) -> None:
    """El catálogo del año en curso incluye todos los tipos de meta: absoluta, aumento relativo, aumento con tope,
    tramos y línea base."""
    rules = [catalog.rule(ind.code, 2026) for ind in catalog.active_indicators(2026)]
    kinds = {r.rule_type for r in rules if r is not None}
    assert {
        GoalRuleType.ABSOLUTE,
        GoalRuleType.RELATIVE_INCREASE,
        GoalRuleType.CAPPED_INCREASE,
        GoalRuleType.BANDS,
        GoalRuleType.BASELINE,
    } <= kinds


def test_activity_index_uses_stock_and_population_with_four_cuts(catalog: Catalog) -> None:
    """El índice de actividad usa stock y población de referencia, con cortes en mayo, julio, septiembre y
    diciembre."""
    indicators = catalog.active_indicators(2026, "IA")
    assert {ind.denominator_type for ind in indicators} == {DenominatorType.POPULATION, DenominatorType.STOCK}
    assert all(ind.cut_months == (5, 7, 9, 12) for ind in indicators)
    assert all(catalog.population(site.code, 2026) for site in catalog.sites)


def test_multipliers_are_declared_only_on_k_stock_indicators(catalog: Catalog) -> None:
    """Los multiplicadores k solo se declaran en indicadores con denominador k × stock."""
    multipliers = {ind.code: ind.multiplier for ind in catalog.indicators if ind.multiplier != 1.0}
    assert sorted(multipliers.values()) == [8.0, 10.0, 12.0]
    assert all(catalog.indicator(code).denominator_type is DenominatorType.K_STOCK for code in multipliers)


def test_generator_is_deterministic_and_independent_of_global_random_state(catalog: Catalog) -> None:
    """Regla: el generador es determinista con su semilla y no depende del estado global del módulo random."""
    random.seed(1)
    first = generate_observations(catalog, SEED)
    random.seed(999)
    second = generate_observations(build_catalog(), SEED)
    assert first == second


def test_different_seed_gives_different_data(catalog: Catalog, observations: list[Observation]) -> None:
    """Con otra semilla cambian los valores pero no la estructura de las observaciones."""
    other = generate_observations(catalog, SEED + 1)
    assert [o.key for o in other] == [o.key for o in observations]
    assert other != observations


def test_three_years_with_the_current_year_up_to_the_cut(observations: list[Observation]) -> None:
    """Los datos cubren dos años completos y el año en curso hasta el mes de corte."""
    months_by_year = defaultdict(set)
    for obs in observations:
        months_by_year[obs.year].add(obs.month)
    assert set(months_by_year) == {2024, 2025, 2026}
    assert months_by_year[2024] == set(range(1, 13))
    assert months_by_year[2025] == set(range(1, 13))
    assert months_by_year[2026] == set(range(1, CURRENT_PERIOD.month + 1))


def test_stocks_are_declared_only_in_cut_months(catalog: Catalog, observations: list[Observation]) -> None:
    """Los stocks solo se informan en sus meses de corte."""
    for obs in observations:
        indicator = catalog.indicator(obs.indicator_code)
        if indicator.aggregation is Aggregation.STOCK:
            assert obs.month in indicator.stock_months
        if indicator.has_stock_denominator and obs.denominator is not None:
            assert obs.month in indicator.stock_months


def test_fixed_goal_indicators_only_have_data_for_sites_with_goals(
    catalog: Catalog, observations: list[Observation]
) -> None:
    """Los indicadores de meta fija solo tienen datos en sedes con meta asignada y no traen denominador."""
    for obs in observations:
        indicator = catalog.indicator(obs.indicator_code)
        if indicator.denominator_type is DenominatorType.FIXED_SITE:
            assert catalog.target(obs.indicator_code, obs.year, obs.site_code) is not None
            assert obs.denominator is None


def test_edge_cases_are_present(observations: list[Observation]) -> None:
    """Los datos sintéticos incluyen meses no reportados, ceros de subregistro, datos imposibles y una sede sin plan
    para un indicador."""
    unreported = [o for o in observations if not o.reported]
    assert unreported
    assert all(o.numerator is None and o.denominator is None for o in unreported)
    zeros = [o for o in observations if o.year == 2026 and o.numerator == 0 and (o.denominator or 0) > 0]
    assert zeros
    impossible = [o for o in observations if o.numerator and o.denominator and o.numerator > o.denominator]
    assert any(o.indicator_code == "CG02" for o in impossible)
    no_plan = [o for o in observations if o.indicator_code == "CG16" and o.site_code == "ORI"]
    assert no_plan == []


def test_seasonality_lowers_february_and_december(catalog: Catalog, observations: list[Observation]) -> None:
    """Los datos sintéticos reproducen la estacionalidad: febrero y diciembre tienen menos actividad que el
    promedio."""
    totals: Counter[int] = Counter()
    for obs in observations:
        indicator = catalog.indicator(obs.indicator_code)
        if obs.year == 2025 and indicator.has_flow_denominator and obs.denominator:
            totals[obs.month] += obs.denominator
    average = sum(totals.values()) / 12
    assert totals[2] < 0.8 * average
    assert totals[12] < 0.9 * average
    assert max(totals.values()) > average


def test_larger_sites_have_larger_volumes(catalog: Catalog, observations: list[Observation]) -> None:
    """Las sedes más grandes tienen volúmenes de actividad mayores."""
    volume: Counter[str] = Counter()
    for obs in observations:
        if obs.year == 2025 and catalog.indicator(obs.indicator_code).has_flow_denominator:
            volume[obs.site_code] += obs.denominator or 0
    assert volume["NOR"] > volume["PON"] > volume["SUR"]


def test_demo_period_shows_the_documented_situations(evaluation: PeriodEvaluation) -> None:
    """El período de demostración muestra las situaciones documentadas: línea base, meta no determinable, sin datos,
    no aplica, intensidad sobre 100 % y los tres colores."""
    network = {r.indicator.code: r for r in evaluation.results(None)}
    assert network["PC01"].status is Status.BASELINE
    assert evaluation.result("CG07", "PON").status is Status.UNDETERMINED
    assert evaluation.result("CG16", "ORI").status is Status.NO_DATA
    assert evaluation.result("CG04", "SUR").status is Status.NOT_APPLICABLE
    intensity = network["PC04"]
    assert intensity.value is not None
    assert intensity.value > 1.0
    counts = evaluation.status_counts(None)
    assert counts.green > 0
    assert counts.yellow > 0
    assert counts.red > 0
