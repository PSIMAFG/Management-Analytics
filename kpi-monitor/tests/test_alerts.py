"""Alertas: riesgo, falta de reporte, subregistro, dato imposible, meta no determinable y novedad por período."""

from __future__ import annotations

from dataclasses import replace

from helpers import evaluate, indicator, monthly, rule, series
from kpi_monitor.domain.alerts import Alert, build_alerts, mark_new, sort_alerts
from kpi_monitor.domain.engine import PeriodEvaluation
from kpi_monitor.domain.enums import AlertKind, DenominatorType, GoalRuleType, Severity
from kpi_monitor.domain.models import Period
from kpi_monitor.domain.results import IndicatorEvaluation

AUG = Period(2026, 8)


def _flat(num: float, den: float | None, months: int = 8, year: int = 2026) -> dict:
    return monthly(year, dict.fromkeys(range(1, months + 1), (num, den)))


def _alerts(*evaluations: IndicatorEvaluation, period: Period = AUG) -> list[Alert]:
    return build_alerts(list(evaluations), period)


def _kinds(alerts: list[Alert], site: str | None = "A") -> set[AlertKind]:
    return {a.kind for a in alerts if a.site_code == site}


def test_red_status_raises_a_critical_risk_alert() -> None:
    """Regla: un indicador en rojo genera una alerta crítica de riesgo con el cumplimiento en formato chileno."""
    evaluation = evaluate(indicator(), rule(value=0.8), [series("A", _flat(5.0, 10.0))], AUG)
    alerts = [a for a in _alerts(evaluation) if a.kind is AlertKind.AT_RISK and a.site_code == "A"]
    assert len(alerts) == 1
    assert alerts[0].severity is Severity.CRITICAL
    assert "62,5 %" in alerts[0].message


def test_projection_below_goal_raises_a_warning_even_if_status_is_not_red() -> None:
    """Regla: si la proyección al cierre queda bajo la meta se alerta como advertencia, aunque el semáforo actual no
    esté en rojo."""
    ind = indicator(denominator=DenominatorType.FIXED_SITE)
    rows = _flat(88.0, None)
    evaluation = evaluate(ind, rule(value=0.9), [series("A", rows, targets={2026: 1200})], AUG)
    site = evaluation.sites[0]
    assert site.status.is_evaluated
    assert site.projection.compliance is not None
    assert site.projection.compliance < 1
    alerts = [a for a in _alerts(evaluation) if a.kind is AlertKind.AT_RISK and a.site_code == "A"]
    assert [a.severity for a in alerts] == [Severity.WARNING]


def test_green_with_projection_on_goal_has_no_risk_alert() -> None:
    """Un indicador en verde cuya proyección cumple la meta no genera alerta de riesgo."""
    evaluation = evaluate(indicator(), rule(value=0.8), [series("A", _flat(9.0, 10.0))], AUG)
    assert AlertKind.AT_RISK not in _kinds(_alerts(evaluation))


def test_missing_cut_month_raises_no_report_alert_per_indicator() -> None:
    """Regla: una sede sin dato en el mes de corte genera una alerta de falta de reporte por indicador, con el mes
    en el mensaje."""
    rows = _flat(9.0, 10.0)
    rows[(2026, 8)] = None
    first = evaluate(indicator("T01"), rule("T01"), [series("A", rows), series("B", _flat(9.0, 10.0))], AUG)
    other = evaluate(indicator("T02"), rule("T02"), [series("A", _flat(9.0, 10.0), code="T02")], AUG)
    alerts = [a for a in _alerts(first, other) if a.kind is AlertKind.NO_REPORT]
    assert [(a.indicator_code, a.site_code) for a in alerts] == [("T01", "A")]
    assert "agosto 2026" in alerts[0].message


def test_site_missing_every_indicator_gets_a_single_site_alert() -> None:
    """Si una sede no informó ningún indicador en el mes de corte se genera una sola alerta de sede, no una por
    indicador."""
    rows = _flat(9.0, 10.0)
    rows[(2026, 8)] = None
    first = evaluate(indicator("T01"), rule("T01"), [series("A", rows)], AUG)
    second = evaluate(indicator("T02"), rule("T02"), [series("A", rows, code="T02")], AUG)
    alerts = [a for a in _alerts(first, second) if a.kind is AlertKind.NO_REPORT]
    assert len(alerts) == 1
    assert alerts[0].indicator_code is None
    assert alerts[0].site_code == "A"


def test_stock_indicator_is_not_expected_outside_its_cut_months() -> None:
    """Regla: un indicador de stock solo se espera en sus meses de corte; fuera de ellos no hay alerta de falta de
    reporte."""
    ind = indicator(denominator=DenominatorType.STOCK)
    rows = _flat(10.0, None)
    rows[(2026, 6)] = (10.0, 50.0)
    evaluation = evaluate(ind, rule(value=1.0), [series("A", rows)], Period(2026, 8))
    assert AlertKind.NO_REPORT not in _kinds(_alerts(evaluation))


def test_reported_zero_with_positive_denominator_is_possible_underreporting() -> None:
    """Regla: un numerador cero con denominador positivo se marca como posible subregistro e indica el mes."""
    rows = _flat(40.0, 50.0)
    rows[(2026, 5)] = (0.0, 50.0)
    evaluation = evaluate(indicator(), rule(), [series("A", rows)], AUG)
    alerts = [a for a in _alerts(evaluation) if a.kind is AlertKind.UNDERREPORTING]
    assert len(alerts) == 1
    assert "mayo" in alerts[0].message


def test_zero_with_tiny_expected_volume_is_not_flagged() -> None:
    """Un cero en una sede con volumen esperado muy bajo es plausible y no se marca como subregistro."""
    rows = _flat(1.0, 2.0)
    rows[(2026, 5)] = (0.0, 2.0)
    evaluation = evaluate(indicator(), rule(), [series("A", rows)], AUG)
    assert AlertKind.UNDERREPORTING not in _kinds(_alerts(evaluation))


def test_value_over_natural_ceiling_is_an_impossible_value() -> None:
    """Regla: un valor sobre el techo natural (por ejemplo, una cobertura sobre 100 %) es un dato imposible y genera
    una alerta crítica."""
    rows = _flat(8.0, 10.0)
    rows[(2026, 4)] = (14.0, 10.0)
    evaluation = evaluate(indicator(ceiling=1.0), rule(), [series("A", rows)], AUG)
    alerts = [a for a in _alerts(evaluation) if a.kind is AlertKind.IMPOSSIBLE_VALUE and a.site_code == "A"]
    assert len(alerts) == 1
    assert alerts[0].severity is Severity.CRITICAL
    assert "abril" in alerts[0].message


def test_intensity_indicator_without_ceiling_may_exceed_one_hundred_percent() -> None:
    """Regla: un indicador de intensidad sin techo natural puede superar el 100 % sin que se marque como dato
    imposible."""
    evaluation = evaluate(indicator(), rule(value=1.0), [series("A", _flat(15.0, 10.0))], AUG)
    assert evaluation.sites[0].compliance is not None
    assert evaluation.sites[0].compliance > 1
    assert AlertKind.IMPOSSIBLE_VALUE not in _kinds(_alerts(evaluation))


def test_undetermined_goal_raises_an_informative_alert() -> None:
    """Regla: una meta no determinable (referencia del año anterior en cero) genera una alerta informativa en la
    sede y en la red."""
    rows = {**_flat(0.0, 10.0, 12, 2025), **_flat(3.0, 10.0)}
    evaluation = evaluate(indicator(), rule(kind=GoalRuleType.RELATIVE_INCREASE, factor=0.2), [series("A", rows)], AUG)
    alerts = [a for a in _alerts(evaluation) if a.kind is AlertKind.UNDETERMINED_GOAL]
    assert {a.site_code for a in alerts} == {"A", None}
    assert all(a.severity is Severity.INFO for a in alerts)


def test_activity_without_denominator_is_flagged() -> None:
    """Regla: la actividad informada sin denominador disponible (stock sin corte) se alerta como denominador
    faltante."""
    ind = indicator(denominator=DenominatorType.STOCK)
    evaluation = evaluate(ind, rule(value=1.0), [series("A", _flat(10.0, None, 2))], Period(2026, 2))
    assert AlertKind.MISSING_DENOMINATOR in _kinds(_alerts(evaluation, period=Period(2026, 2)))


def test_not_applicable_sites_have_no_alerts() -> None:
    """Las sedes donde el indicador no aplica (sin meta asignada) no generan alertas."""
    ind = indicator(denominator=DenominatorType.FIXED_SITE)
    data = [series("A", _flat(10.0, None), targets={2026: 1200}), series("B", _flat(0.0, None))]
    alerts = _alerts(evaluate(ind, rule(value=0.9), data, AUG))
    assert not [a for a in alerts if a.site_code == "B"]


def test_alerts_are_new_only_if_their_key_did_not_exist_in_the_previous_period() -> None:
    """Regla: una alerta es "nueva" según el período de datos anterior, no según cuántas veces se recalculó."""
    evaluation = evaluate(indicator(), rule(value=0.8), [series("A", _flat(5.0, 10.0))], AUG)
    current = _alerts(evaluation)
    previous = [replace(current[0], period=Period(2026, 7))]
    marked = mark_new(current, previous)
    assert marked[0].is_new is False
    assert all(a.is_new for a in marked[1:])
    assert mark_new(current, previous) == marked


def test_alerts_are_sorted_by_severity_first() -> None:
    """Las alertas se ordenan primero por gravedad, de la más crítica a la informativa."""
    evaluation = evaluate(indicator(), rule(value=0.8), [series("A", _flat(5.0, 10.0))], AUG)
    alerts = sort_alerts(reversed(_alerts(evaluation)))
    ranks = [a.severity.rank for a in alerts]
    assert ranks == sorted(ranks)


def test_synthetic_alerts_cover_the_edge_cases_and_keys_are_unique(evaluation: PeriodEvaluation) -> None:
    """Los datos sintéticos producen cada tipo de alerta y la clave indicador-sede-período de cada alerta es única."""
    kinds = {a.kind for a in evaluation.alerts}
    for kind in (
        AlertKind.AT_RISK,
        AlertKind.NO_REPORT,
        AlertKind.UNDERREPORTING,
        AlertKind.IMPOSSIBLE_VALUE,
        AlertKind.UNDETERMINED_GOAL,
    ):
        assert kind in kinds, kind
    keys = [a.full_key for a in evaluation.alerts]
    assert len(keys) == len(set(keys))
    assert all(a.period == evaluation.period for a in evaluation.alerts)


def test_alert_filters_by_program_and_level(evaluation: PeriodEvaluation) -> None:
    """Las alertas se filtran por nivel (solo la red) y por programa; las alertas de sede sin indicador se
    conservan."""
    network_only = evaluation.alerts_for(site_codes=[None])
    assert network_only
    assert all(a.site_code is None for a in network_only)
    program = evaluation.alerts_for(program_code="IA")
    assert program
    for alert in program:
        assert alert.indicator_code is None or evaluation.catalog.indicator(alert.indicator_code).program_code == "IA"
