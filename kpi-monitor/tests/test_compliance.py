"""Cumplimiento con dirección y tramos, y semáforo con umbrales."""

from __future__ import annotations

import pytest

from helpers import indicator, rule
from kpi_monitor.domain.compliance import classify, compute_compliance, meets_goal
from kpi_monitor.domain.enums import Direction, GoalRuleType, Scale, Status
from kpi_monitor.domain.models import Thresholds


def test_higher_is_better_divides_value_by_goal() -> None:
    """Regla: cuando mayor es mejor, el cumplimiento es el valor dividido por la meta a la fecha."""
    assert compute_compliance(indicator(), rule(), 0.72, 0.8) == pytest.approx(0.9)


def test_lower_is_better_divides_goal_by_value() -> None:
    """Regla: cuando menor es mejor, el cumplimiento es la meta dividida por el valor, y un valor bajo la meta
    supera el 100 %."""
    ind = indicator(direction=Direction.LOWER_IS_BETTER, scale=Scale.DAYS)
    assert compute_compliance(ind, rule(value=20.0), 25.0, 20.0) == pytest.approx(0.8)
    assert compute_compliance(ind, rule(value=20.0), 10.0, 20.0) == pytest.approx(2.0)


def test_lower_is_better_with_zero_value_meets_goal() -> None:
    """Regla: cuando menor es mejor, un valor cero cumple la meta (100 %) en vez de dividir por cero."""
    ind = indicator(direction=Direction.LOWER_IS_BETTER, scale=Scale.DAYS)
    assert compute_compliance(ind, rule(value=20.0), 0.0, 20.0) == 1.0


def test_bands_use_band_share_instead_of_ratio() -> None:
    """Regla: en metas por tramos el cumplimiento es el porcentaje del tramo, no la razón entre valor y meta."""
    ind = indicator(direction=Direction.LOWER_IS_BETTER, scale=Scale.DAYS)
    assert compute_compliance(ind, rule(kind=GoalRuleType.BANDS), 27.0, 20.0) == 0.75


@pytest.mark.parametrize(("value", "goal"), [(None, 0.8), (0.5, None), (0.5, 0.0)])
def test_missing_value_or_goal_gives_no_compliance(value: float | None, goal: float | None) -> None:
    """Sin valor, sin meta o con meta cero no hay cumplimiento calculable."""
    assert compute_compliance(indicator(), rule(), value, goal) is None


def test_missing_rule_gives_no_compliance() -> None:
    """Un indicador sin regla de meta en el año no tiene cumplimiento."""
    assert compute_compliance(indicator(), None, 0.5, 0.8) is None


@pytest.mark.parametrize(
    ("compliance", "expected"),
    [
        (1.2, Status.GREEN),
        (1.0, Status.GREEN),
        (0.9999999999, Status.GREEN),
        (0.85, Status.YELLOW),
        (0.9, Status.YELLOW),
        (0.8499, Status.YELLOW),
        (0.8449, Status.RED),
        (0.0, Status.RED),
    ],
)
def test_default_thresholds(compliance: float, expected: Status) -> None:
    """Regla: existe un único semáforo oficial y configurable (verde >= 100 %, amarillo >= 85 %, resto rojo).

    Clasifica sobre el cumplimiento redondeado a la precisión publicada (0,1 punto
    porcentual): 0,8499 se publica como 85,0 % y es amarillo, nunca rojo.
    """
    assert classify(compliance, Thresholds()) is expected


def test_custom_thresholds_are_respected() -> None:
    """Regla: los umbrales configurados reemplazan a los por defecto al clasificar el semáforo."""
    thresholds = Thresholds(green=0.95, yellow=0.8)
    assert classify(0.96, thresholds) is Status.GREEN
    assert classify(0.81, thresholds) is Status.YELLOW
    assert classify(0.79, thresholds) is Status.RED


def test_classify_without_compliance_returns_none() -> None:
    """Sin cumplimiento no se asigna color de semáforo."""
    assert classify(None, Thresholds()) is None


def test_meets_goal() -> None:
    """Cumplir la meta exige llegar al 100 %; sin cumplimiento no se cumple."""
    assert meets_goal(1.0)
    assert not meets_goal(0.99)
    assert not meets_goal(None)
