"""Contraste de la proyección con la estructura financiera del convenio: asignado, planificado y ejecutado.

- Planificado de un ítem = costo de recurso humano de las posiciones que se le
  imputan (de la proyección) más los otros gastos planificados que se le
  imputan. Saldo = asignado (el monto del ítem) - planificado.
- Ejecutado a la fecha = suma de los meses del ítem que registran ejecución
  importada; el proyectado a la fecha usa los mismos meses, para que la
  diferencia y el porcentaje comparen exactamente lo mismo.
- Todos los totales por programa son la suma de los ítems del programa, de
  modo que el nivel de detalle nunca contradice el resumen.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from staffing_simulator.domain.models import BudgetItem, ExecutionRecord, OtherExpense, Program
from staffing_simulator.domain.projection import Dimension, Projection
from staffing_simulator.domain.units import safe_ratio


@dataclass(frozen=True)
class ItemLine:
    """Un ítem presupuestario con su planificación mensual y su ejecución."""

    item: BudgetItem
    planned_hr: tuple[int, ...]
    planned_other: tuple[int, ...]
    executed: tuple[int | None, ...]

    @property
    def planned_monthly(self) -> tuple[int, ...]:
        return tuple(hr + other for hr, other in zip(self.planned_hr, self.planned_other, strict=True))

    @property
    def assigned(self) -> int:
        return self.item.amount

    @property
    def planned_hr_total(self) -> int:
        return sum(self.planned_hr)

    @property
    def planned_other_total(self) -> int:
        return sum(self.planned_other)

    @property
    def planned_total(self) -> int:
        return self.planned_hr_total + self.planned_other_total

    @property
    def balance(self) -> int:
        """Asignado - planificado (positivo = holgura, negativo = déficit)."""
        return self.assigned - self.planned_total

    @property
    def used_share(self) -> Decimal | None:
        return safe_ratio(self.planned_total, self.assigned)

    @property
    def has_execution(self) -> bool:
        return any(value is not None for value in self.executed)

    @property
    def executed_to_date(self) -> int:
        return sum(value for value in self.executed if value is not None)

    @property
    def projected_to_date(self) -> int:
        """Planificado de los meses que registran ejecución (el mismo alcance que `executed_to_date`)."""
        return sum(
            planned
            for planned, executed in zip(self.planned_monthly, self.executed, strict=True)
            if executed is not None
        )

    @property
    def execution_difference(self) -> int | None:
        return None if not self.has_execution else self.executed_to_date - self.projected_to_date

    @property
    def execution_share(self) -> Decimal | None:
        """Ejecutado / asignado del ítem (porcentaje de ejecución presupuestaria)."""
        return safe_ratio(self.executed_to_date, self.assigned) if self.assigned else None


@dataclass(frozen=True)
class ProgramLine:
    """Un programa con la suma de sus ítems."""

    program: Program
    assigned: int
    planned_hr: int
    planned_other: int
    executed_to_date: int
    projected_to_date: int
    has_execution: bool

    @property
    def planned_total(self) -> int:
        return self.planned_hr + self.planned_other

    @property
    def balance(self) -> int:
        return self.assigned - self.planned_total

    @property
    def used_share(self) -> Decimal | None:
        return safe_ratio(self.planned_total, self.assigned)

    @property
    def execution_difference(self) -> int | None:
        return None if not self.has_execution else self.executed_to_date - self.projected_to_date

    @property
    def execution_difference_share(self) -> Decimal | None:
        difference = self.execution_difference
        return None if difference is None else safe_ratio(difference, self.projected_to_date)


@dataclass(frozen=True)
class ItemReport:
    """Asignado, planificado y ejecutado de todos los ítems de los programas de un escenario."""

    year: int
    lines: tuple[ItemLine, ...]

    @property
    def total_assigned(self) -> int:
        return sum(line.assigned for line in self.lines)

    @property
    def total_planned_hr(self) -> int:
        return sum(line.planned_hr_total for line in self.lines)

    @property
    def total_planned_other(self) -> int:
        return sum(line.planned_other_total for line in self.lines)

    @property
    def total_planned(self) -> int:
        return self.total_planned_hr + self.total_planned_other

    @property
    def total_balance(self) -> int:
        return self.total_assigned - self.total_planned

    @property
    def total_executed_to_date(self) -> int:
        return sum(line.executed_to_date for line in self.lines)

    @property
    def total_projected_to_date(self) -> int:
        return sum(line.projected_to_date for line in self.lines)

    def by_program(self) -> tuple[ProgramLine, ...]:
        """Suma de los ítems agrupados por programa, ordenados por nombre."""
        programs: dict[int, Program] = {}
        totals: dict[int, dict[str, int]] = defaultdict(
            lambda: dict.fromkeys(("assigned", "hr", "other", "exec", "proj"), 0)
        )
        has_execution: dict[int, bool] = defaultdict(bool)
        for line in self.lines:
            key = line.item.program.id
            programs[key] = line.item.program
            bucket = totals[key]
            bucket["assigned"] += line.assigned
            bucket["hr"] += line.planned_hr_total
            bucket["other"] += line.planned_other_total
            bucket["exec"] += line.executed_to_date
            bucket["proj"] += line.projected_to_date
            has_execution[key] = has_execution[key] or line.has_execution
        return tuple(
            ProgramLine(
                program=programs[key],
                assigned=bucket["assigned"],
                planned_hr=bucket["hr"],
                planned_other=bucket["other"],
                executed_to_date=bucket["exec"],
                projected_to_date=bucket["proj"],
                has_execution=has_execution[key],
            )
            for key, bucket in sorted(totals.items(), key=lambda entry: programs[entry[0]].name)
        )


def _other_expense_totals(expenses: Sequence[OtherExpense], year: int) -> dict[int, tuple[int, ...]]:
    totals: dict[int, list[int]] = {}
    for expense in expenses:
        bucket = totals.setdefault(expense.budget_item.id, [0] * 12)
        for index, amount in enumerate(expense.monthly_amounts(year)):
            bucket[index] += amount
    return {key: tuple(value) for key, value in totals.items()}


def _execution_totals(records: Sequence[ExecutionRecord], year: int) -> dict[int, list[int | None]]:
    totals: dict[int, list[int | None]] = {}
    for record in records:
        if record.year != year:
            continue
        bucket = totals.setdefault(record.budget_item_id, [None] * 12)
        bucket[record.month - 1] = (bucket[record.month - 1] or 0) + record.amount
    return totals


def item_report(
    items: Sequence[BudgetItem],
    projection: Projection,
    other_expenses: Sequence[OtherExpense] = (),
    execution: Sequence[ExecutionRecord] = (),
) -> ItemReport:
    """Construye el informe por ítem a partir de la proyección, los otros gastos y la ejecución."""
    year = projection.scenario.year
    hr_by_item = {row.key: row.monthly for row in projection.breakdown(Dimension.BUDGET_ITEM)}
    other_by_item = _other_expense_totals(other_expenses, year)
    executed_by_item = _execution_totals(execution, year)
    zero_hr = (0,) * 12
    zero_other = (0,) * 12
    no_execution: tuple[int | None, ...] = (None,) * 12
    lines = tuple(
        ItemLine(
            item=item,
            planned_hr=hr_by_item.get(item.id, zero_hr),
            planned_other=other_by_item.get(item.id, zero_other),
            executed=tuple(executed_by_item.get(item.id, list(no_execution))),
        )
        for item in items
    )
    return ItemReport(year=year, lines=lines)
