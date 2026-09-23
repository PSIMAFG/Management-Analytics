"""Proyección de un escenario y sus agregados.

Todos los totales se obtienen sumando las líneas por posición y mes, de modo
que cualquier desglose (por tipo de contrato, cargo, sede o programa) suma
exactamente el total del escenario.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import accumulate

from staffing_simulator.domain.costing import CostLine, cost_positions
from staffing_simulator.domain.models import MONTHS, Position, Scenario
from staffing_simulator.domain.parameters import CostParameters
from staffing_simulator.domain.units import MINUTES_PER_HOUR, ZERO, round_pesos, safe_ratio

HOURS_DISPLAY = Decimal("0.1")


class Dimension(StrEnum):
    """Ejes de desglose del costo."""

    CONTRACT_TYPE = "contract_type"
    JOB_ROLE = "job_role"
    SITE = "site"
    PROGRAM = "program"
    BUDGET_ITEM = "budget_item"

    @property
    def label(self) -> str:
        return {
            Dimension.CONTRACT_TYPE: "Tipo de contrato",
            Dimension.JOB_ROLE: "Cargo",
            Dimension.SITE: "Sede",
            Dimension.PROGRAM: "Programa",
            Dimension.BUDGET_ITEM: "Ítem presupuestario",
        }[self]

    def key_of(self, position: Position) -> tuple[int, str]:
        """Identificador y nombre del elemento de la dimensión para una posición."""
        if self is Dimension.CONTRACT_TYPE:
            return position.contract_type.id, position.contract_type.name
        if self is Dimension.JOB_ROLE:
            return position.job_role.id, position.job_role.name
        if self is Dimension.SITE:
            return position.site.id, position.site.name
        if self is Dimension.BUDGET_ITEM:
            return position.budget_item.id, position.budget_item.label
        return position.program.id, position.program.name


class Measure(StrEnum):
    """Monto de la línea de costo que se agrega."""

    COST = "cost"
    GROSS = "gross"
    BASE = "base"
    ABSENCE_DISCOUNT = "absence_discount"
    EMPLOYER_CONTRIBUTION = "employer_contribution"
    RETENTION = "retention"

    def of(self, line: CostLine) -> int:
        value: int = getattr(line, self.value)
        return value


@dataclass(frozen=True)
class BreakdownRow:
    key: int
    label: str
    monthly: tuple[int, ...]
    total: int
    share: Decimal | None


@dataclass(frozen=True)
class PositionCost:
    """Resumen anual de una posición."""

    position: Position
    lines: tuple[CostLine, ...]

    @property
    def monthly(self) -> tuple[int, ...]:
        return tuple(line.cost for line in self.lines)

    @property
    def total(self) -> int:
        return sum(line.cost for line in self.lines)

    @property
    def gross(self) -> int:
        return sum(line.gross for line in self.lines)

    @property
    def employer_contribution(self) -> int:
        return sum(line.employer_contribution for line in self.lines)

    @property
    def retention(self) -> int:
        return sum(line.retention for line in self.lines)

    @property
    def absence_discount(self) -> int:
        return sum(line.absence_discount for line in self.lines)

    @property
    def active_months(self) -> int:
        return sum(1 for line in self.lines if line.active)

    @property
    def hourly_rate(self) -> Decimal:
        return max((line.hourly_rate for line in self.lines), default=ZERO)


@dataclass(frozen=True)
class StaffingSummary:
    """Dotación del escenario en el año.

    - `persons`: personas distintas con al menos una posición vigente en el año.
    - `vacancies`: vacantes (suma de las cantidades de las posiciones sin persona).
    - `position_records`: registros de posición vigentes, contando cantidades; los
      tramos de una misma persona (por ejemplo, un cambio de jornada) cuentan por separado.
    - `weekly_hours`: horas semanales promedio del año, ponderadas por días de vigencia.
    """

    persons: int
    vacancies: int
    position_records: int
    weekly_hours: Decimal

    @property
    def headcount(self) -> int:
        """Puestos: personas más vacantes (una persona con varias posiciones cuenta una vez)."""
        return self.persons + self.vacancies


@dataclass(frozen=True)
class Projection:
    """Resultado del costeo de un escenario: líneas por posición y mes y sus agregados."""

    scenario: Scenario
    positions: tuple[Position, ...]
    lines: tuple[CostLine, ...]
    weeks_per_month: Decimal = Decimal(4)

    def _sum_by_month(self, lines: Iterable[CostLine], measure: Measure) -> tuple[int, ...]:
        totals = dict.fromkeys(MONTHS, 0)
        for line in lines:
            totals[line.month] += measure.of(line)
        return tuple(totals[month] for month in MONTHS)

    def monthly(self, measure: Measure = Measure.COST) -> tuple[int, ...]:
        return self._sum_by_month(self.lines, measure)

    def cumulative(self, measure: Measure = Measure.COST) -> tuple[int, ...]:
        return tuple(accumulate(self.monthly(measure)))

    def total(self, measure: Measure = Measure.COST) -> int:
        return sum(measure.of(line) for line in self.lines)

    @property
    def total_cost(self) -> int:
        return self.total(Measure.COST)

    @property
    def average_monthly_cost(self) -> int:
        """Promedio de los 12 meses del año, redondeado a peso."""
        return round_pesos(Decimal(self.total_cost) / 12)

    @property
    def fee_gross_total(self) -> int:
        """Bruto de los contratos a los que se les aplica retención."""
        return sum(line.gross for line in self.lines if line.applies_retention)

    @property
    def retention_total(self) -> int:
        return self.total(Measure.RETENTION)

    @property
    def fee_net_total(self) -> int:
        """Líquido estimado de los honorarios (bruto - retención)."""
        return self.fee_gross_total - self.retention_total

    def _position_index(self) -> dict[int, Position]:
        return {position.id: position for position in self.positions}

    def breakdown(self, dimension: Dimension, measure: Measure = Measure.COST) -> tuple[BreakdownRow, ...]:
        """Totales mensuales y anuales por elemento de la dimensión, de mayor a menor."""
        index = self._position_index()
        grouped: dict[tuple[int, str], list[CostLine]] = defaultdict(list)
        for line in self.lines:
            grouped[dimension.key_of(index[line.position_id])].append(line)
        grand_total = self.total(measure)
        rows = []
        for (key, label), lines in grouped.items():
            monthly = self._sum_by_month(lines, measure)
            total = sum(monthly)
            rows.append(BreakdownRow(key, label, monthly, total, safe_ratio(total, grand_total)))
        return tuple(sorted(rows, key=lambda row: (-row.total, row.label)))

    def position_costs(self) -> tuple[PositionCost, ...]:
        by_position: dict[int, list[CostLine]] = defaultdict(list)
        for line in self.lines:
            by_position[line.position_id].append(line)
        return tuple(
            PositionCost(position, tuple(sorted(by_position[position.id], key=lambda line: line.month)))
            for position in self.positions
        )

    def active_positions(self) -> tuple[Position, ...]:
        """Posiciones con al menos un día de vigencia en el año del escenario."""
        return tuple(position for position in self.positions if position.period_in_year(self.scenario.year))

    def _equivalent_weekly_hours(self, position: Position) -> Decimal:
        minutes = position.weekly_equivalent_minutes(self.weeks_per_month) * position.quantity
        return minutes / MINUTES_PER_HOUR

    def weekly_hours_on(self, day: date) -> Decimal:
        """Horas semanales de las posiciones vigentes en una fecha (contratos por horas en equivalente)."""
        total = sum(
            (self._equivalent_weekly_hours(position) for position in self.positions if position.is_active_on(day)),
            ZERO,
        )
        return total.quantize(HOURS_DISPLAY)

    def staffing(self) -> StaffingSummary:
        """Dotación con vigencia en el año y horas semanales promedio ponderadas por días vigentes."""
        year = self.scenario.year
        days_in_year = (date(year, 12, 31) - date(year, 1, 1)).days + 1
        active = self.active_positions()
        persons = {position.person.id for position in active if position.person is not None}
        weighted = ZERO
        for position in active:
            period = position.period_in_year(year)
            if period is None:
                continue
            share = Decimal((period[1] - period[0]).days + 1) / Decimal(days_in_year)
            weighted += self._equivalent_weekly_hours(position) * share
        return StaffingSummary(
            persons=len(persons),
            vacancies=sum(position.quantity for position in active if position.person is None),
            position_records=sum(position.quantity for position in active),
            weekly_hours=weighted.quantize(HOURS_DISPLAY),
        )


def project_scenario(
    scenario: Scenario,
    positions: Iterable[Position],
    params: CostParameters,
    on_position: Callable[[int, int], None] | None = None,
) -> Projection:
    """Costea todas las posiciones del escenario.

    `on_position(hechas, total)` permite informar avance en escenarios grandes.
    """
    ordered = tuple(positions)
    lines: list[CostLine] = []
    for done, position in enumerate(ordered, start=1):
        lines.extend(cost_positions((position,), scenario, params))
        if on_position is not None:
            on_position(done, len(ordered))
    return Projection(scenario, ordered, tuple(lines), params.settings.weeks_per_month)
