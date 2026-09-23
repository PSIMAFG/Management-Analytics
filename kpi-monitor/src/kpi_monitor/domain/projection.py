"""Proyección al cierre del año con bootstrap de los aportes mensuales.

Método (una sola proyección por indicador y nivel):
- Ritmo promedio: el valor central supone que cada mes que falta aporta el promedio de los
  meses informados del año. Los meses no informados se estiman igual que los futuros
  (faltante no es cero).
- Incertidumbre: se remuestrean con reemplazo los aportes mensuales informados del año
  (pares numerador-denominador cuando el denominador también es flujo) y se reportan los
  percentiles verdaderos 10, 50 y 90 del valor al cierre. La probabilidad de cumplir es la
  fracción de simulaciones cuyo cumplimiento al cierre llega al 100 %.
- Horizonte: los meses que faltan hasta diciembre (12 - mes de corte); nunca más allá.
- Con menos meses informados que el mínimo configurado no se proyecta ("datos insuficientes").
- La red suma, simulación por simulación, los numeradores y denominadores de sus sedes.

Los stocks no se proyectan como flujo: se arrastra el último corte (lo resuelve el motor).
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from kpi_monitor.domain.compliance import EPSILON, classify, round_compliance
from kpi_monitor.domain.enums import Direction, GoalRuleType, ProjectionMethod
from kpi_monitor.domain.goals import GoalResolution
from kpi_monitor.domain.models import GoalRule, Indicator, Thresholds
from kpi_monitor.domain.results import Projection, ProjectionPoint

PERCENTILES = (10.0, 50.0, 90.0)


def derive_seed(base_seed: int, *parts: object) -> int:
    """Semilla estable (no depende del hash aleatorio de Python) para cada serie."""
    text = ":".join(str(part) for part in (base_seed, *parts))
    return zlib.crc32(text.encode("utf-8"))


@dataclass(frozen=True)
class ProjectionBasis:
    """Insumos de una sede: aportes mensuales informados y acumulados a la fecha."""

    month: int
    numerators: tuple[float, ...]
    denominators: tuple[float, ...] | None
    num_acc: float
    den_acc: float

    @property
    def months_observed(self) -> int:
        return len(self.numerators)


@dataclass(frozen=True)
class Draws:
    """Simulaciones del acumulado (numerador y denominador) de una sede o de la red.

    `num_path` y `den_path` tienen forma (simulaciones, meses futuros) con el
    acumulado a fin de cada mes desde el siguiente al corte hasta diciembre.
    """

    month: int
    num_acc: float
    den_acc: float
    num_close: np.ndarray
    den_close: np.ndarray
    num_path: np.ndarray
    den_path: np.ndarray
    central_num_close: float
    central_den_close: float
    central_num_path: np.ndarray
    central_den_path: np.ndarray
    current_rhythm: float
    months_observed: int
    months_projected: int
    bootstrap: bool

    def __add__(self, other: Draws) -> Draws:
        return Draws(
            month=self.month,
            num_acc=self.num_acc + other.num_acc,
            den_acc=self.den_acc + other.den_acc,
            num_close=self.num_close + other.num_close,
            den_close=self.den_close + other.den_close,
            num_path=self.num_path + other.num_path,
            den_path=self.den_path + other.den_path,
            central_num_close=self.central_num_close + other.central_num_close,
            central_den_close=self.central_den_close + other.central_den_close,
            central_num_path=self.central_num_path + other.central_num_path,
            central_den_path=self.central_den_path + other.central_den_path,
            current_rhythm=self.current_rhythm + other.current_rhythm,
            months_observed=max(self.months_observed, other.months_observed),
            months_projected=max(self.months_projected, other.months_projected),
            bootstrap=self.bootstrap or other.bootstrap,
        )


def build_draws(basis: ProjectionBasis, *, draws: int, min_months: int, seed: int) -> Draws | None:
    """Simula el cierre de una sede. Devuelve None si no hay ningún mes informado.

    Con menos meses que `min_months` la simulación es determinista (solo el
    ritmo promedio) y queda marcada como no bootstrap.
    """
    observed = basis.months_observed
    if observed == 0:
        return None
    horizon = 12 - basis.month
    missing_past = max(0, basis.month - observed)
    remaining = missing_past + horizon
    nums = np.asarray(basis.numerators, dtype=float)
    dens = np.asarray(basis.denominators, dtype=float) if basis.denominators is not None else None
    use_bootstrap = observed >= min_months

    if use_bootstrap:
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, observed, size=(draws, remaining))
        num_steps = nums[idx]
        den_steps = dens[idx] if dens is not None else np.zeros((draws, remaining))
    else:
        num_steps = np.full((draws, remaining), nums.mean())
        den_steps = np.full((draws, remaining), dens.mean() if dens is not None else 0.0)

    num_cum = basis.num_acc + np.cumsum(num_steps, axis=1)
    den_cum = basis.den_acc + np.cumsum(den_steps, axis=1)
    central_num_steps = np.full(remaining, nums.mean())
    central_den_steps = np.full(remaining, dens.mean() if dens is not None else 0.0)
    central_num_cum = basis.num_acc + np.cumsum(central_num_steps)
    central_den_cum = basis.den_acc + np.cumsum(central_den_steps)

    # Las primeras columnas corresponden a meses pasados no informados; la ruta mensual solo
    # muestra los meses futuros, que ya incluyen la estimación de esos faltantes.
    path_slice = slice(missing_past, remaining)
    if remaining:
        num_close, den_close = num_cum[:, -1], den_cum[:, -1]
        central_num_close, central_den_close = float(central_num_cum[-1]), float(central_den_cum[-1])
    else:
        num_close = np.full(draws, basis.num_acc)
        den_close = np.full(draws, basis.den_acc)
        central_num_close, central_den_close = basis.num_acc, basis.den_acc
    return Draws(
        month=basis.month,
        num_acc=basis.num_acc,
        den_acc=basis.den_acc,
        num_close=num_close,
        den_close=den_close,
        num_path=num_cum[:, path_slice],
        den_path=den_cum[:, path_slice],
        central_num_close=central_num_close,
        central_den_close=central_den_close,
        central_num_path=central_num_cum[path_slice],
        central_den_path=central_den_cum[path_slice],
        current_rhythm=float(nums.mean()),
        months_observed=observed,
        months_projected=remaining,
        bootstrap=use_bootstrap,
    )


def with_static_denominator(draws: Draws, denominator: float) -> Draws:
    """Fija el denominador al cierre para tipos que no se acumulan (meta fija, stock, población)."""
    size, steps = draws.num_path.shape
    return Draws(
        month=draws.month,
        num_acc=draws.num_acc,
        den_acc=denominator,
        num_close=draws.num_close,
        den_close=np.full(size, denominator),
        num_path=draws.num_path,
        den_path=np.full((size, steps), denominator),
        central_num_close=draws.central_num_close,
        central_den_close=denominator,
        central_num_path=draws.central_num_path,
        central_den_path=np.full(steps, denominator),
        current_rhythm=draws.current_rhythm,
        months_observed=draws.months_observed,
        months_projected=draws.months_projected,
        bootstrap=draws.bootstrap,
    )


def combine(parts: Sequence[Draws]) -> Draws | None:
    """Suma simulación por simulación las sedes que forman la red."""
    if not parts:
        return None
    total = parts[0]
    for part in parts[1:]:
        total = total + part
    return total


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    out = np.full(num.shape, np.nan)
    np.divide(num, den, out=out, where=den > 0)
    return out


def _published(values: np.ndarray) -> np.ndarray:
    """Cumplimientos redondeados a la precisión publicada (0,1 punto porcentual, mitad hacia arriba).

    Una simulación cumple si su cumplimiento, publicado con un decimal, llega a 100,0 %:
    el mismo criterio que el semáforo.
    """
    return np.floor(values * 1000 + 0.5 + EPSILON) / 1000


def compliance_array(
    indicator: Indicator, rule: GoalRule | None, values: np.ndarray, goal: float | None
) -> np.ndarray | None:
    """Cumplimiento vectorizado con las mismas reglas que `compute_compliance`."""
    if rule is None:
        return None
    if rule.rule_type is GoalRuleType.BANDS:
        result = np.full(values.shape, rule.bands[-1].compliance)
        for band in reversed(rule.bands[:-1]):
            assert band.upper_bound is not None
            result = np.where(values <= band.upper_bound, band.compliance, result)
        return np.where(np.isnan(values), np.nan, result)
    if goal is None or goal <= 0:
        return None
    if indicator.direction is Direction.HIGHER_IS_BETTER:
        return values / goal
    safe = np.where(values > 0, values, 1.0)
    return np.where(values > 0, goal / safe, np.where(np.isnan(values), np.nan, 1.0))


def summarize(
    indicator: Indicator,
    rule: GoalRule | None,
    goal: GoalResolution,
    draws: Draws,
    thresholds: Thresholds,
) -> Projection:
    """Convierte las simulaciones en la proyección que ven la interfaz y los reportes."""
    horizon = 12 - draws.month
    if not draws.bootstrap:
        return Projection(
            method=ProjectionMethod.INSUFFICIENT,
            months_observed=draws.months_observed,
            months_projected=draws.months_projected,
            horizon=horizon,
            current_rhythm=draws.current_rhythm,
        )
    values = _ratio(draws.num_close, draws.den_close)
    central = draws.central_num_close / draws.central_den_close if draws.central_den_close > 0 else None
    valid = values[~np.isnan(values)]
    p10 = p50 = p90 = None
    if valid.size:
        p10, p50, p90 = (float(v) for v in np.percentile(valid, PERCENTILES))

    goal_value = goal.value if goal.determinable else None
    compliance = None
    probability = None
    status = None
    if goal.determinable and central is not None:
        central_compliance = compliance_array(indicator, rule, np.array([central]), goal_value)
        if central_compliance is not None:
            compliance = round_compliance(float(central_compliance[0]))
            status = classify(compliance, thresholds)
        draw_compliance = compliance_array(indicator, rule, values, goal_value)
        if draw_compliance is not None:
            finite = draw_compliance[~np.isnan(draw_compliance)]
            if finite.size:
                probability = float(np.mean(_published(finite) >= 1 - EPSILON))

    path: list[ProjectionPoint] = []
    path_values = _ratio(draws.num_path, draws.den_path)
    central_path = _ratio(draws.central_num_path, draws.central_den_path)
    for step in range(path_values.shape[1]):
        column = path_values[:, step]
        column = column[~np.isnan(column)]
        low, high = (float(v) for v in np.percentile(column, (10.0, 90.0))) if column.size else (None, None)
        mid = float(central_path[step]) if not np.isnan(central_path[step]) else None
        path.append(ProjectionPoint(draws.month + step + 1, mid, low, high))

    required = None
    if (
        goal_value is not None
        and horizon > 0
        and indicator.direction is Direction.HIGHER_IS_BETTER
        and rule is not None
        and rule.rule_type is not GoalRuleType.BANDS
    ):
        required = max(0.0, (goal_value * draws.central_den_close - draws.num_acc) / horizon)

    return Projection(
        method=ProjectionMethod.BOOTSTRAP,
        value=central,
        p10=p10,
        p50=p50,
        p90=p90,
        compliance=compliance,
        status=status,
        probability=probability,
        months_observed=draws.months_observed,
        months_projected=draws.months_projected,
        horizon=horizon,
        current_rhythm=draws.current_rhythm,
        required_rhythm=required,
        draws=int(draws.num_close.size),
        path=tuple(path),
    )
