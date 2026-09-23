"""Alertas derivadas de la evaluación del período.

Cada alerta tiene una clave (tipo, indicador, sede) y el período evaluado. Una
alerta es "nueva" si su clave no estaba en el período anterior, no en la
ejecución anterior: el resultado no depende de cuántas veces se abra la
aplicación.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from kpi_monitor.domain.compliance import EPSILON
from kpi_monitor.domain.enums import AlertKind, Direction, Severity, Status
from kpi_monitor.domain.models import Period
from kpi_monitor.domain.results import IndicatorEvaluation, IndicatorResult
from kpi_monitor.domain.text import format_pct, format_value, join_months

_KIND_ORDER = {kind: i for i, kind in enumerate(AlertKind)}
# Sin alertas: el indicador no aplica a la sede o todavía no corresponde su primer corte del año.
_SKIPPED = (Status.NOT_APPLICABLE, Status.PENDING)
# Casos esperados en el mes desde los cuales un cero se considera sospechoso.
MIN_EXPECTED_FOR_UNDERREPORTING = 3.0


@dataclass(frozen=True)
class Alert:
    """Aviso para la jefatura, con su gravedad y un mensaje listo para mostrar."""

    kind: AlertKind
    severity: Severity
    period: Period
    indicator_code: str | None
    site_code: str | None
    site_name: str
    indicator_name: str
    message: str
    is_new: bool = True

    @property
    def key(self) -> tuple[AlertKind, str | None, str | None]:
        """Clave sin período, usada para saber si la alerta ya existía el mes anterior."""
        return (self.kind, self.indicator_code, self.site_code)

    @property
    def full_key(self) -> tuple[AlertKind, str | None, str | None, int, int]:
        return (*self.key, self.period.year, self.period.month)


def _alert(
    kind: AlertKind, severity: Severity, result: IndicatorResult, message: str, *, indicator: bool = True
) -> Alert:
    return Alert(
        kind=kind,
        severity=severity,
        period=result.period,
        indicator_code=result.indicator.code if indicator else None,
        site_code=result.site_code,
        site_name=result.site_name,
        indicator_name=result.indicator.short_name if indicator else "Todos los indicadores",
        message=message,
    )


def _risk_alert(result: IndicatorResult) -> Alert | None:
    scale = result.indicator.scale
    if result.status is Status.RED:
        return _alert(
            AlertKind.AT_RISK,
            Severity.CRITICAL,
            result,
            f"Cumplimiento a la fecha de {format_pct(result.compliance)}: valor {format_value(result.value, scale)} "
            f"frente a una meta a la fecha de {format_value(result.goal_to_date, scale)}.",
        )
    projected = result.projection.compliance
    if result.status.is_evaluated and projected is not None and projected < 1 - EPSILON:
        return _alert(
            AlertKind.AT_RISK,
            Severity.WARNING,
            result,
            f"La proyección al cierre ({format_value(result.projection.value, scale)}) queda bajo la meta anual "
            f"({format_value(result.goal.value, scale)}); cumplimiento proyectado {format_pct(projected)}.",
        )
    return None


def _impossible_alert(result: IndicatorResult) -> Alert | None:
    ceiling = result.indicator.natural_ceiling
    if ceiling is None:
        return None
    scale = result.indicator.scale
    reasons: list[str] = []
    value = result.value
    if value is not None and value > ceiling + EPSILON:
        reasons.append(f"el acumulado ({format_value(value, scale)}) supera el techo natural")
    if not result.is_network and result.indicator.has_flow_denominator:
        months = [
            p.month
            for p in result.monthly
            if p.monthly_value is not None and p.monthly_value > ceiling + EPSILON and p.month <= result.period.month
        ]
        if months:
            reasons.append(f"el numerador supera al denominador en {join_months(months)}")
    if not reasons:
        return None
    text = "; ".join(reasons)
    return _alert(
        AlertKind.IMPOSSIBLE_VALUE,
        Severity.CRITICAL,
        result,
        f"Dato imposible: {text}. El techo natural del indicador es {format_value(ceiling, scale)}; "
        "revise el registro de origen.",
    )


def _expected_numerator(result: IndicatorResult, network: IndicatorResult, point_denominator: float) -> float | None:
    """Numerador esperado en un mes si la sede rindiera como la red.

    Si el valor crece con el tiempo, el ritmo mensual de la red es su acumulado
    dividido por sus meses informados (un mes faltante no baja ese ritmo).
    """
    reference = network.value
    if reference is None:
        return None
    if result.indicator.grows_with_time:
        months = network.months_equivalent
        return point_denominator * reference / months if months > 0 else None
    return point_denominator * reference


def _underreporting_alert(result: IndicatorResult, network: IndicatorResult) -> Alert | None:
    """Ceros informados donde lo esperable era una cantidad clara (al menos 3 casos).

    Con volúmenes pequeños un cero puede ser real; por eso se compara con lo que
    habría registrado la sede al ritmo de la red.
    """
    if result.is_network or result.indicator.direction is not Direction.HIGHER_IS_BETTER:
        return None
    months = []
    for p in result.monthly[: result.period.month]:
        if not p.reported or p.numerator != 0 or p.denominator is None or p.denominator <= 0:
            continue
        expected = _expected_numerator(result, network, p.denominator)
        if expected is not None and expected >= MIN_EXPECTED_FOR_UNDERREPORTING:
            months.append(p.month)
    if not months:
        return None
    return _alert(
        AlertKind.UNDERREPORTING,
        Severity.WARNING,
        result,
        f"Numerador cero con denominador positivo en {join_months(months)}, cuando al ritmo de la red se "
        "esperaban varios casos: posible subregistro.",
    )


def _missing_denominator_alert(result: IndicatorResult) -> Alert | None:
    if result.numerator is None or (result.denominator is not None and result.denominator > 0):
        return None
    return _alert(
        AlertKind.MISSING_DENOMINATOR,
        Severity.WARNING,
        result,
        "Hay actividad informada pero no hay denominador vigente; el indicador no se puede calcular.",
    )


def build_alerts(evaluations: Sequence[IndicatorEvaluation], period: Period) -> list[Alert]:
    """Genera las alertas del período para la red y cada sede."""
    alerts: list[Alert] = []
    missing: dict[str, list[IndicatorResult]] = defaultdict(list)
    expected: dict[str, int] = defaultdict(int)
    for evaluation in evaluations:
        for result in evaluation.all_results:
            if result.status in _SKIPPED:
                continue
            candidates = [
                _risk_alert(result),
                _impossible_alert(result),
                _underreporting_alert(result, evaluation.network),
                _missing_denominator_alert(result),
            ]
            if result.status is Status.UNDETERMINED:
                candidates.append(
                    _alert(AlertKind.UNDETERMINED_GOAL, Severity.INFO, result, result.goal.reason or result.note)
                )
            if (
                result.status is Status.NO_DATA
                and result.numerator is None
                and not result.is_network
                and result.indicator.expects_data_until(period.month)
            ):
                candidates.append(_alert(AlertKind.NO_REPORT, Severity.WARNING, result, result.note))
            alerts.extend(a for a in candidates if a is not None)
            if result.is_network or result.numerator is None or not result.indicator.expects_data_in(period.month):
                continue
            assert result.site_code is not None
            expected[result.site_code] += 1
            point = result.monthly[period.month - 1]
            if point.reported is not True:
                missing[result.site_code].append(result)
    for site_code, results in missing.items():
        if len(results) == expected[site_code]:
            alerts.append(
                _alert(
                    AlertKind.NO_REPORT,
                    Severity.WARNING,
                    results[0],
                    f"La sede no informó datos de {period.label}.",
                    indicator=False,
                )
            )
            continue
        alerts.extend(
            _alert(AlertKind.NO_REPORT, Severity.WARNING, r, f"No hay dato informado de {period.label}.")
            for r in results
        )
    return sort_alerts(alerts)


def sort_alerts(alerts: Iterable[Alert]) -> list[Alert]:
    return sorted(
        alerts,
        key=lambda a: (
            a.severity.rank,
            _KIND_ORDER[a.kind],
            a.site_code is not None,
            a.site_name,
            a.indicator_code or "",
        ),
    )


def mark_new(current: Sequence[Alert], previous: Iterable[Alert]) -> list[Alert]:
    """Marca como nuevas las alertas cuya clave no existía en el período anterior."""
    known = {alert.key for alert in previous}
    return [replace(alert, is_new=alert.key not in known) for alert in current]
