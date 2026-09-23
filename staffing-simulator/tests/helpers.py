"""Constructores de objetos de dominio para los tests.

Las tarifas de los tests son números redondos (categoría B = 10.000 pesos)
para que los resultados esperados se puedan verificar a mano.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from staffing_simulator.domain.models import (
    BudgetItem,
    BudgetItemType,
    ContractType,
    CostMethod,
    JobRole,
    PartialMonthMethod,
    Person,
    Position,
    Program,
    Scenario,
    Site,
)
from staffing_simulator.domain.parameters import (
    CategoryRate,
    ContributionTable,
    CostParameters,
    CostSettings,
    EmployerContribution,
    RateTable,
    RetentionRate,
    RetentionTable,
    RoleRate,
    SalaryScale,
    SalaryTable,
)

YEAR = 2026
RATE_B = 10_000
RATE_A = 20_000
DOCTOR_RATE = 23_000
GRADE_15_B = 500_000
GRADE_15_A = 900_000

PROFESSIONAL = JobRole(1, "PSI", "Psicólogo", "B")
DOCTOR = JobRole(2, "MED", "Médico psiquiatra", "A")
GENERAL_DOCTOR = JobRole(3, "MEG", "Médico general", "A")
TECHNICIAN = JobRole(4, "TNS", "Técnico de nivel superior", "C")

WEEKLY_FEE = ContractType(1, "HON", "Honorarios", CostMethod.WEEKLY_FEE, True)
HOURLY_FEE = ContractType(2, "HPH", "Honorarios por horas", CostMethod.HOURLY_FEE, True)
SALARIED = ContractType(3, "PF", "Plazo fijo", CostMethod.SALARIED, False)

SITE = Site(1, "SC", "Sede Centro")
OTHER_SITE = Site(2, "SN", "Sede Norte")
PROGRAM = Program(1, "P1", "Programa Base")
OTHER_PROGRAM = Program(2, "P2", "Programa Comunitario")
BUDGET_ITEM = BudgetItem(1, PROGRAM, YEAR, "P1-RRHH", "Recurso humano", BudgetItemType.HUMAN_RESOURCES, 100_000_000)
OTHER_BUDGET_ITEM = BudgetItem(
    2, OTHER_PROGRAM, YEAR, "P2-RRHH", "Recurso humano", BudgetItemType.HUMAN_RESOURCES, 100_000_000
)
OPERATION_ITEM = BudgetItem(3, PROGRAM, YEAR, "P1-ARR", "Arriendo", BudgetItemType.OPERATION, 5_000_000)

RETENTION = {
    2020: "0.1075",
    2021: "0.115",
    2022: "0.1225",
    2023: "0.13",
    2024: "0.1375",
    2025: "0.145",
    2026: "0.1525",
    2027: "0.16",
    2028: "0.17",
}


def make_params(
    *,
    weeks_per_month: Decimal = Decimal(4),
    contribution: Decimal = Decimal("0.05"),
    years: tuple[int, ...] = (2025, 2026, 2027),
    settings: CostSettings | None = None,
) -> CostParameters:
    categories = [
        CategoryRate(category, year, RATE_A if category == "A" else RATE_B) for year in years for category in "AB"
    ]
    categories += [CategoryRate("C", year, 7_000) for year in years]
    return CostParameters(
        rates=RateTable.from_records(categories, [RoleRate(DOCTOR.id, year, DOCTOR_RATE) for year in years]),
        retention=RetentionTable(tuple(RetentionRate(year, Decimal(rate)) for year, rate in RETENTION.items())),
        contributions=ContributionTable.from_records(
            [EmployerContribution(SALARIED.id, year, contribution) for year in years]
        ),
        salary=SalaryTable(
            scales=(
                SalaryScale("A", date(2025, 1, 1), GRADE_15_A),
                SalaryScale("B", date(2025, 1, 1), GRADE_15_B),
                SalaryScale("C", date(2025, 1, 1), 400_000),
            ),
            adjustments=(),
        ),
        settings=settings or CostSettings(weeks_per_month=weeks_per_month),
    )


def make_scenario(
    *,
    year: int = YEAR,
    method: PartialMonthMethod = PartialMonthMethod.PROPORTIONAL,
    absence: Decimal = Decimal(0),
    scenario_id: int = 1,
    name: str = "Escenario de prueba",
) -> Scenario:
    stamp = datetime(2026, 1, 1, 8, 0, 0)
    return Scenario(scenario_id, name, "", year, method, absence, None, stamp, stamp)


_next_id = iter(range(1000, 10_000))


def make_position(
    *,
    weekly_hours: float | str | None = 44,
    monthly_hours: float | str | None = None,
    contract: ContractType = WEEKLY_FEE,
    role: JobRole = PROFESSIONAL,
    start: date = date(YEAR, 1, 1),
    end: date | None = None,
    person: Person | None = None,
    quantity: int = 1,
    site: Site = SITE,
    program: Program = PROGRAM,
    budget_item: BudgetItem = BUDGET_ITEM,
    grade: int | None = None,
    position_id: int | None = None,
) -> Position:
    if contract.cost_method is CostMethod.HOURLY_FEE:
        weekly_minutes = None
        monthly_minutes = int(Decimal(str(monthly_hours if monthly_hours is not None else 38)) * 60)
    else:
        weekly_minutes = int(Decimal(str(weekly_hours)) * 60)
        monthly_minutes = None
    return Position(
        id=position_id if position_id is not None else next(_next_id),
        scenario_id=1,
        job_role=role,
        contract_type=contract,
        site=site,
        program=program,
        budget_item=budget_item,
        start_date=start,
        end_date=end,
        person=person,
        weekly_minutes=weekly_minutes,
        monthly_minutes=monthly_minutes,
        quantity=quantity,
        grade=grade,
    )
