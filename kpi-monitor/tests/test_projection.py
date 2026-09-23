"""Proyección al cierre: horizonte, bootstrap determinista, percentiles, probabilidad y ritmo."""

from __future__ import annotations

import numpy as np
import pytest

from helpers import CONFIG, evaluate, indicator, monthly, rule, series
from kpi_monitor.domain.enums import Aggregation, DenominatorType, ProjectionMethod
from kpi_monitor.domain.models import Period
from kpi_monitor.domain.projection import ProjectionBasis, build_draws, combine, derive_seed

FIXED = indicator(denominator=DenominatorType.FIXED_SITE)


def _fixed_series(months: int, value: float = 100.0, site: str = "A") -> list:
    rows = monthly(2026, {m: (value + (m % 3) * 10, None) for m in range(1, months + 1)})
    return [series(site=site, rows=rows, targets={2026: 1200})]


@pytest.mark.parametrize("month", range(3, 12))
def test_horizon_is_months_left_until_december(month: int) -> None:
    """Regla: el horizonte de proyección es siempre 12 menos el mes de corte, sin un mínimo de meses fijo."""
    result = evaluate(FIXED, rule(value=0.9), _fixed_series(month), Period(2026, month)).network
    projection = result.projection
    assert projection.method is ProjectionMethod.BOOTSTRAP
    assert projection.horizon == 12 - month
    assert projection.months_projected + projection.months_observed == 12
    assert len(projection.path) == 12 - month
    assert [p.month for p in projection.path] == list(range(month + 1, 13))


def test_unreported_past_months_are_estimated_not_zero() -> None:
    """Regla: un mes pasado sin reporte se estima con el ritmo observado en vez de contar como cero en la
    proyección."""
    rows = monthly(2026, dict.fromkeys(range(1, 9), (100.0, None)))
    rows[(2026, 4)] = None
    data = [series(rows=rows, targets={2026: 1200})]
    projection = evaluate(FIXED, rule(value=0.9), data, Period(2026, 8)).sites[0].projection
    assert projection.months_observed == 7
    assert projection.months_projected == 5
    assert projection.value == pytest.approx(1200 / 1200)


def test_bootstrap_is_deterministic_and_percentiles_are_ordered() -> None:
    """Regla: la proyección usa un único bootstrap con semilla fija y reporta percentiles p10/p50/p90 verdaderos."""
    first = evaluate(FIXED, rule(value=0.9), _fixed_series(8), Period(2026, 8)).network.projection
    second = evaluate(FIXED, rule(value=0.9), _fixed_series(8), Period(2026, 8)).network.projection
    assert first == second
    assert first.p10 is not None
    assert first.p50 is not None
    assert first.p90 is not None
    assert first.p10 <= first.p50 <= first.p90
    assert all(p.p10 is not None and p.p90 is not None and p.p10 <= p.p90 for p in first.path)


def test_different_seed_changes_draws() -> None:
    """Con otra semilla el bootstrap produce simulaciones distintas: la semilla controla el resultado."""
    basis = ProjectionBasis(6, (10.0, 30.0, 20.0, 50.0, 5.0, 40.0), None, 155.0, 0.0)
    a = build_draws(basis, draws=300, min_months=3, seed=1)
    b = build_draws(basis, draws=300, min_months=3, seed=2)
    assert a is not None
    assert b is not None
    assert not np.array_equal(a.num_close, b.num_close)


def test_projected_counts_are_never_below_the_accumulated() -> None:
    """Regla: los conteos proyectados se acotan a valores no negativos y nunca caen bajo lo ya acumulado."""
    basis = ProjectionBasis(6, (0.0, 3.0, 0.0, 1.0, 2.0, 0.0), None, 6.0, 0.0)
    draws = build_draws(basis, draws=400, min_months=3, seed=3)
    assert draws is not None
    assert (draws.num_close >= basis.num_acc).all()
    assert (np.diff(draws.num_path, axis=1) >= 0).all()


def test_fewer_than_three_months_is_insufficient() -> None:
    """Regla: con menos de tres meses observados la proyección se informa como datos insuficientes, sin valor ni
    probabilidad."""
    projection = evaluate(FIXED, rule(value=0.9), _fixed_series(2), Period(2026, 2)).network.projection
    assert projection.method is ProjectionMethod.INSUFFICIENT
    assert projection.value is None
    assert projection.probability is None


def test_probability_reflects_distance_to_goal() -> None:
    """La probabilidad de cumplir es 100 % con un ritmo holgado sobre la meta y 0 % con uno muy por debajo."""
    high = evaluate(FIXED, rule(value=0.9), _fixed_series(8, value=150.0), Period(2026, 8)).network.projection
    low = evaluate(FIXED, rule(value=0.9), _fixed_series(8, value=40.0), Period(2026, 8)).network.projection
    assert high.probability == 1.0
    assert low.probability == 0.0
    assert high.compliance is not None
    assert high.compliance > 1


def test_required_rhythm_to_meet_the_goal() -> None:
    """Regla: el ritmo necesario es (meta anual × denominador - acumulado) / meses restantes."""
    rows = monthly(2026, dict.fromkeys(range(1, 7), (100.0, None)))
    data = [series(rows=rows, targets={2026: 1200})]
    projection = evaluate(FIXED, rule(value=0.9), data, Period(2026, 6)).network.projection
    assert projection.current_rhythm == pytest.approx(100.0)
    assert projection.required_rhythm == pytest.approx((0.9 * 1200 - 600) / 6)


def test_closed_year_uses_actual_value() -> None:
    """Con el año cerrado (corte en diciembre) no se proyecta: se usa el valor real con horizonte cero."""
    projection = evaluate(FIXED, rule(value=0.9), _fixed_series(12), Period(2026, 12)).network.projection
    assert projection.method is ProjectionMethod.CLOSED
    assert projection.horizon == 0


def test_stock_numerator_is_carried_forward() -> None:
    """Regla: un numerador de stock se proyecta arrastrando el último corte, sin bootstrap ni probabilidad."""
    ind = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.FIXED_SITE)
    data = [series(rows={(2026, 3): (30.0, None), (2026, 6): (36.0, None)}, targets={2026: 40})]
    projection = evaluate(ind, rule(value=0.9), data, Period(2026, 8)).network.projection
    assert projection.method is ProjectionMethod.CARRY_FORWARD
    assert projection.value == pytest.approx(0.9)
    assert projection.compliance == pytest.approx(1.0)
    assert projection.probability is None


def test_flow_over_flow_projects_numerator_and_denominator_together() -> None:
    """En un indicador de flujo sobre flujo, numerador y denominador se proyectan juntos; una razón estable se
    mantiene."""
    rows = monthly(2026, dict.fromkeys(range(1, 7), (8.0, 10.0)))
    projection = evaluate(indicator(), rule(value=0.8), [series(rows=rows)], Period(2026, 6)).network.projection
    assert projection.value == pytest.approx(0.8)
    assert projection.p10 == pytest.approx(0.8)
    assert projection.p90 == pytest.approx(0.8)


def test_network_projection_sums_site_draws() -> None:
    """Invariante: la proyección de la red suma las simulaciones y los acumulados de las sedes."""
    a = build_draws(ProjectionBasis(6, (1.0, 2.0, 3.0, 4.0, 5.0, 6.0), None, 21.0, 0.0), draws=50, min_months=3, seed=1)
    b = build_draws(ProjectionBasis(6, (10.0, 10.0, 10.0), None, 30.0, 0.0), draws=50, min_months=3, seed=2)
    assert a is not None
    assert b is not None
    total = combine([a, b])
    assert total is not None
    assert np.allclose(total.num_close, a.num_close + b.num_close)
    assert total.num_acc == 51.0


def test_derived_seed_is_stable() -> None:
    """La semilla derivada es estable para la misma combinación de indicador, sede y período, y cambia entre sedes."""
    assert derive_seed(1, "CG01", "NOR", 2026, 8) == derive_seed(1, "CG01", "NOR", 2026, 8)
    assert derive_seed(1, "CG01", "NOR", 2026, 8) != derive_seed(1, "CG01", "SUR", 2026, 8)


def test_draw_count_follows_configuration() -> None:
    """La cantidad de simulaciones del bootstrap sigue el parámetro configurado."""
    projection = evaluate(FIXED, rule(value=0.9), _fixed_series(6), Period(2026, 6)).network.projection
    assert projection.draws == CONFIG.draws
