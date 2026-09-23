"""Motor de evaluación: estados del semáforo, red, cortes, serie mensual y regresiones de errores conocidos."""

from __future__ import annotations

import pytest

from helpers import evaluate, indicator, monthly, rule, series
from kpi_monitor.domain.compliance import round_compliance
from kpi_monitor.domain.engine import PeriodEvaluation
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Direction, GoalRuleType, Scale, Status
from kpi_monitor.domain.models import Period

AUG = Period(2026, 8)
FIXED = indicator(denominator=DenominatorType.FIXED_SITE)


def _flat(year: int, num: float, den: float | None, months: int = 12) -> dict:
    return monthly(year, dict.fromkeys(range(1, months + 1), (num, den)))


def test_status_follows_thresholds_on_compliance_to_date() -> None:
    """Regla: el semáforo de cada sede se asigna aplicando los umbrales al cumplimiento a la fecha."""
    data = [
        series("A", _flat(2026, 8.0, 10.0, 8)),
        series("B", _flat(2026, 7.2, 10.0, 8)),
        series("C", _flat(2026, 6.0, 10.0, 8)),
    ]
    evaluation = evaluate(indicator(), rule(value=0.8), data, AUG)
    statuses = [r.status for r in evaluation.sites]
    assert statuses == [Status.GREEN, Status.YELLOW, Status.RED]
    assert [r.compliance for r in evaluation.sites] == pytest.approx([1.0, 0.9, 0.75])


def test_fixed_goal_is_prorated_to_date_and_compared_with_sum_of_months() -> None:
    """Regla: con meta fija por sede, la suma de los meses se divide por la meta fija y la meta anual se prorratea
    por mes/12."""
    rows = monthly(2026, dict.fromkeys(range(1, 7), (90.0, None)))
    result = evaluate(FIXED, rule(value=0.9), [series(rows=rows, targets={2026: 1200})], Period(2026, 6)).sites[0]
    assert result.value == pytest.approx(540 / 1200)
    assert result.goal_to_date == pytest.approx(0.9 * 6 / 12)
    assert result.compliance == pytest.approx(1.0)
    assert result.status is Status.GREEN


def test_network_is_sum_of_sites_and_not_the_worst_site() -> None:
    """Regla: la red suma numeradores y denominadores de las sedes; nunca copia el semáforo de la peor sede."""
    data = [
        series("A", _flat(2026, 95.0, 100.0, 8)),
        series("B", _flat(2026, 2.0, 10.0, 8)),
    ]
    evaluation = evaluate(indicator(), rule(value=0.8), data, AUG)
    network = evaluation.network
    assert evaluation.sites[1].status is Status.RED
    assert network.numerator == pytest.approx(sum(r.numerator or 0 for r in evaluation.sites))
    assert network.denominator == pytest.approx(sum(r.denominator or 0 for r in evaluation.sites))
    assert network.value == pytest.approx((95 * 8 + 2 * 8) / (100 * 8 + 10 * 8))
    assert network.status is Status.GREEN
    assert network.sites_included == ("A", "B")


def test_network_fixed_goal_is_the_sum_of_site_goals() -> None:
    """Regla: la meta fija de la red es la suma de las metas fijas de las sedes."""
    data = [
        series("A", monthly(2026, {1: (50.0, None)}), targets={2026: 600}),
        series("B", monthly(2026, {1: (25.0, None)}), targets={2026: 300}),
    ]
    network = evaluate(FIXED, rule(value=0.9), data, Period(2026, 1)).network
    assert network.denominator == 900
    assert network.value == pytest.approx(75 / 900)


def test_site_without_fixed_goal_does_not_apply() -> None:
    """Regla: una sede sin meta fija asignada queda como no aplica y no suma denominador en la red."""
    data = [
        series("A", monthly(2026, {1: (50.0, None)}), targets={2026: 600}),
        series("B", monthly(2026, {1: (25.0, None)})),
    ]
    evaluation = evaluate(FIXED, rule(value=0.9), data, Period(2026, 1))
    assert evaluation.sites[1].status is Status.NOT_APPLICABLE
    assert evaluation.sites[1].compliance is None
    assert evaluation.network.denominator == 600


def test_zero_activity_with_known_fixed_goal_is_zero_percent_and_red() -> None:
    """Regresión: una sede sin actividad pero con meta conocida debe verse en 0 %, no desaparecer."""
    rows = monthly(2026, dict.fromkeys(range(1, 9), (0.0, None)))
    result = evaluate(FIXED, rule(value=0.9), [series(rows=rows, targets={2026: 1200})], AUG).sites[0]
    assert result.value == 0.0
    assert result.compliance == 0.0
    assert result.status is Status.RED


def test_site_without_reports_has_no_data_status() -> None:
    """Regla: una sede sin observaciones reportadas queda sin datos y no entra en la suma de la red."""
    evaluation = evaluate(indicator(), rule(), [series("A", {}), series("B", _flat(2026, 8.0, 10.0, 8))], AUG)
    assert evaluation.sites[0].status is Status.NO_DATA
    assert evaluation.sites[0].compliance is None
    assert evaluation.network.sites_included == ("B",)


def test_zero_reference_makes_relative_goal_undetermined() -> None:
    """Regresión: una referencia del año anterior en cero no produce una meta cero ni un verde falso."""
    data = [series("A", {**_flat(2025, 0.0, 100.0), **_flat(2026, 5.0, 100.0, 8)})]
    result = evaluate(indicator(), rule(kind=GoalRuleType.RELATIVE_INCREASE, factor=0.2), data, AUG).sites[0]
    assert result.status is Status.UNDETERMINED
    assert result.goal.determinable is False
    assert result.compliance is None
    assert result.goal.reason


def test_missing_reference_makes_capped_goal_undetermined() -> None:
    """Regla: sin datos del año anterior, la meta de aumento con tope no es determinable."""
    data = [series("A", _flat(2026, 5.0, 100.0, 8))]
    kind = GoalRuleType.CAPPED_INCREASE
    result = evaluate(indicator(), rule(kind=kind, factor=0.2, ceiling=0.9), data, AUG).sites[0]
    assert result.status is Status.UNDETERMINED


def test_relative_goal_uses_full_previous_year_at_each_level() -> None:
    """Regla: la meta de aumento relativo usa el acumulado del año anterior completo de cada nivel; la red usa su
    propia referencia sumada."""
    data = [
        series("A", {**_flat(2025, 10.0, 100.0), **_flat(2026, 12.0, 100.0, 8)}),
        series("B", {**_flat(2025, 30.0, 100.0), **_flat(2026, 30.0, 100.0, 8)}),
    ]
    evaluation = evaluate(indicator(), rule(kind=GoalRuleType.RELATIVE_INCREASE, factor=0.2), data, AUG)
    assert evaluation.sites[0].goal.value == pytest.approx(0.10 * 1.2)
    assert evaluation.sites[1].goal.value == pytest.approx(0.30 * 1.2)
    assert evaluation.network.goal.reference == pytest.approx(0.20)
    assert evaluation.network.goal.value == pytest.approx(0.20 * 1.2)
    assert evaluation.sites[0].status is Status.GREEN
    assert evaluation.sites[1].status is Status.RED


def test_capped_goal_never_exceeds_the_cap_for_a_site_already_above_it() -> None:
    """Regresión: la meta de aumento con tope es min(tope, referencia × (1 + f)) en todas las vistas."""
    ind = indicator(denominator=DenominatorType.STOCK, scale=Scale.RATE)
    rows = {**_flat(2025, 100.0, 120.0), **_flat(2026, 100.0, 120.0, 8)}
    kind = GoalRuleType.CAPPED_INCREASE
    result = evaluate(ind, rule(kind=kind, factor=0.2, ceiling=9.0), [series(rows=rows)], AUG).sites[0]
    assert result.goal.reference == pytest.approx(1200 / 120)
    assert result.goal.value == 9.0
    assert result.goal_to_date == pytest.approx(9.0 * 8 / 12)


def test_multiplier_is_applied_once_also_in_the_previous_year_reference() -> None:
    """Regla: el multiplicador k se aplica una sola vez también al calcular la referencia del año anterior."""
    ind = indicator(denominator=DenominatorType.K_STOCK, multiplier=7.0)
    rows = {**_flat(2025, 14.0, 20.0), **_flat(2026, 14.0, 20.0, 8)}
    kind = GoalRuleType.RELATIVE_INCREASE
    result = evaluate(ind, rule(kind=kind, factor=0.1), [series(rows=rows)], AUG).sites[0]
    assert result.denominator == pytest.approx(7 * 20)
    assert result.goal.reference == pytest.approx(14 * 12 / (7 * 20))


def test_stock_numerator_is_evaluated_at_the_latest_cut_without_prorating() -> None:
    """Regla: un numerador de stock se evalúa en el último corte contra la meta anual completa, sin prorrateo."""
    ind = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.FIXED_SITE)
    rows = {(2026, 3): (20.0, None), (2026, 6): (27.0, None)}
    result = evaluate(ind, rule(value=0.9), [series(rows=rows, targets={2026: 30})], AUG).sites[0]
    assert result.numerator == 27.0
    assert result.stock_month == 6
    assert result.goal_to_date == pytest.approx(0.9)
    assert result.compliance == pytest.approx(1.0)


def test_stock_denominator_projection_does_not_sum_the_stock() -> None:
    """Regresión: la proyección divide por el stock vigente, no por el stock repetido cada mes."""
    ind = indicator(denominator=DenominatorType.STOCK, scale=Scale.RATE)
    rows = monthly(2026, dict.fromkeys(range(1, 9), (50.0, None)))
    rows[(2026, 3)] = (50.0, 100.0)
    rows[(2026, 6)] = (50.0, 100.0)
    result = evaluate(ind, rule(value=6.0), [series(rows=rows)], AUG).sites[0]
    assert result.denominator == 100.0
    assert result.projection.value == pytest.approx(600 / 100)
    assert result.projection.compliance == pytest.approx(1.0)


def test_projection_is_not_capped_at_the_goal() -> None:
    """Regresión: el sobrecumplimiento proyectado se muestra tal cual, sin topear en la meta."""
    rows = monthly(2026, dict.fromkeys(range(1, 9), (150.0, None)))
    result = evaluate(FIXED, rule(value=0.9), [series(rows=rows, targets={2026: 1200})], AUG).sites[0]
    assert result.projection.value == pytest.approx(1800 / 1200)
    assert result.projection.compliance is not None
    assert result.projection.compliance > 1.6


def test_previous_year_data_never_fills_missing_current_months() -> None:
    """Regresión: sin datos del año en curso no se usan los del año anterior."""
    rows = {**_flat(2025, 9.0, 10.0), (2026, 1): None, (2026, 2): None}
    result = evaluate(indicator(), rule(), [series(rows=rows)], Period(2026, 2)).sites[0]
    assert result.numerator is None
    assert result.status is Status.NO_DATA
    assert result.months_reported == 0


def test_previous_same_period_compares_the_same_months() -> None:
    """Regla: la variación interanual compara el mismo tramo de meses del año anterior, no el año anterior completo."""
    rows = {**monthly(2025, {m: (float(m), 10.0) for m in range(1, 13)}), **_flat(2026, 6.0, 10.0, 3)}
    result = evaluate(indicator(), rule(), [series(rows=rows)], Period(2026, 3)).sites[0]
    assert result.previous_same_period == pytest.approx((1 + 2 + 3) / 30)
    assert result.goal.reference == pytest.approx(78 / 120)
    assert result.change_vs_previous_year == pytest.approx(0.6 / 0.2 - 1)


def test_population_denominator_without_population_is_not_published() -> None:
    """Regla: sin población de referencia para una sede no se publica el indicador de esa sede (queda sin datos)."""
    ind = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.POPULATION)
    data = [
        series("A", {(2026, 6): (500.0, None)}, populations={2026: 10000}),
        series("B", {(2026, 6): (400.0, None)}),
    ]
    evaluation = evaluate(ind, rule(value=0.2), data, AUG)
    assert evaluation.sites[0].value == pytest.approx(500 / 2200)
    assert evaluation.sites[1].status is Status.NO_DATA
    assert evaluation.sites[1].value is None
    assert evaluation.network.sites_included == ("A",)


def test_baseline_rule_has_no_traffic_light() -> None:
    """Regla: una meta de línea base publica el valor sin cumplimiento ni color de semáforo."""
    result = evaluate(indicator(), rule(kind=GoalRuleType.BASELINE), [series(rows=_flat(2026, 1, 2, 8))], AUG)
    assert result.network.status is Status.BASELINE
    assert result.network.compliance is None
    assert result.network.value == pytest.approx(0.5)


def test_lower_is_better_uses_bands_on_unrounded_average() -> None:
    """Regla: en tiempo de espera (menor es mejor) los tramos se aplican al promedio sin redondear; 20,1 días ya cae
    en el tramo de 75 %."""
    ind = indicator(direction=Direction.LOWER_IS_BETTER, scale=Scale.DAYS)
    data = [series("A", _flat(2026, 201.0, 10.0, 8)), series("B", _flat(2026, 150.0, 10.0, 8))]
    evaluation = evaluate(ind, rule(kind=GoalRuleType.BANDS), data, AUG)
    assert evaluation.sites[0].value == pytest.approx(20.1)
    assert evaluation.sites[0].compliance == 0.75
    assert evaluation.sites[0].status is Status.RED
    assert evaluation.sites[1].compliance == 1.0


def test_future_cuts_are_pending_and_past_cuts_are_evaluated() -> None:
    """Regla: un corte posterior al mes de corte queda pendiente, nunca se evalúa con datos congelados."""
    ind = indicator(cut_months=(5, 7, 9, 12))
    result = evaluate(ind, rule(), [series(rows=_flat(2026, 8.0, 10.0, 8))], AUG).network
    assert [c.month for c in result.cuts] == [5, 7, 9, 12]
    assert [c.status for c in result.cuts] == [Status.GREEN, Status.GREEN, Status.PENDING, Status.PENDING]
    assert result.cuts[2].value is None


def test_monthly_series_has_twelve_points_and_pending_months_after_the_cut() -> None:
    """La serie mensual tiene doce puntos: un mes faltante no aporta pero conserva el acumulado, y los meses
    posteriores al corte quedan pendientes."""
    rows = monthly(2026, {1: (8.0, 10.0), 2: None, 3: (6.0, 10.0)})
    result = evaluate(indicator(), rule(), [series(rows=rows)], Period(2026, 3)).sites[0]
    assert [p.month for p in result.monthly] == list(range(1, 13))
    assert [p.reported for p in result.monthly[:3]] == [True, False, True]
    assert result.monthly[1].monthly_value is None
    assert result.monthly[1].cumulative_value == pytest.approx(0.8)
    assert result.monthly[2].cumulative_value == pytest.approx(14 / 20)
    assert all(p.status is Status.PENDING for p in result.monthly[3:])
    assert all(p.goal_to_date == pytest.approx(0.8) for p in result.monthly)


def test_monthly_contributions_add_up_to_the_cumulative_value_when_goal_is_fixed() -> None:
    """Con meta fija, los aportes mensuales suman el valor acumulado y la meta a la fecha crece en doceavos."""
    rows = monthly(2026, {1: (100.0, None), 2: (80.0, None), 3: (120.0, None)})
    result = evaluate(FIXED, rule(value=0.9), [series(rows=rows, targets={2026: 1200})], Period(2026, 3)).sites[0]
    total = sum(p.monthly_value or 0 for p in result.monthly)
    assert total == pytest.approx(result.value)
    assert [p.goal_to_date for p in result.monthly[:3]] == pytest.approx([0.075, 0.15, 0.225])


def test_network_invariant_on_synthetic_data(evaluation: PeriodEvaluation) -> None:
    """Invariante: numerador y denominador de la red son la suma de las sedes incluidas, en cada indicador."""
    for item in evaluation.evaluations:
        network = item.network
        included = [r for r in item.sites if r.site_code in network.sites_included]
        if not included:
            assert network.numerator is None
            continue
        assert network.numerator == pytest.approx(sum(r.numerator or 0 for r in included))
        assert network.denominator == pytest.approx(sum(r.denominator or 0 for r in included))
        excluded = [r for r in item.sites if r.site_code not in network.sites_included]
        # Una sede queda fuera de la red sin dato, o con dato pero meta no determinable (referencia del
        # año anterior cero o faltante): la red no arma su acumulado ni su referencia con esa sede.
        assert all(r.value is None or not r.goal.determinable for r in excluded)


def test_projection_horizon_plus_observed_months_is_twelve_on_synthetic_data(evaluation: PeriodEvaluation) -> None:
    """Regla: en los datos sintéticos, el horizonte de proyección es 12 menos el mes de corte y la trayectoria
    termina en diciembre."""
    for result in evaluation.all_results():
        projection = result.projection
        assert projection.horizon == 12 - evaluation.period.month
        if projection.path:
            assert len(projection.path) + evaluation.period.month == 12
            assert projection.path[-1].month == 12


def test_synthetic_evaluation_has_every_kind_of_status(evaluation: PeriodEvaluation) -> None:
    """Los datos sintéticos producen todos los estados: verde, amarillo, rojo, sin datos, no determinable, línea
    base y no aplica."""
    statuses = {r.status for r in evaluation.all_results()}
    for status in (
        Status.GREEN,
        Status.YELLOW,
        Status.RED,
        Status.NO_DATA,
        Status.UNDETERMINED,
        Status.BASELINE,
        Status.NOT_APPLICABLE,
    ):
        assert status in statuses, status


def test_every_view_uses_the_same_effective_goal(evaluation: PeriodEvaluation) -> None:
    """Regresión: la meta de la red, del semáforo, de la proyección y de la serie mensual es la misma."""
    for result in evaluation.all_results():
        if not result.goal.determinable:
            continue
        annual = result.goal.value
        assert annual is not None
        last_point = result.monthly[-1]
        assert last_point.goal_to_date == pytest.approx(annual)
        projection = result.projection
        ratio_rule = result.goal.rule_type is not GoalRuleType.BANDS
        higher = result.indicator.direction is Direction.HIGHER_IS_BETTER
        if projection.compliance is not None and projection.value is not None and ratio_rule and higher:
            # El cumplimiento se publica redondeado a 0,1 punto porcentual (compliance.round_compliance).
            assert projection.compliance == round_compliance(projection.value / annual)


def test_period_evaluation_lookups(evaluation: PeriodEvaluation) -> None:
    """La evaluación del período permite buscar resultados por indicador, sede y programa; un código no vigente da
    error."""
    assert len(evaluation.evaluations) == 24
    assert evaluation.result("CG01", None).is_network
    assert evaluation.result("CG01", "NOR").site_name == "Sede Norte"
    assert {r.program_code for r in evaluation.results(None, "IA")} == {"IA"}
    assert len(evaluation.results("SUR")) == 24
    with pytest.raises(Exception, match="no está vigente"):
        evaluation.evaluation("XX99")


def test_reported_zero_months_count_in_the_monthly_rhythm() -> None:
    """Regresión: el ritmo promedio divide por los meses informados, incluidos los informados con cero."""
    rows = monthly(2026, {1: (100.0, None), 2: (0.0, None), 3: (100.0, None), 4: (0.0, None)})
    result = evaluate(FIXED, rule(value=0.9), [series(rows=rows, targets={2026: 1200})], Period(2026, 4)).sites[0]
    assert result.projection.current_rhythm == pytest.approx(50.0)
    assert result.projection.value == pytest.approx((200 + 50 * 8) / 1200)
