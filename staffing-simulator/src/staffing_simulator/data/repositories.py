"""Repositorios: traducen entre filas de SQLite y objetos del dominio.

Los repositorios no abren ni confirman transacciones: reciben una conexión
y el servicio que los usa decide el alcance con `db.transaction`.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from staffing_simulator.domain.models import (
    BudgetItem,
    BudgetItemDraft,
    BudgetItemType,
    Catalog,
    ContractType,
    CostMethod,
    ExecutionRecord,
    JobRole,
    OtherExpense,
    OtherExpenseDraft,
    OtherExpenseType,
    PartialMonthMethod,
    Person,
    Position,
    Program,
    ProgramBudget,
    RetentionBasis,
    Scenario,
    ScenarioDraft,
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
    SalaryAdjustment,
    SalaryScale,
    SalaryTable,
)
from staffing_simulator.domain.units import bp_to_fraction, fraction_to_bp, to_decimal
from staffing_simulator.domain.validation import ValidPosition
from staffing_simulator.errors import DataError, NotFoundError

SETTING_WEEKS_PER_MONTH = "weeks_per_month"
SETTING_RETENTION_BASIS = "retention_basis"
SETTING_FULL_TIME_WEEKLY_MINUTES = "full_time_weekly_minutes"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def iso_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def parse_date(text: str | None) -> date | None:
    return None if text is None else date.fromisoformat(text)


def _timestamp(value: datetime) -> str:
    return value.strftime(TIMESTAMP_FORMAT)


def _parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text, TIMESTAMP_FORMAT)


class CatalogRepository:
    """Sedes, programas, cargos, tipos de contrato y personas."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def sites(self) -> list[Site]:
        rows = self.conn.execute("SELECT id, code, name FROM site ORDER BY name")
        return [Site(row["id"], row["code"], row["name"]) for row in rows]

    def programs(self) -> list[Program]:
        rows = self.conn.execute("SELECT id, code, name FROM program ORDER BY code")
        return [Program(row["id"], row["code"], row["name"]) for row in rows]

    def job_roles(self) -> list[JobRole]:
        rows = self.conn.execute("SELECT id, code, name, category FROM job_role ORDER BY category, name")
        return [JobRole(row["id"], row["code"], row["name"], row["category"]) for row in rows]

    def contract_types(self) -> list[ContractType]:
        rows = self.conn.execute(f"SELECT {_CONTRACT_TYPE_COLUMNS} FROM contract_type ORDER BY id")
        return [_contract_type(row) for row in rows]

    def persons(self) -> list[Person]:
        rows = self.conn.execute("SELECT id, full_name, rut FROM person ORDER BY full_name")
        return [Person(row["id"], row["full_name"], row["rut"]) for row in rows]

    def catalog(self) -> Catalog:
        return Catalog(
            sites=tuple(self.sites()),
            programs=tuple(self.programs()),
            job_roles=tuple(self.job_roles()),
            contract_types=tuple(self.contract_types()),
            persons=tuple(self.persons()),
        )

    def program(self, program_id: int) -> Program:
        row = self.conn.execute("SELECT id, code, name FROM program WHERE id = ?", (program_id,)).fetchone()
        if row is None:
            raise NotFoundError("El programa seleccionado no existe.")
        return Program(row["id"], row["code"], row["name"])

    def contract_type(self, contract_type_id: int) -> ContractType:
        row = self.conn.execute(
            f"SELECT {_CONTRACT_TYPE_COLUMNS} FROM contract_type WHERE id = ?",
            (contract_type_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("El tipo de contrato seleccionado no existe.")
        return _contract_type(row)

    def job_role(self, job_role_id: int) -> JobRole:
        row = self.conn.execute("SELECT id, code, name, category FROM job_role WHERE id = ?", (job_role_id,)).fetchone()
        if row is None:
            raise NotFoundError("El cargo seleccionado no existe.")
        return JobRole(row["id"], row["code"], row["name"], row["category"])

    def person(self, person_id: int) -> Person:
        row = self.conn.execute("SELECT id, full_name, rut FROM person WHERE id = ?", (person_id,)).fetchone()
        if row is None:
            raise NotFoundError("La persona seleccionada no existe.")
        return Person(row["id"], row["full_name"], row["rut"])

    def exists(self, table: str, row_id: int) -> bool:
        if table not in {"site", "program", "job_role", "contract_type", "person"}:
            raise DataError(f"Tabla desconocida: {table}")
        return self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is not None

    def find_person_by_rut(self, rut: str) -> Person | None:
        row = self.conn.execute("SELECT id, full_name, rut FROM person WHERE rut = ?", (rut,)).fetchone()
        return None if row is None else Person(row["id"], row["full_name"], row["rut"])

    def add_site(self, code: str, name: str) -> Site:
        cursor = self.conn.execute("INSERT INTO site (code, name) VALUES (?, ?)", (code, name))
        return Site(_last_id(cursor), code, name)

    def add_program(self, code: str, name: str) -> Program:
        cursor = self.conn.execute("INSERT INTO program (code, name) VALUES (?, ?)", (code, name))
        return Program(_last_id(cursor), code, name)

    def add_job_role(self, code: str, name: str, category: str) -> JobRole:
        cursor = self.conn.execute(
            "INSERT INTO job_role (code, name, category) VALUES (?, ?, ?)", (code, name, category)
        )
        return JobRole(_last_id(cursor), code, name, category)

    def add_contract_type(self, code: str, name: str, method: CostMethod, applies_retention: bool) -> ContractType:
        cursor = self.conn.execute(
            "INSERT INTO contract_type (code, name, cost_method, applies_retention) VALUES (?, ?, ?, ?)",
            (code, name, method.value, int(applies_retention)),
        )
        return ContractType(_last_id(cursor), code, name, method, applies_retention)

    def add_person(self, full_name: str, rut: str | None) -> Person:
        cursor = self.conn.execute("INSERT INTO person (full_name, rut) VALUES (?, ?)", (full_name, rut))
        return Person(_last_id(cursor), full_name, rut)

    def update_contract_type(self, contract_type_id: int, name: str, applies_retention: bool) -> None:
        self.conn.execute(
            "UPDATE contract_type SET name = ?, applies_retention = ? WHERE id = ?",
            (name, int(applies_retention), contract_type_id),
        )

    def update_job_role_category(self, job_role_id: int, category: str) -> None:
        self.conn.execute("UPDATE job_role SET category = ? WHERE id = ?", (category, job_role_id))


def _last_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise DataError("No se pudo obtener el identificador del registro creado.")
    return cursor.lastrowid


_CONTRACT_TYPE_COLUMNS = "id, code, name, cost_method, applies_retention"


def _contract_type(row: sqlite3.Row) -> ContractType:
    return ContractType(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        cost_method=CostMethod(row["cost_method"]),
        applies_retention=bool(row["applies_retention"]),
    )


class ParameterRepository:
    """Tarifas de honorarios, escala de sueldo, reajustes, retenciones, aportes y convenciones."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def category_rates(self) -> list[CategoryRate]:
        rows = self.conn.execute(
            "SELECT category, year, hourly_rate FROM rate WHERE category IS NOT NULL ORDER BY year, category"
        )
        return [CategoryRate(row["category"], row["year"], row["hourly_rate"]) for row in rows]

    def role_rates(self) -> list[RoleRate]:
        rows = self.conn.execute(
            "SELECT job_role_id, year, hourly_rate FROM rate WHERE job_role_id IS NOT NULL ORDER BY year, job_role_id"
        )
        return [RoleRate(row["job_role_id"], row["year"], row["hourly_rate"]) for row in rows]

    def set_category_rate(self, category: str, year: int, hourly_rate: int) -> None:
        self.conn.execute(
            "INSERT INTO rate (category, year, hourly_rate) VALUES (?, ?, ?) "
            "ON CONFLICT (category, year) DO UPDATE SET hourly_rate = excluded.hourly_rate",
            (category, year, hourly_rate),
        )

    def delete_category_rate(self, category: str, year: int) -> int:
        cursor = self.conn.execute("DELETE FROM rate WHERE category = ? AND year = ?", (category, year))
        return cursor.rowcount

    def set_role_rate(self, job_role_id: int, year: int, hourly_rate: int) -> None:
        self.conn.execute(
            "INSERT INTO rate (job_role_id, year, hourly_rate) VALUES (?, ?, ?) "
            "ON CONFLICT (job_role_id, year) DO UPDATE SET hourly_rate = excluded.hourly_rate",
            (job_role_id, year, hourly_rate),
        )

    def delete_role_rate(self, job_role_id: int, year: int) -> int:
        cursor = self.conn.execute("DELETE FROM rate WHERE job_role_id = ? AND year = ?", (job_role_id, year))
        return cursor.rowcount

    def salary_scales(self) -> list[SalaryScale]:
        rows = self.conn.execute(
            "SELECT category, valid_from, monthly_amount FROM salary_scale ORDER BY category, valid_from"
        )
        return [
            SalaryScale(row["category"], date.fromisoformat(row["valid_from"]), row["monthly_amount"]) for row in rows
        ]

    def set_salary_scale(self, category: str, valid_from: date, monthly_amount: int) -> None:
        self.conn.execute(
            "INSERT INTO salary_scale (category, valid_from, monthly_amount) VALUES (?, ?, ?) "
            "ON CONFLICT (category, valid_from) DO UPDATE SET monthly_amount = excluded.monthly_amount",
            (category, valid_from.isoformat(), monthly_amount),
        )

    def delete_salary_scale(self, category: str, valid_from: date) -> int:
        cursor = self.conn.execute(
            "DELETE FROM salary_scale WHERE category = ? AND valid_from = ?", (category, valid_from.isoformat())
        )
        return cursor.rowcount

    def salary_adjustments(self) -> list[SalaryAdjustment]:
        rows = self.conn.execute(
            "SELECT valid_from, percent_bp, description FROM salary_adjustment ORDER BY valid_from"
        )
        return [
            SalaryAdjustment(
                date.fromisoformat(row["valid_from"]), bp_to_fraction(row["percent_bp"]), row["description"]
            )
            for row in rows
        ]

    def set_salary_adjustment(self, valid_from: date, percent: Decimal, description: str) -> None:
        self.conn.execute(
            "INSERT INTO salary_adjustment (valid_from, percent_bp, description) VALUES (?, ?, ?) "
            "ON CONFLICT (valid_from) DO UPDATE SET percent_bp = excluded.percent_bp, "
            "description = excluded.description",
            (valid_from.isoformat(), fraction_to_bp(percent), description),
        )

    def delete_salary_adjustment(self, valid_from: date) -> int:
        cursor = self.conn.execute("DELETE FROM salary_adjustment WHERE valid_from = ?", (valid_from.isoformat(),))
        return cursor.rowcount

    def retention_rates(self) -> list[RetentionRate]:
        rows = self.conn.execute("SELECT year, rate_bp FROM retention_rate ORDER BY year")
        return [RetentionRate(row["year"], bp_to_fraction(row["rate_bp"])) for row in rows]

    def set_retention_rate(self, year: int, rate: Decimal) -> None:
        self.conn.execute(
            "INSERT INTO retention_rate (year, rate_bp) VALUES (?, ?) "
            "ON CONFLICT (year) DO UPDATE SET rate_bp = excluded.rate_bp",
            (year, fraction_to_bp(rate)),
        )

    def delete_retention_rate(self, year: int) -> int:
        return self.conn.execute("DELETE FROM retention_rate WHERE year = ?", (year,)).rowcount

    def contributions(self) -> list[EmployerContribution]:
        rows = self.conn.execute(
            "SELECT contract_type_id, year, rate_bp FROM employer_contribution ORDER BY contract_type_id, year"
        )
        return [
            EmployerContribution(row["contract_type_id"], row["year"], bp_to_fraction(row["rate_bp"])) for row in rows
        ]

    def set_contribution(self, contract_type_id: int, year: int, rate: Decimal) -> None:
        self.conn.execute(
            "INSERT INTO employer_contribution (contract_type_id, year, rate_bp) VALUES (?, ?, ?) "
            "ON CONFLICT (contract_type_id, year) DO UPDATE SET rate_bp = excluded.rate_bp",
            (contract_type_id, year, fraction_to_bp(rate)),
        )

    def delete_contribution(self, contract_type_id: int, year: int) -> int:
        cursor = self.conn.execute(
            "DELETE FROM employer_contribution WHERE contract_type_id = ? AND year = ?", (contract_type_id, year)
        )
        return cursor.rowcount

    def settings(self) -> CostSettings:
        values = {row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM setting")}
        defaults = CostSettings()
        weeks = values.get(SETTING_WEEKS_PER_MONTH)
        basis = values.get(SETTING_RETENTION_BASIS)
        full_time = values.get(SETTING_FULL_TIME_WEEKLY_MINUTES)
        return CostSettings(
            weeks_per_month=defaults.weeks_per_month if weeks is None else to_decimal(weeks),
            retention_basis=defaults.retention_basis if basis is None else RetentionBasis(basis),
            full_time_weekly_minutes=defaults.full_time_weekly_minutes if full_time is None else int(full_time),
        )

    def save_settings(self, settings: CostSettings) -> None:
        for key, value in (
            (SETTING_WEEKS_PER_MONTH, f"{settings.weeks_per_month.normalize():f}"),
            (SETTING_RETENTION_BASIS, settings.retention_basis.value),
            (SETTING_FULL_TIME_WEEKLY_MINUTES, str(settings.full_time_weekly_minutes)),
        ):
            self.conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def cost_parameters(self) -> CostParameters:
        return CostParameters(
            rates=RateTable.from_records(self.category_rates(), self.role_rates()),
            retention=RetentionTable(tuple(self.retention_rates())),
            contributions=ContributionTable.from_records(self.contributions()),
            salary=SalaryTable(tuple(self.salary_scales()), tuple(self.salary_adjustments())),
            settings=self.settings(),
        )


class FinancialRepository:
    """Estructura financiera del convenio: total por programa, ítems y otros gastos."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def program_budgets(self, year: int | None = None) -> list[ProgramBudget]:
        query = "SELECT program_id, year, amount, start_date, end_date, reference FROM program_budget"
        params: tuple[int, ...] = ()
        if year is not None:
            query += " WHERE year = ?"
            params = (year,)
        rows = self.conn.execute(query + " ORDER BY year, program_id", params)
        return [
            ProgramBudget(
                row["program_id"],
                row["year"],
                row["amount"],
                parse_date(row["start_date"]),
                parse_date(row["end_date"]),
                row["reference"],
            )
            for row in rows
        ]

    def set_program_budget(self, budget: ProgramBudget) -> None:
        self.conn.execute(
            "INSERT INTO program_budget (program_id, year, amount, start_date, end_date, reference) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (program_id, year) DO UPDATE SET amount = excluded.amount, start_date = excluded.start_date, "
            "end_date = excluded.end_date, reference = excluded.reference",
            (
                budget.program_id,
                budget.year,
                budget.amount,
                iso_date(budget.start_date),
                iso_date(budget.end_date),
                budget.reference,
            ),
        )

    def delete_program_budget(self, program_id: int, year: int) -> int:
        return self.conn.execute(
            "DELETE FROM program_budget WHERE program_id = ? AND year = ?", (program_id, year)
        ).rowcount

    def _item(self, row: sqlite3.Row, programs: dict[int, Program]) -> BudgetItem:
        return BudgetItem(
            id=row["id"],
            program=programs[row["program_id"]],
            year=row["year"],
            code=row["code"],
            name=row["name"],
            item_type=BudgetItemType(row["item_type"]),
            amount=row["amount"],
        )

    def items(self, program_id: int | None = None, year: int | None = None) -> list[BudgetItem]:
        query = "SELECT id, program_id, year, code, name, item_type, amount FROM budget_item"
        clauses, params = [], []
        if program_id is not None:
            clauses.append("program_id = ?")
            params.append(program_id)
        if year is not None:
            clauses.append("year = ?")
            params.append(year)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(query + " ORDER BY program_id, code", params).fetchall()
        programs = {program.id: program for program in CatalogRepository(self.conn).programs()}
        return [self._item(row, programs) for row in rows]

    def item(self, item_id: int) -> BudgetItem:
        row = self.conn.execute(
            "SELECT id, program_id, year, code, name, item_type, amount FROM budget_item WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("El ítem presupuestario seleccionado no existe.")
        programs = {program.id: program for program in CatalogRepository(self.conn).programs()}
        return self._item(row, programs)

    def default_human_resources_item(self, program_id: int, year: int) -> BudgetItem | None:
        """El primer ítem de recurso humano del programa y año, para imputar posiciones por defecto."""
        row = self.conn.execute(
            "SELECT id FROM budget_item WHERE program_id = ? AND year = ? AND item_type = 'human_resources' "
            "ORDER BY code LIMIT 1",
            (program_id, year),
        ).fetchone()
        return None if row is None else self.item(row["id"])

    def add_item(self, draft: BudgetItemDraft) -> BudgetItem:
        cursor = self.conn.execute(
            "INSERT INTO budget_item (program_id, year, code, name, item_type, amount) VALUES (?, ?, ?, ?, ?, ?)",
            (draft.program_id, draft.year, draft.code, draft.name, draft.item_type.value, draft.amount),
        )
        return self.item(_last_id(cursor))

    def update_item(self, item_id: int, draft: BudgetItemDraft) -> BudgetItem:
        self.conn.execute(
            "UPDATE budget_item SET program_id = ?, year = ?, code = ?, name = ?, item_type = ?, amount = ? "
            "WHERE id = ?",
            (draft.program_id, draft.year, draft.code, draft.name, draft.item_type.value, draft.amount, item_id),
        )
        return self.item(item_id)

    def delete_item(self, item_id: int) -> int:
        return self.conn.execute("DELETE FROM budget_item WHERE id = ?", (item_id,)).rowcount

    def _expense(self, row: sqlite3.Row, items: dict[int, BudgetItem]) -> OtherExpense:
        return OtherExpense(
            id=row["id"],
            scenario_id=row["scenario_id"],
            budget_item=items[row["budget_item_id"]],
            description=row["description"],
            expense_type=OtherExpenseType(row["expense_type"]),
            start_date=date.fromisoformat(row["start_date"]),
            end_date=parse_date(row["end_date"]),
            amount=row["amount"],
        )

    def other_expenses(self, scenario_id: int) -> list[OtherExpense]:
        rows = self.conn.execute(
            "SELECT id, scenario_id, budget_item_id, description, expense_type, start_date, end_date, amount "
            "FROM other_expense WHERE scenario_id = ? ORDER BY id",
            (scenario_id,),
        ).fetchall()
        items = {item.id: item for item in self.items()}
        return [self._expense(row, items) for row in rows]

    def other_expense(self, expense_id: int) -> OtherExpense:
        row = self.conn.execute(
            "SELECT id, scenario_id, budget_item_id, description, expense_type, start_date, end_date, amount "
            "FROM other_expense WHERE id = ?",
            (expense_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("El gasto seleccionado no existe. Actualice la lista de otros gastos.")
        items = {item.id: item for item in self.items()}
        return self._expense(row, items)

    def add_other_expense(self, draft: OtherExpenseDraft) -> OtherExpense:
        cursor = self.conn.execute(
            "INSERT INTO other_expense (scenario_id, budget_item_id, description, expense_type, start_date, "
            "end_date, amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                draft.scenario_id,
                draft.budget_item_id,
                draft.description,
                draft.expense_type.value,
                iso_date(draft.start_date),
                iso_date(draft.end_date),
                draft.amount,
            ),
        )
        return self.other_expense(_last_id(cursor))

    def update_other_expense(self, expense_id: int, draft: OtherExpenseDraft) -> OtherExpense:
        self.conn.execute(
            "UPDATE other_expense SET budget_item_id = ?, description = ?, expense_type = ?, start_date = ?, "
            "end_date = ?, amount = ? WHERE id = ?",
            (
                draft.budget_item_id,
                draft.description,
                draft.expense_type.value,
                iso_date(draft.start_date),
                iso_date(draft.end_date),
                draft.amount,
                expense_id,
            ),
        )
        return self.other_expense(expense_id)

    def delete_other_expense(self, expense_id: int) -> int:
        return self.conn.execute("DELETE FROM other_expense WHERE id = ?", (expense_id,)).rowcount

    def copy_other_expenses(self, source_id: int, target_id: int) -> int:
        """Copia profunda de los otros gastos de un escenario a otro (para duplicar escenarios)."""
        cursor = self.conn.execute(
            "INSERT INTO other_expense (scenario_id, budget_item_id, description, expense_type, start_date, "
            "end_date, amount) "
            "SELECT ?, budget_item_id, description, expense_type, start_date, end_date, amount "
            "FROM other_expense WHERE scenario_id = ? ORDER BY id",
            (target_id, source_id),
        )
        return cursor.rowcount


_POSITION_SELECT = """
SELECT p.id, p.scenario_id, p.weekly_minutes, p.monthly_minutes, p.quantity, p.start_date, p.end_date,
       p.grade, p.note,
       r.id AS role_id, r.code AS role_code, r.name AS role_name, r.category AS role_category,
       c.id AS ct_id, c.code AS ct_code, c.name AS ct_name, c.cost_method AS ct_method,
       c.applies_retention AS ct_retention,
       s.id AS site_id, s.code AS site_code, s.name AS site_name,
       g.id AS program_id, g.code AS program_code, g.name AS program_name,
       i.id AS item_id, i.year AS item_year, i.code AS item_code, i.name AS item_name,
       i.item_type AS item_type, i.amount AS item_amount,
       e.id AS person_id, e.full_name AS person_name, e.rut AS person_rut
FROM position p
JOIN job_role r ON r.id = p.job_role_id
JOIN contract_type c ON c.id = p.contract_type_id
JOIN site s ON s.id = p.site_id
JOIN program g ON g.id = p.program_id
JOIN budget_item i ON i.id = p.budget_item_id
LEFT JOIN person e ON e.id = p.person_id
"""


def _position(row: sqlite3.Row) -> Position:
    person = None
    if row["person_id"] is not None:
        person = Person(row["person_id"], row["person_name"], row["person_rut"])
    program = Program(row["program_id"], row["program_code"], row["program_name"])
    budget_item = BudgetItem(
        id=row["item_id"],
        program=program,
        year=row["item_year"],
        code=row["item_code"],
        name=row["item_name"],
        item_type=BudgetItemType(row["item_type"]),
        amount=row["item_amount"],
    )
    return Position(
        id=row["id"],
        scenario_id=row["scenario_id"],
        job_role=JobRole(row["role_id"], row["role_code"], row["role_name"], row["role_category"]),
        contract_type=ContractType(
            row["ct_id"], row["ct_code"], row["ct_name"], CostMethod(row["ct_method"]), bool(row["ct_retention"])
        ),
        site=Site(row["site_id"], row["site_code"], row["site_name"]),
        program=program,
        budget_item=budget_item,
        start_date=date.fromisoformat(row["start_date"]),
        end_date=parse_date(row["end_date"]),
        person=person,
        weekly_minutes=row["weekly_minutes"],
        monthly_minutes=row["monthly_minutes"],
        quantity=row["quantity"],
        grade=row["grade"],
        note=row["note"],
    )


def _scenario(row: sqlite3.Row) -> Scenario:
    return Scenario(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        year=row["year"],
        partial_month_method=PartialMonthMethod(row["partial_month_method"]),
        expected_absence=bp_to_fraction(row["expected_absence_bp"]),
        base_scenario_id=row["base_scenario_id"],
        created_at=_parse_timestamp(row["created_at"]),
        updated_at=_parse_timestamp(row["updated_at"]),
    )


class ScenarioRepository:
    """Escenarios y sus posiciones."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_all(self) -> list[Scenario]:
        rows = self.conn.execute("SELECT * FROM scenario ORDER BY id")
        return [_scenario(row) for row in rows]

    def get(self, scenario_id: int) -> Scenario:
        row = self.conn.execute("SELECT * FROM scenario WHERE id = ?", (scenario_id,)).fetchone()
        if row is None:
            raise NotFoundError("El escenario seleccionado no existe. Actualice la lista de escenarios.")
        return _scenario(row)

    def name_taken(self, name: str, exclude_id: int | None = None) -> bool:
        row = self.conn.execute(
            "SELECT id FROM scenario WHERE name = ? COLLATE NOCASE AND id IS NOT ?", (name, exclude_id)
        ).fetchone()
        return row is not None

    def names(self) -> set[str]:
        return {row["name"].casefold() for row in self.conn.execute("SELECT name FROM scenario")}

    def insert(self, draft: ScenarioDraft, now: datetime) -> int:
        cursor = self.conn.execute(
            "INSERT INTO scenario (name, description, year, partial_month_method, expected_absence_bp, "
            "base_scenario_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.name,
                draft.description,
                draft.year,
                PartialMonthMethod(draft.partial_month_method).value,
                fraction_to_bp(draft.expected_absence),
                draft.base_scenario_id,
                _timestamp(now),
                _timestamp(now),
            ),
        )
        return _last_id(cursor)

    def update(self, scenario_id: int, draft: ScenarioDraft, now: datetime) -> None:
        self.conn.execute(
            "UPDATE scenario SET name = ?, description = ?, year = ?, partial_month_method = ?, "
            "expected_absence_bp = ?, base_scenario_id = ?, updated_at = ? WHERE id = ?",
            (
                draft.name,
                draft.description,
                draft.year,
                PartialMonthMethod(draft.partial_month_method).value,
                fraction_to_bp(draft.expected_absence),
                draft.base_scenario_id,
                _timestamp(now),
                scenario_id,
            ),
        )

    def touch(self, scenario_id: int, now: datetime) -> None:
        self.conn.execute("UPDATE scenario SET updated_at = ? WHERE id = ?", (_timestamp(now), scenario_id))

    def delete(self, scenario_id: int) -> None:
        self.conn.execute("DELETE FROM scenario WHERE id = ?", (scenario_id,))

    def base_chain(self, scenario_id: int) -> list[int]:
        """Escenarios base encadenados a partir de uno (para evitar ciclos)."""
        chain: list[int] = []
        current: int | None = scenario_id
        while current is not None and current not in chain:
            chain.append(current)
            row = self.conn.execute("SELECT base_scenario_id FROM scenario WHERE id = ?", (current,)).fetchone()
            current = None if row is None else row["base_scenario_id"]
        return chain

    def positions(self, scenario_id: int) -> list[Position]:
        rows = self.conn.execute(_POSITION_SELECT + " WHERE p.scenario_id = ? ORDER BY p.id", (scenario_id,))
        return [_position(row) for row in rows]

    def position(self, position_id: int) -> Position:
        row = self.conn.execute(_POSITION_SELECT + " WHERE p.id = ?", (position_id,)).fetchone()
        if row is None:
            raise NotFoundError("La posición seleccionada no existe. Actualice la lista de posiciones.")
        return _position(row)

    def insert_position(self, scenario_id: int, valid: ValidPosition) -> int:
        cursor = self.conn.execute(
            "INSERT INTO position (scenario_id, job_role_id, contract_type_id, site_id, program_id, "
            "budget_item_id, person_id, weekly_minutes, monthly_minutes, quantity, start_date, end_date, "
            "grade, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scenario_id, *self._position_values(valid)),
        )
        return _last_id(cursor)

    def update_position(self, position_id: int, valid: ValidPosition) -> None:
        self.conn.execute(
            "UPDATE position SET job_role_id = ?, contract_type_id = ?, site_id = ?, program_id = ?, "
            "budget_item_id = ?, person_id = ?, weekly_minutes = ?, monthly_minutes = ?, quantity = ?, "
            "start_date = ?, end_date = ?, grade = ?, note = ? WHERE id = ?",
            (*self._position_values(valid), position_id),
        )

    @staticmethod
    def _position_values(valid: ValidPosition) -> tuple[object, ...]:
        return (
            valid.job_role_id,
            valid.contract_type_id,
            valid.site_id,
            valid.program_id,
            valid.budget_item_id,
            valid.person_id,
            valid.weekly_minutes,
            valid.monthly_minutes,
            valid.quantity,
            iso_date(valid.start_date),
            iso_date(valid.end_date),
            valid.grade,
            valid.note,
        )

    def delete_position(self, position_id: int) -> int:
        return self.conn.execute("DELETE FROM position WHERE id = ?", (position_id,)).rowcount

    def copy_positions(self, source_id: int, target_id: int) -> int:
        """Copia profunda: cada posición del origen se inserta como fila nueva del destino."""
        cursor = self.conn.execute(
            "INSERT INTO position (scenario_id, job_role_id, contract_type_id, site_id, program_id, "
            "budget_item_id, person_id, weekly_minutes, monthly_minutes, quantity, start_date, end_date, "
            "grade, note) "
            "SELECT ?, job_role_id, contract_type_id, site_id, program_id, budget_item_id, person_id, "
            "weekly_minutes, monthly_minutes, quantity, start_date, end_date, grade, note "
            "FROM position WHERE scenario_id = ? ORDER BY id",
            (target_id, source_id),
        )
        return cursor.rowcount


class ExecutionRepository:
    """Montos ejecutados por ítem presupuestario y mes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def records(self, year: int | None = None) -> list[ExecutionRecord]:
        query = (
            "SELECT ex.budget_item_id, i.year, ex.month, ex.amount FROM execution ex "
            "JOIN budget_item i ON i.id = ex.budget_item_id"
        )
        params: tuple[int, ...] = ()
        if year is not None:
            query += " WHERE i.year = ?"
            params = (year,)
        rows = self.conn.execute(query + " ORDER BY i.year, ex.month, ex.budget_item_id", params)
        return [ExecutionRecord(row["budget_item_id"], row["year"], row["month"], row["amount"]) for row in rows]

    def upsert(self, budget_item_id: int, month: int, amount: int) -> None:
        self.conn.execute(
            "INSERT INTO execution (budget_item_id, month, amount) VALUES (?, ?, ?) "
            "ON CONFLICT (budget_item_id, month) DO UPDATE SET amount = excluded.amount",
            (budget_item_id, month, amount),
        )

    def delete(self, budget_item_id: int, month: int) -> int:
        cursor = self.conn.execute(
            "DELETE FROM execution WHERE budget_item_id = ? AND month = ?", (budget_item_id, month)
        )
        return cursor.rowcount

    def years(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT i.year FROM execution ex JOIN budget_item i ON i.id = ex.budget_item_id ORDER BY i.year"
        )
        return [row["year"] for row in rows]
