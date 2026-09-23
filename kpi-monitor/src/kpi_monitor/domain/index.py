r"""Índice ponderado de cumplimiento por programa.

$$I = \frac{\sum_i w_i \min(1, c_i)}{\sum_{i \text{ con dato}} w_i}$$

Cada indicador aporta como máximo su peso (sobrecumplir uno no compensa a
otro). Los indicadores sin datos o con meta no determinable no entran al
cálculo, pero su peso se informa aparte para que se vea qué parte del
programa no está medida. Los de línea base, los que no aplican a la sede y
los que aún no llegan a su primer corte del año quedan fuera por diseño.

El índice proyectado al cierre usa los mismos indicadores y pesos que el
índice a la fecha: cada uno entra con su cumplimiento proyectado y, si no tiene
proyección propia (por ejemplo, con datos insuficientes), con su cumplimiento a
la fecha. Así la diferencia entre ambos índices solo refleja las proyecciones.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kpi_monitor.domain.enums import Status
from kpi_monitor.domain.results import IndicatorResult

# Diferencias de peso menores que esto se consideran ruido de punto flotante.
WEIGHT_EPSILON = 1e-9


@dataclass(frozen=True)
class WeightedIndex:
    """Índice ponderado de un programa en un nivel (sede o red)."""

    program_code: str
    site_code: str | None
    value: float | None
    projected: float | None
    weight_total: float
    weight_with_data: float
    weight_without_data: float
    weight_undetermined: float
    weight_baseline: float
    weight_not_applicable: float
    weight_projected: float
    indicators_counted: int
    weight_pending: float = 0.0

    @property
    def weight_evaluable(self) -> float:
        """Peso de los indicadores que deberían tener semáforo a la fecha."""
        return self.weight_total - self.weight_baseline - self.weight_not_applicable - self.weight_pending

    @property
    def coverage(self) -> float | None:
        """Fracción del peso evaluable que tiene dato (entre 0 y 1)."""
        evaluable = self.weight_evaluable
        if evaluable <= WEIGHT_EPSILON:
            return None
        return min(1.0, max(0.0, self.weight_with_data / evaluable))

    @property
    def pct_weight_without_data(self) -> float | None:
        """Fracción del peso evaluable sin dato o con meta no determinable (entre 0 y 1).

        Se redondea a cero cuando la diferencia es solo ruido de punto flotante,
        para no mostrar un "-0,0 %".
        """
        coverage = self.coverage
        if coverage is None:
            return None
        missing = 1.0 - coverage
        return 0.0 if missing < WEIGHT_EPSILON else missing

    @property
    def projected_share(self) -> float | None:
        """Fracción del peso del índice que tiene proyección propia (el resto entra con su valor a la fecha)."""
        if self.weight_with_data <= WEIGHT_EPSILON:
            return None
        return min(1.0, self.weight_projected / self.weight_with_data)


def _projected_or_current(result: IndicatorResult) -> float:
    projected = result.capped_projected_compliance
    return projected if projected is not None else (result.capped_compliance or 0.0)


def weighted_index(results: Sequence[IndicatorResult], program_code: str, site_code: str | None) -> WeightedIndex:
    """Calcula el índice a la fecha y al cierre proyectado para un programa y un nivel.

    Sin ninguna proyección propia en el programa, el índice proyectado queda vacío
    (no se presenta el valor a la fecha como si fuera una proyección).
    """
    chosen = [r for r in results if r.program_code == program_code and r.site_code == site_code]
    total = sum(r.weight for r in chosen)
    counted = [r for r in chosen if r.status.is_evaluated and r.capped_compliance is not None]
    with_data = sum(r.weight for r in counted)
    value = sum(r.weight * (r.capped_compliance or 0.0) for r in counted) / with_data if with_data > 0 else None

    weight_projected = sum(r.weight for r in counted if r.capped_projected_compliance is not None)
    projected = None
    if with_data > 0 and weight_projected > WEIGHT_EPSILON:
        projected = sum(r.weight * _projected_or_current(r) for r in counted) / with_data

    def weight_of(status: Status) -> float:
        return sum(r.weight for r in chosen if r.status is status)

    return WeightedIndex(
        program_code=program_code,
        site_code=site_code,
        value=value,
        projected=projected,
        weight_total=total,
        weight_with_data=with_data,
        weight_without_data=weight_of(Status.NO_DATA),
        weight_undetermined=weight_of(Status.UNDETERMINED),
        weight_baseline=weight_of(Status.BASELINE),
        weight_not_applicable=weight_of(Status.NOT_APPLICABLE),
        weight_projected=weight_projected,
        indicators_counted=len(counted),
        weight_pending=weight_of(Status.PENDING),
    )


def index_by_month(results: Sequence[IndicatorResult], program_code: str, site_code: str | None) -> list[float | None]:
    """Evolución del índice ponderado a la fecha, mes a mes, hasta el mes de corte.

    Usa el cumplimiento acumulado de cada mes que ya trae cada resultado, con la
    misma regla de tope y de pesos que el índice del período.
    """
    chosen = [r for r in results if r.program_code == program_code and r.site_code == site_code]
    if not chosen:
        return []
    month = chosen[0].period.month
    values: list[float | None] = []
    for m in range(1, month + 1):
        points = [(r.weight, r.monthly[m - 1]) for r in chosen]
        counted = [(w, p.compliance) for w, p in points if p.status.is_evaluated and p.compliance is not None]
        total = sum(w for w, _ in counted)
        values.append(sum(w * min(1.0, c or 0.0) for w, c in counted) / total if total > 0 else None)
    return values
