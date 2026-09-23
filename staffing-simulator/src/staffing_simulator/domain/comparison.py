"""Comparación de dos o más escenarios contra un escenario base.

Diferencia absoluta = valor del escenario - valor del base.
Diferencia porcentual = diferencia / valor del base (sin valor si el base es 0).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from staffing_simulator.domain.models import MONTH_LABELS, MONTHS
from staffing_simulator.domain.projection import Dimension, Projection, StaffingSummary
from staffing_simulator.domain.units import safe_ratio
from staffing_simulator.errors import ValidationError

MAX_COMPARED = 6


@dataclass(frozen=True)
class ScenarioTotals:
    scenario_id: int
    name: str
    year: int
    total_cost: int
    monthly: tuple[int, ...]
    cumulative: tuple[int, ...]
    staffing: StaffingSummary


@dataclass(frozen=True)
class ComparisonRow:
    """Un concepto comparado: un valor por escenario y sus diferencias contra el base."""

    label: str
    values: tuple[int, ...]
    differences: tuple[int, ...]
    percentages: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class ScenarioComparison:
    scenarios: tuple[ScenarioTotals, ...]
    base_index: int
    total: ComparisonRow
    monthly: tuple[ComparisonRow, ...]
    by_contract_type: tuple[ComparisonRow, ...]
    by_job_role: tuple[ComparisonRow, ...]

    @property
    def base(self) -> ScenarioTotals:
        return self.scenarios[self.base_index]


def _row(label: str, values: Sequence[int], base_index: int) -> ComparisonRow:
    base_value = values[base_index]
    differences = tuple(value - base_value for value in values)
    return ComparisonRow(
        label=label,
        values=tuple(values),
        differences=differences,
        percentages=tuple(safe_ratio(difference, base_value) for difference in differences),
    )


def _dimension_rows(
    projections: Sequence[Projection], dimension: Dimension, base_index: int
) -> tuple[ComparisonRow, ...]:
    """Filas por elemento de la dimensión; los que faltan en un escenario valen 0."""
    labels: dict[str, list[int]] = {}
    for index, projection in enumerate(projections):
        for row in projection.breakdown(dimension):
            labels.setdefault(row.label, [0] * len(projections))[index] += row.total
    rows = [_row(label, values, base_index) for label, values in labels.items()]
    return tuple(sorted(rows, key=lambda row: (-max(row.values), row.label)))


def compare_projections(projections: Sequence[Projection], base_index: int = 0) -> ScenarioComparison:
    """Compara escenarios ya proyectados; el base es `projections[base_index]`."""
    if len(projections) < 2:
        raise ValidationError("Seleccione al menos dos escenarios para comparar.")
    if len(projections) > MAX_COMPARED:
        raise ValidationError(
            f"Se pueden comparar como máximo {MAX_COMPARED} escenarios a la vez para que el gráfico se pueda leer."
        )
    if not 0 <= base_index < len(projections):
        raise ValidationError("El escenario base debe estar entre los escenarios comparados.")
    ids = [projection.scenario.id for projection in projections]
    if len(set(ids)) != len(ids):
        raise ValidationError("Cada escenario puede aparecer una sola vez en la comparación.")
    totals = tuple(
        ScenarioTotals(
            scenario_id=projection.scenario.id,
            name=projection.scenario.name,
            year=projection.scenario.year,
            total_cost=projection.total_cost,
            monthly=projection.monthly(),
            cumulative=projection.cumulative(),
            staffing=projection.staffing(),
        )
        for projection in projections
    )
    monthly_rows = tuple(
        _row(MONTH_LABELS[month - 1], [item.monthly[month - 1] for item in totals], base_index) for month in MONTHS
    )
    return ScenarioComparison(
        scenarios=totals,
        base_index=base_index,
        total=_row("Costo total", [item.total_cost for item in totals], base_index),
        monthly=monthly_rows,
        by_contract_type=_dimension_rows(projections, Dimension.CONTRACT_TYPE, base_index),
        by_job_role=_dimension_rows(projections, Dimension.JOB_ROLE, base_index),
    )
