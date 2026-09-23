"""Meta efectiva, meta a la fecha y tramos."""

from __future__ import annotations

import pytest

from helpers import BANDS, indicator, rule
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Direction, GoalRuleType, Scale
from kpi_monitor.domain.goals import band_compliance, describe_rule, goal_to_date, resolve_goal


def test_absolute_goal_uses_configured_value() -> None:
    """Regla: la meta absoluta es el valor configurado, sin depender de la referencia del año anterior."""
    goal = resolve_goal(rule(value=0.9), reference=0.5)
    assert goal.determinable
    assert goal.value == pytest.approx(0.9)


def test_absolute_goal_of_zero_is_not_determinable() -> None:
    """Regla: una meta absoluta igual a cero se trata como no determinable."""
    goal = resolve_goal(rule(value=0.0), reference=None)
    assert not goal.determinable
    assert goal.value is None


def test_relative_increase_multiplies_previous_year_reference() -> None:
    """Regla: la meta de aumento relativo es la referencia del año anterior multiplicada por (1 + factor)."""
    goal = resolve_goal(rule(kind=GoalRuleType.RELATIVE_INCREASE, factor=0.2), reference=0.086)
    assert goal.determinable
    assert goal.value == pytest.approx(0.1032)
    assert goal.reference == pytest.approx(0.086)


@pytest.mark.parametrize(("reference", "expected"), [(10.0, 9.0), (6.0, 7.2), (7.5, 9.0)])
def test_capped_increase_never_exceeds_the_cap(reference: float, expected: float) -> None:
    """Regla: la meta de aumento con tope es el menor valor entre el tope y la referencia multiplicada por (1 +
    factor)."""
    goal = resolve_goal(rule(kind=GoalRuleType.CAPPED_INCREASE, factor=0.2, ceiling=9.0), reference)
    assert goal.value == pytest.approx(expected)


@pytest.mark.parametrize("kind", [GoalRuleType.RELATIVE_INCREASE, GoalRuleType.CAPPED_INCREASE])
@pytest.mark.parametrize("reference", [None, 0.0])
def test_missing_or_zero_reference_makes_goal_undetermined(kind: GoalRuleType, reference: float | None) -> None:
    """Regla: si la referencia del año anterior falta o es cero, las metas interanuales no son determinables y se
    explica el motivo."""
    goal = resolve_goal(rule(kind=kind, factor=0.2, ceiling=9.0), reference)
    assert not goal.determinable
    assert goal.value is None
    assert "año anterior" in goal.reason


def test_baseline_has_no_goal() -> None:
    """Regla: una meta de línea base no tiene valor de meta ni semáforo."""
    goal = resolve_goal(rule(kind=GoalRuleType.BASELINE), reference=0.4)
    assert goal.is_baseline
    assert not goal.determinable


def test_bands_goal_is_the_first_bound() -> None:
    """Regla: en metas por tramos, la meta de referencia es la cota del primer tramo (el de 100 %)."""
    goal = resolve_goal(rule(kind=GoalRuleType.BANDS), reference=None)
    assert goal.determinable
    assert goal.value == 20.0


def test_missing_rule_is_undetermined() -> None:
    """Un indicador sin regla de meta para el año queda con meta no determinable."""
    goal = resolve_goal(None, reference=1.0)
    assert not goal.determinable


@pytest.mark.parametrize(
    ("aggregation", "denominator", "prorated"),
    [
        (Aggregation.FLOW, DenominatorType.FIXED_SITE, True),
        (Aggregation.FLOW, DenominatorType.STOCK, True),
        (Aggregation.FLOW, DenominatorType.K_STOCK, True),
        (Aggregation.FLOW, DenominatorType.POPULATION, True),
        (Aggregation.FLOW, DenominatorType.FLOW, False),
        (Aggregation.FLOW, DenominatorType.MANUAL, False),
        (Aggregation.STOCK, DenominatorType.FIXED_SITE, False),
        (Aggregation.STOCK, DenominatorType.POPULATION, False),
    ],
)
def test_goal_is_prorated_only_when_value_grows_with_time(
    aggregation: Aggregation, denominator: DenominatorType, prorated: bool
) -> None:
    """Regla: la meta a la fecha se prorratea por mes/12 solo si el ratio crece con el tiempo (flujo/fijo o
    flujo/stock); flujo/flujo o manual no se prorratea. Depende de la naturaleza del ratio, no del tipo de meta."""
    ind = indicator(aggregation=aggregation, denominator=denominator)
    expected = 0.9 * 6 / 12 if prorated else 0.9
    assert goal_to_date(ind, 0.9, 6) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, 1.0),
        (20.0, 1.0),
        (20.0001, 0.75),
        (30.0, 0.75),
        (30.5, 0.5),
        (40.0, 0.5),
        (50.0, 0.25),
        (50.01, 0.0),
        (120.0, 0.0),
    ],
)
def test_bands_are_half_open_intervals_without_rounding(value: float, expected: float) -> None:
    """Regla: los tramos usan intervalos semiabiertos explícitos (x <= cota) sobre el promedio sin redondear."""
    assert band_compliance(BANDS, value) == expected


def test_rule_descriptions_are_generated_from_structured_rules() -> None:
    """La descripción de cada meta se genera desde la regla estructurada, con formato chileno y los tramos
    explícitos."""
    ind = indicator(denominator=DenominatorType.FIXED_SITE)
    assert "90,0 %" in describe_rule(rule(value=0.9), ind)
    assert "parte proporcional" in describe_rule(rule(value=0.9), ind)
    assert "20 %" in describe_rule(rule(kind=GoalRuleType.RELATIVE_INCREASE, factor=0.2), ind)
    rate = indicator(denominator=DenominatorType.STOCK, scale=Scale.RATE)
    assert "tope de 9,00" in describe_rule(rule(kind=GoalRuleType.CAPPED_INCREASE, factor=0.2, ceiling=9.0), rate)
    days = indicator(direction=Direction.LOWER_IS_BETTER, scale=Scale.DAYS)
    text = describe_rule(rule(kind=GoalRuleType.BANDS), days)
    assert text.startswith("Tramos: hasta 20 días: 100 %")
    assert "más de 50 días: 0 %" in text
    assert "Línea base" in describe_rule(rule(kind=GoalRuleType.BASELINE), ind)
