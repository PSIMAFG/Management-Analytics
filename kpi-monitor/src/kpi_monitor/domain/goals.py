"""Meta efectiva, meta a la fecha y cumplimiento por tramos.

Hay una sola función de meta efectiva (`resolve_goal`) que usan el semáforo,
la proyección, el índice ponderado, las alertas y los reportes, de modo que
ninguna vista compare contra una meta distinta.
"""

from __future__ import annotations

from dataclasses import dataclass

from kpi_monitor.domain.enums import GoalRuleType, Scale
from kpi_monitor.domain.models import GoalBand, GoalRule, Indicator
from kpi_monitor.domain.text import format_decimal, format_pct, format_value


@dataclass(frozen=True)
class GoalResolution:
    """Meta anual efectiva de un indicador en un nivel (sede o red).

    `value` está en la escala del indicador. En las reglas por tramos es la
    cota del primer tramo (el valor que asegura el 100 %).
    """

    rule_type: GoalRuleType | None
    value: float | None
    reference: float | None
    determinable: bool
    reason: str = ""

    @property
    def is_baseline(self) -> bool:
        return self.rule_type is GoalRuleType.BASELINE


def resolve_goal(rule: GoalRule | None, reference: float | None) -> GoalResolution:
    """Calcula la meta efectiva del año a partir de la regla y del valor de referencia.

    `reference` es el valor del año anterior completo en el mismo nivel
    (suma de numeradores sobre suma de denominadores). Si la regla depende de
    él y falta o es cero, la meta no es determinable: nunca se inventa una meta.
    """
    if rule is None:
        return GoalResolution(None, None, reference, False, "El indicador no tiene regla de meta para el año.")
    kind = rule.rule_type
    if kind is GoalRuleType.BASELINE:
        return GoalResolution(kind, None, reference, False, "Año de línea base: se mide sin meta de cumplimiento.")
    if kind is GoalRuleType.BANDS:
        return GoalResolution(kind, rule.bands[0].upper_bound, reference, True)
    if kind is GoalRuleType.ABSOLUTE:
        assert rule.value is not None
        if rule.value <= 0:
            return GoalResolution(kind, None, reference, False, "La meta configurada es cero.")
        return GoalResolution(kind, rule.value, reference, True)
    if reference is None:
        return GoalResolution(kind, None, None, False, "No hay valor del año anterior para calcular la meta.")
    if reference <= 0:
        return GoalResolution(
            kind, None, reference, False, "El valor del año anterior es cero; la meta de aumento no se puede calcular."
        )
    assert rule.factor is not None
    raised = reference * (1 + rule.factor)
    if kind is GoalRuleType.CAPPED_INCREASE:
        assert rule.ceiling is not None
        return GoalResolution(kind, min(rule.ceiling, raised), reference, True)
    return GoalResolution(kind, raised, reference, True)


def goal_to_date(indicator: Indicator, annual_goal: float, months: float) -> float:
    """Meta exigible tras `months` meses de actividad.

    Se prorratea (meta × meses / 12) solo cuando el valor acumulado crece con el
    tiempo: numerador de flujo sobre un denominador fijo, de stock o de
    población. Las razones flujo sobre flujo, los promedios y los stocks no se
    prorratean. El motor pasa los meses informados, no el mes calendario, para
    que un mes faltante no se cuente como un mes con cero actividad.
    """
    if indicator.grows_with_time:
        return annual_goal * months / 12
    return annual_goal


def band_compliance(bands: tuple[GoalBand, ...], value: float) -> float:
    """Cumplimiento según tramos semiabiertos: el primer tramo con valor <= cota.

    Con cotas 20, 30, 40 y 50: 20 da 100 %, 20,01 da 75 % y 50,5 da 0 %.
    El valor no se redondea antes de compararlo.
    """
    for band in bands:
        if band.upper_bound is None or value <= band.upper_bound:
            return band.compliance
    return bands[-1].compliance


def describe_rule(rule: GoalRule | None, indicator: Indicator) -> str:
    """Texto de la meta generado desde la regla estructurada (nunca se guarda aparte)."""
    if rule is None:
        return "Sin regla de meta para el año."
    kind = rule.rule_type
    if kind is GoalRuleType.BASELINE:
        return "Línea base: el año se usa para medir; no hay meta de cumplimiento ni semáforo."
    if kind is GoalRuleType.ABSOLUTE:
        assert rule.value is not None
        text = f"Meta anual: {format_value(rule.value, indicator.scale)}"
        if indicator.grows_with_time:
            text += " (a la fecha se exige la parte proporcional de los meses transcurridos)"
        return text + "."
    if kind is GoalRuleType.RELATIVE_INCREASE:
        assert rule.factor is not None
        return f"Aumento de {format_pct(rule.factor, 0)} sobre el valor del año anterior completo."
    if kind is GoalRuleType.CAPPED_INCREASE:
        assert rule.factor is not None
        assert rule.ceiling is not None
        return (
            f"Aumento de {format_pct(rule.factor, 0)} sobre el valor del año anterior completo, "
            f"con tope de {format_value(rule.ceiling, indicator.scale)}."
        )
    return "Tramos: " + "; ".join(
        _describe_band(band, previous, indicator.scale) for band, previous in _band_pairs(rule)
    )


def _band_pairs(rule: GoalRule) -> list[tuple[GoalBand, float | None]]:
    pairs: list[tuple[GoalBand, float | None]] = []
    previous: float | None = None
    for band in rule.bands:
        pairs.append((band, previous))
        previous = band.upper_bound
    return pairs


def _describe_band(band: GoalBand, previous: float | None, scale: Scale) -> str:
    unit = " días" if scale is Scale.DAYS else ""
    share = format_pct(band.compliance, 0)
    if band.upper_bound is None:
        return f"más de {format_decimal(previous or 0, 0)}{unit}: {share}"
    if previous is None:
        return f"hasta {format_decimal(band.upper_bound, 0)}{unit}: {share}"
    return f"más de {format_decimal(previous, 0)} y hasta {format_decimal(band.upper_bound, 0)}{unit}: {share}"
