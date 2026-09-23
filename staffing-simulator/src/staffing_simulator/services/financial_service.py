"""Casos de uso de la estructura financiera: presupuesto del programa, ítems y otros gastos."""

from __future__ import annotations

import sqlite3
from datetime import date

from staffing_simulator.data.db import transaction
from staffing_simulator.data.repositories import CatalogRepository, FinancialRepository, ScenarioRepository
from staffing_simulator.domain.models import (
    BudgetItem,
    BudgetItemDraft,
    BudgetItemType,
    OtherExpense,
    OtherExpenseDraft,
    OtherExpenseType,
    ProgramBudget,
)
from staffing_simulator.domain.validation import validate_name, validate_year
from staffing_simulator.errors import NotFoundError, ValidationError
from staffing_simulator.services.common import ServiceBase

MAX_AMOUNT = 10**13
MAX_ITEM_CODE_LENGTH = 20


def _validate_amount(amount: int, label: str, *, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(amount, bool) or not isinstance(amount, int) or not minimum <= amount <= MAX_AMOUNT:
        raise ValidationError(
            f"{label} debe ser un monto entero de pesos {'mayor o igual a 0' if allow_zero else 'mayor que 0'}."
        )
    return amount


def _validate_item_code(code: str) -> str:
    clean = (code or "").strip().upper()
    if not clean:
        raise ValidationError("El código del ítem no puede quedar vacío.")
    if len(clean) > MAX_ITEM_CODE_LENGTH:
        raise ValidationError(f"El código del ítem admite como máximo {MAX_ITEM_CODE_LENGTH} caracteres.")
    return clean


class FinancialService(ServiceBase):
    """CRUD de la estructura financiera del convenio: presupuesto, ítems y otros gastos."""

    # Presupuesto total del convenio

    def program_budgets(self, year: int) -> list[ProgramBudget]:
        with self._connection() as conn:
            return FinancialRepository(conn).program_budgets(year)

    def set_program_budget(
        self,
        program_id: int,
        year: int,
        amount: int,
        start_date: date | None = None,
        end_date: date | None = None,
        reference: str = "",
    ) -> None:
        validate_year(year)
        _validate_amount(amount, "El monto del convenio")
        if start_date is not None and end_date is not None and end_date < start_date:
            raise ValidationError("La vigencia del convenio termina antes de empezar.")
        with self._connection() as conn, transaction(conn):
            if not CatalogRepository(conn).exists("program", program_id):
                raise NotFoundError("El programa seleccionado no existe.")
            FinancialRepository(conn).set_program_budget(
                ProgramBudget(program_id, year, amount, start_date, end_date, (reference or "").strip()[:200])
            )

    def delete_program_budget(self, program_id: int, year: int) -> None:
        with self._connection() as conn, transaction(conn):
            if FinancialRepository(conn).delete_program_budget(program_id, year) == 0:
                raise NotFoundError("El presupuesto indicado no existe.")

    # Ítems presupuestarios

    def items(self, program_id: int | None = None, year: int | None = None) -> list[BudgetItem]:
        with self._connection() as conn:
            return FinancialRepository(conn).items(program_id, year)

    def item(self, item_id: int) -> BudgetItem:
        with self._connection() as conn:
            return FinancialRepository(conn).item(item_id)

    def default_human_resources_item(self, program_id: int, year: int) -> BudgetItem | None:
        with self._connection() as conn:
            return FinancialRepository(conn).default_human_resources_item(program_id, year)

    def _validate_item_draft(self, draft: BudgetItemDraft) -> BudgetItemDraft:
        validate_year(draft.year)
        code = _validate_item_code(draft.code)
        name = validate_name(draft.name, "El nombre del ítem")
        _validate_amount(draft.amount, "El monto del ítem")
        try:
            item_type = BudgetItemType(draft.item_type)
        except ValueError as error:
            raise ValidationError("El tipo de ítem no es válido.") from error
        return BudgetItemDraft(draft.program_id, draft.year, code, name, item_type, draft.amount)

    def add_item(self, draft: BudgetItemDraft) -> BudgetItem:
        clean = self._validate_item_draft(draft)
        with self._connection() as conn, transaction(conn):
            if not CatalogRepository(conn).exists("program", clean.program_id):
                raise NotFoundError("El programa seleccionado no existe.")
            try:
                return FinancialRepository(conn).add_item(clean)
            except sqlite3.IntegrityError as error:
                raise ValidationError(
                    f"Ya existe un ítem con el código «{clean.code}» en ese programa y año."
                ) from error

    def update_item(self, item_id: int, draft: BudgetItemDraft) -> BudgetItem:
        clean = self._validate_item_draft(draft)
        with self._connection() as conn, transaction(conn):
            repo = FinancialRepository(conn)
            repo.item(item_id)
            if not CatalogRepository(conn).exists("program", clean.program_id):
                raise NotFoundError("El programa seleccionado no existe.")
            try:
                return repo.update_item(item_id, clean)
            except sqlite3.IntegrityError as error:
                raise ValidationError(
                    f"Ya existe un ítem con el código «{clean.code}» en ese programa y año."
                ) from error

    def delete_item(self, item_id: int) -> None:
        with self._connection() as conn, transaction(conn):
            try:
                if FinancialRepository(conn).delete_item(item_id) == 0:
                    raise NotFoundError("El ítem indicado no existe.")
            except sqlite3.IntegrityError as error:
                raise ValidationError(
                    "No se puede eliminar el ítem: hay posiciones, otros gastos o ejecución que lo usan."
                ) from error

    # Otros gastos planificados (por escenario)

    def other_expenses(self, scenario_id: int, program_id: int | None = None) -> list[OtherExpense]:
        """Otros gastos del escenario; con `program_id` solo los imputados a ese convenio."""
        with self._connection() as conn:
            expenses = FinancialRepository(conn).other_expenses(scenario_id)
        if program_id is None:
            return expenses
        return [expense for expense in expenses if expense.budget_item.program.id == program_id]

    def other_expense(self, expense_id: int) -> OtherExpense:
        with self._connection() as conn:
            return FinancialRepository(conn).other_expense(expense_id)

    def _validate_expense_draft(self, conn: sqlite3.Connection, draft: OtherExpenseDraft) -> OtherExpenseDraft:
        ScenarioRepository(conn).get(draft.scenario_id)
        item = FinancialRepository(conn).item(draft.budget_item_id)
        if item.item_type is BudgetItemType.HUMAN_RESOURCES:
            raise ValidationError(
                "Los otros gastos no se imputan a un ítem de recurso humano: use un ítem de operación, "
                "inversión u otro."
            )
        description = validate_name(draft.description, "La descripción del gasto")
        if not isinstance(draft.start_date, date):
            raise ValidationError("Indique una fecha válida.")
        try:
            expense_type = OtherExpenseType(draft.expense_type)
        except ValueError as error:
            raise ValidationError("El tipo de gasto debe ser 'mensual recurrente' o 'único'.") from error
        end_date = draft.end_date
        if expense_type is OtherExpenseType.ONE_TIME:
            end_date = None
        elif end_date is not None and end_date < draft.start_date:
            raise ValidationError("La fecha de término es anterior a la de inicio.")
        _validate_amount(draft.amount, "El monto del gasto", allow_zero=False)
        return OtherExpenseDraft(
            draft.scenario_id, draft.budget_item_id, description, expense_type, draft.start_date, end_date, draft.amount
        )

    def add_other_expense(self, draft: OtherExpenseDraft) -> OtherExpense:
        with self._connection() as conn, transaction(conn):
            clean = self._validate_expense_draft(conn, draft)
            return FinancialRepository(conn).add_other_expense(clean)

    def update_other_expense(self, expense_id: int, draft: OtherExpenseDraft) -> OtherExpense:
        with self._connection() as conn, transaction(conn):
            repo = FinancialRepository(conn)
            repo.other_expense(expense_id)
            clean = self._validate_expense_draft(conn, draft)
            return repo.update_other_expense(expense_id, clean)

    def delete_other_expense(self, expense_id: int) -> None:
        with self._connection() as conn, transaction(conn):
            if FinancialRepository(conn).delete_other_expense(expense_id) == 0:
                raise NotFoundError("El gasto indicado no existe.")

    def duplicate_other_expense(self, expense_id: int) -> OtherExpense:
        with self._connection() as conn, transaction(conn):
            repo = FinancialRepository(conn)
            source = repo.other_expense(expense_id)
            draft = OtherExpenseDraft(
                scenario_id=source.scenario_id,
                budget_item_id=source.budget_item.id,
                description=f"Copia de {source.description}"[:80],
                expense_type=source.expense_type,
                start_date=source.start_date,
                end_date=source.end_date,
                amount=source.amount,
            )
            return repo.add_other_expense(draft)
