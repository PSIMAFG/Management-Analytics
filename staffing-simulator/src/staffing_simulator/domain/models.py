"""Entidades del dominio como dataclasses inmutables.

Las horas se guardan en minutos enteros y se exponen como `Decimal`; las
fechas son `date`; los montos son pesos enteros o `Decimal` exactos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from staffing_simulator.domain.units import minutes_to_hours

CATEGORIES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F")
CATEGORY_LABELS: dict[str, str] = {
    "A": "Médicos y profesionales afines",
    "B": "Otros profesionales",
    "C": "Técnicos de nivel superior",
    "D": "Técnicos",
    "E": "Administrativos",
    "F": "Auxiliares",
}
MONTHS: tuple[int, ...] = tuple(range(1, 13))
MONTH_LABELS: tuple[str, ...] = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
MONTH_NAMES: tuple[str, ...] = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


class CostMethod(StrEnum):
    """Forma de calcular el costo mensual de un tipo de contrato."""

    WEEKLY_FEE = "weekly_fee"
    HOURLY_FEE = "hourly_fee"
    SALARIED = "salaried"

    @property
    def label(self) -> str:
        return {
            CostMethod.WEEKLY_FEE: "Honorarios con jornada semanal",
            CostMethod.HOURLY_FEE: "Honorarios por horas",
            CostMethod.SALARIED: "Contrato dependiente",
        }[self]

    @property
    def uses_weekly_hours(self) -> bool:
        """Los honorarios semanales y los dependientes se pactan en horas semanales."""
        return self is not CostMethod.HOURLY_FEE


class PartialMonthMethod(StrEnum):
    """Tratamiento de los meses en que la posición está vigente solo una parte."""

    PROPORTIONAL = "proportional"
    FULL_MONTH = "full_month"

    @property
    def label(self) -> str:
        return {PartialMonthMethod.PROPORTIONAL: "Proporcional", PartialMonthMethod.FULL_MONTH: "Mes completo"}[self]


class RetentionBasis(StrEnum):
    """Año cuya tasa de retención se aplica a un mes de servicio."""

    SERVICE = "service"
    PAYMENT = "payment"

    @property
    def label(self) -> str:
        return {
            RetentionBasis.SERVICE: "Año del mes de servicio",
            RetentionBasis.PAYMENT: "Año del pago (mes siguiente)",
        }[self]


@dataclass(frozen=True)
class Site:
    id: int
    code: str
    name: str


@dataclass(frozen=True)
class Program:
    id: int
    code: str
    name: str


@dataclass(frozen=True)
class JobRole:
    """Cargo con su categoría de tarifa (A a F)."""

    id: int
    code: str
    name: str
    category: str


@dataclass(frozen=True)
class ContractType:
    """Tipo de contrato: honorarios (semanal u horas) o dependiente (plazo fijo o planta)."""

    id: int
    code: str
    name: str
    cost_method: CostMethod
    applies_retention: bool


@dataclass(frozen=True)
class Person:
    id: int
    full_name: str
    rut: str | None = None


@dataclass(frozen=True)
class ScenarioDraft:
    """Datos editables de un escenario (supuestos)."""

    name: str
    year: int
    description: str = ""
    partial_month_method: PartialMonthMethod = PartialMonthMethod.PROPORTIONAL
    expected_absence: Decimal = Decimal(0)
    base_scenario_id: int | None = None


@dataclass(frozen=True)
class Scenario:
    id: int
    name: str
    description: str
    year: int
    partial_month_method: PartialMonthMethod
    expected_absence: Decimal
    base_scenario_id: int | None
    created_at: datetime
    updated_at: datetime

    def to_draft(self) -> ScenarioDraft:
        return ScenarioDraft(
            name=self.name,
            year=self.year,
            description=self.description,
            partial_month_method=self.partial_month_method,
            expected_absence=self.expected_absence,
            base_scenario_id=self.base_scenario_id,
        )


class BudgetItemType(StrEnum):
    """Tipo de ítem presupuestario de un convenio."""

    HUMAN_RESOURCES = "human_resources"
    OPERATION = "operation"
    INVESTMENT = "investment"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            BudgetItemType.HUMAN_RESOURCES: "Recurso humano",
            BudgetItemType.OPERATION: "Operación",
            BudgetItemType.INVESTMENT: "Inversión",
            BudgetItemType.OTHER: "Otro",
        }[self]


@dataclass(frozen=True)
class BudgetItemDraft:
    """Datos editables de un ítem presupuestario."""

    program_id: int
    year: int
    code: str
    name: str
    item_type: BudgetItemType
    amount: int


@dataclass(frozen=True)
class BudgetItem:
    """Ítem presupuestario de un programa y año (por ejemplo, recurso humano o arriendo)."""

    id: int
    program: Program
    year: int
    code: str
    name: str
    item_type: BudgetItemType
    amount: int

    @property
    def label(self) -> str:
        return f"{self.program.name} - {self.name}"

    def to_draft(self) -> BudgetItemDraft:
        return BudgetItemDraft(
            program_id=self.program.id,
            year=self.year,
            code=self.code,
            name=self.name,
            item_type=self.item_type,
            amount=self.amount,
        )


@dataclass(frozen=True)
class PositionDraft:
    """Datos de una posición tal como los ingresa el usuario (horas en decimal).

    `budget_item_id` es el ítem de recurso humano del programa al que se imputa
    la posición; `grade` es el grado real de la persona (solo informativo, para
    contratos dependientes): el costo siempre usa el grado 15.
    """

    job_role_id: int
    contract_type_id: int
    site_id: int
    program_id: int
    budget_item_id: int | None
    start_date: date
    end_date: date | None = None
    person_id: int | None = None
    weekly_hours: Decimal | None = None
    monthly_hours: Decimal | None = None
    quantity: int = 1
    grade: int | None = None
    note: str = ""


@dataclass(frozen=True)
class Position:
    """Posición de un escenario: un cargo con contrato, lugar, jornada y vigencia.

    `quantity` > 1 representa posiciones idénticas. Si `person` es None, la
    posición es una vacante. Una misma persona puede tener varias posiciones
    concurrentes o sucesivas (tramos); cada una se costea por separado.
    """

    id: int
    scenario_id: int
    job_role: JobRole
    contract_type: ContractType
    site: Site
    program: Program
    budget_item: BudgetItem
    start_date: date
    end_date: date | None = None
    person: Person | None = None
    weekly_minutes: int | None = None
    monthly_minutes: int | None = None
    quantity: int = 1
    grade: int | None = None
    note: str = ""

    @property
    def weekly_hours(self) -> Decimal | None:
        return None if self.weekly_minutes is None else minutes_to_hours(self.weekly_minutes)

    @property
    def monthly_hours(self) -> Decimal | None:
        """Horas mensuales estimadas (solo contratos por horas)."""
        return None if self.monthly_minutes is None else minutes_to_hours(self.monthly_minutes)

    def weekly_equivalent_minutes(self, weeks_per_month: Decimal) -> Decimal:
        """Minutos semanales de una unidad de la posición.

        Los contratos por horas se convierten a su equivalente semanal con
        horas mensuales / semanas por mes (la misma convención del costeo).
        """
        if self.weekly_minutes is not None:
            return Decimal(self.weekly_minutes)
        if self.monthly_minutes is not None:
            return Decimal(self.monthly_minutes) / weeks_per_month
        return Decimal(0)

    @property
    def is_vacancy(self) -> bool:
        return self.person is None

    @property
    def holder_label(self) -> str:
        return "Vacante" if self.person is None else self.person.full_name

    def period_in_year(self, year: int) -> tuple[date, date] | None:
        """Tramo de vigencia dentro del año o None si no lo intersecta.

        Una fecha de término vacía significa vigencia hasta fin de año.
        """
        first, last = date(year, 1, 1), date(year, 12, 31)
        start = max(self.start_date, first)
        end = min(self.end_date or last, last)
        return (start, end) if start <= end else None

    def is_active_on(self, day: date) -> bool:
        return self.start_date <= day and (self.end_date is None or day <= self.end_date)

    def to_draft(self) -> PositionDraft:
        return PositionDraft(
            job_role_id=self.job_role.id,
            contract_type_id=self.contract_type.id,
            site_id=self.site.id,
            program_id=self.program.id,
            budget_item_id=self.budget_item.id,
            start_date=self.start_date,
            end_date=self.end_date,
            person_id=None if self.person is None else self.person.id,
            weekly_hours=self.weekly_hours,
            monthly_hours=self.monthly_hours,
            quantity=self.quantity,
            grade=self.grade,
            note=self.note,
        )


@dataclass(frozen=True)
class ProgramBudget:
    """Estructura financiera del convenio: monto total, vigencia y una referencia opcional."""

    program_id: int
    year: int
    amount: int
    start_date: date | None = None
    end_date: date | None = None
    reference: str = ""


class OtherExpenseType(StrEnum):
    """Un gasto planificado distinto de recurso humano: mensual recurrente o único."""

    MONTHLY = "monthly"
    ONE_TIME = "one_time"

    @property
    def label(self) -> str:
        return {OtherExpenseType.MONTHLY: "Mensual recurrente", OtherExpenseType.ONE_TIME: "Único"}[self]


@dataclass(frozen=True)
class OtherExpenseDraft:
    """Datos editables de un gasto planificado (arriendos, compras, insumos, capacitación...)."""

    scenario_id: int
    budget_item_id: int
    description: str
    expense_type: OtherExpenseType
    start_date: date
    end_date: date | None
    amount: int


@dataclass(frozen=True)
class OtherExpense:
    """Gasto planificado del escenario, imputado a un ítem presupuestario.

    Único: se paga íntegro en el mes de `start_date`. Mensual recurrente: se
    paga íntegro cada mes entre `start_date` y `end_date` (vacío = hasta fin
    de año).
    """

    id: int
    scenario_id: int
    budget_item: BudgetItem
    description: str
    expense_type: OtherExpenseType
    start_date: date
    end_date: date | None
    amount: int

    def to_draft(self) -> OtherExpenseDraft:
        return OtherExpenseDraft(
            scenario_id=self.scenario_id,
            budget_item_id=self.budget_item.id,
            description=self.description,
            expense_type=self.expense_type,
            start_date=self.start_date,
            end_date=self.end_date,
            amount=self.amount,
        )

    def _period_in_year(self, year: int) -> tuple[date, date] | None:
        first, last = date(year, 1, 1), date(year, 12, 31)
        start = max(self.start_date, first)
        end = min(self.end_date or last, last)
        return (start, end) if start <= end else None

    def monthly_amounts(self, year: int) -> tuple[int, ...]:
        """Los doce montos mensuales de `year` (0 en los meses sin gasto)."""
        if self.expense_type is OtherExpenseType.ONE_TIME:
            amounts = [0] * 12
            if self.start_date.year == year:
                amounts[self.start_date.month - 1] = self.amount
            return tuple(amounts)
        period = self._period_in_year(year)
        if period is None:
            return (0,) * 12
        amounts = []
        for month in MONTHS:
            month_start = date(year, month, 1)
            month_end = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
            overlaps = period[0] <= month_end and period[1] >= month_start
            amounts.append(self.amount if overlaps else 0)
        return tuple(amounts)


@dataclass(frozen=True)
class ExecutionRecord:
    """Monto ejecutado (pagado) por un ítem presupuestario en un mes."""

    budget_item_id: int
    year: int
    month: int
    amount: int


@dataclass(frozen=True)
class Catalog:
    """Listas de referencia para formularios e importaciones."""

    sites: tuple[Site, ...] = field(default_factory=tuple)
    programs: tuple[Program, ...] = field(default_factory=tuple)
    job_roles: tuple[JobRole, ...] = field(default_factory=tuple)
    contract_types: tuple[ContractType, ...] = field(default_factory=tuple)
    persons: tuple[Person, ...] = field(default_factory=tuple)
