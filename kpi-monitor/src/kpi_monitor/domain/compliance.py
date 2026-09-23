"""Cumplimiento respecto de la meta y clasificación en el semáforo oficial.

El cumplimiento se publica con una sola precisión: 0,1 punto porcentual. Se
redondea una vez (mitad hacia arriba) antes de clasificarlo, de modo que el
color del semáforo coincide siempre con el porcentaje que se muestra: un
0,8497 se publica como 85,0 % y es amarillo, nunca un "85,0 %" en rojo.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from kpi_monitor.domain.enums import Direction, GoalRuleType, Status
from kpi_monitor.domain.goals import band_compliance
from kpi_monitor.domain.models import GoalRule, Indicator, Thresholds

# Tolerancia para comparar fracciones ya redondeadas sin sorpresas del punto flotante binario.
EPSILON = 1e-9
# Precisión publicada del cumplimiento como fracción: 0,001 equivale a 0,1 punto porcentual.
COMPLIANCE_QUANTUM = Decimal("0.001")


def round_compliance(value: float | None) -> float | None:
    """Redondea un cumplimiento a la precisión publicada (0,1 punto porcentual, mitad hacia arriba).

    Usa la representación decimal del número, igual que el formato de los textos:
    0,8497 da 0,85 y 0,99955 da 1,0.
    """
    if value is None:
        return None
    return float(Decimal(repr(value)).quantize(COMPLIANCE_QUANTUM, rounding=ROUND_HALF_UP))


def compute_compliance(
    indicator: Indicator, rule: GoalRule | None, value: float | None, goal: float | None
) -> float | None:
    """Cumplimiento del valor frente a la meta exigible, redondeado a la precisión publicada.

    - Tramos: porcentaje del tramo en que cae el valor.
    - Mayor es mejor: valor / meta.
    - Menor es mejor: meta / valor; un valor cero cumple (100 %).

    Devuelve None si falta el valor o la meta.
    """
    if value is None or rule is None:
        return None
    if rule.rule_type is GoalRuleType.BANDS:
        return round_compliance(band_compliance(rule.bands, value))
    if goal is None or goal <= 0:
        return None
    if indicator.direction is Direction.HIGHER_IS_BETTER:
        return round_compliance(value / goal)
    if value <= 0:
        return 1.0
    return round_compliance(goal / value)


def classify(compliance: float | None, thresholds: Thresholds) -> Status | None:
    """Color del semáforo: verde desde el umbral verde, amarillo desde el amarillo, rojo bajo él.

    Clasifica el cumplimiento redondeado a la precisión publicada, el mismo que se muestra.
    """
    rounded = round_compliance(compliance)
    if rounded is None:
        return None
    if rounded >= thresholds.green - EPSILON:
        return Status.GREEN
    if rounded >= thresholds.yellow - EPSILON:
        return Status.YELLOW
    return Status.RED


def meets_goal(compliance: float | None) -> bool:
    """Indica si el cumplimiento, redondeado como se publica, alcanza el 100 % de la meta."""
    rounded = round_compliance(compliance)
    return rounded is not None and rounded >= 1 - EPSILON
