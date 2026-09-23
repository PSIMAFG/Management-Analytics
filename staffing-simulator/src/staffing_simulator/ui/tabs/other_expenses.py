"""Pestaña Otros gastos: arriendos, compras, insumos y otros gastos planificados del escenario."""

from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from staffing_simulator.domain.models import BudgetItem, BudgetItemType, OtherExpenseDraft, OtherExpenseType, Scenario
from staffing_simulator.errors import AppError
from staffing_simulator.services import Services
from staffing_simulator.ui import dialogs
from staffing_simulator.ui.formatting import format_clp, format_date
from staffing_simulator.ui.forms import FormDialog, MoneyEdit, combo, date_edit, edit_date, select_data, set_edit_date
from staffing_simulator.ui.presenters import other_expense_rows
from staffing_simulator.ui.widgets import Column, RecordTableModel, make_table_view, muted_label, selected_records

COLUMNS = (
    Column("description", "Descripción"),
    Column("program", "Programa"),
    Column("item", "Ítem"),
    Column("expense_type", "Tipo"),
    Column("start", "Inicio", format_date),
    Column("end_text", "Término"),
    Column("amount", "Monto", format_clp, numeric=True),
    Column("annual_total", "Total anual", format_clp, numeric=True),
)


class OtherExpensesTab(QWidget):
    changed = Signal(str)
    import_requested = Signal()
    template_requested = Signal()

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.scenario: Scenario | None = None
        self.program_id: int | None = None
        self.budget_items: list[BudgetItem] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.title_label = QLabel("Otros gastos", objectName="pageTitle")
        layout.addWidget(self.title_label)
        layout.addWidget(
            muted_label(
                "Gastos planificados que no son recurso humano: arriendos, compras, insumos, capacitación, "
                "movilización, etc. Cada uno se imputa a un ítem presupuestario de operación, inversión u otro."
            )
        )

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.add_button = QPushButton("Agregar gasto", objectName="primary")
        self.edit_button = QPushButton("Editar")
        self.duplicate_button = QPushButton("Duplicar")
        self.delete_button = QPushButton("Eliminar")
        for button in (self.add_button, self.edit_button, self.duplicate_button, self.delete_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.template_button = QPushButton("Plantilla Excel...")
        self.import_button = QPushButton("Importar Excel...")
        self.template_button.clicked.connect(self.template_requested)
        self.import_button.clicked.connect(self.import_requested)
        toolbar.addWidget(self.template_button)
        toolbar.addWidget(self.import_button)
        layout.addLayout(toolbar)

        self.model = RecordTableModel(COLUMNS, self)
        self.view = make_table_view(self.model, self, stretch_column=0)
        self.view.doubleClicked.connect(self._edit_current)
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        layout.addWidget(self.view, 1)
        self.summary_label = muted_label("", wrap=False)
        layout.addWidget(self.summary_label)

        self.add_button.clicked.connect(self.add_expense)
        self.edit_button.clicked.connect(self._edit_current)
        self.duplicate_button.clicked.connect(self.duplicate_expense)
        self.delete_button.clicked.connect(self._delete_selected)
        self._update_buttons()

    def set_scenario(self, scenario: Scenario | None, program_id: int | None = None) -> None:
        self.scenario = scenario
        self.program_id = program_id
        self.title_label.setText("Otros gastos" if scenario is None else f"Otros gastos de «{scenario.name}»")
        for button in (self.add_button, self.edit_button, self.duplicate_button, self.delete_button):
            button.setEnabled(scenario is not None)
        self.refresh()

    def refresh(self) -> None:
        if self.scenario is None:
            self.model.set_rows([])
            self.budget_items = []
            self.summary_label.setText("")
            return
        try:
            self.budget_items = self.services.financial.items(program_id=self.program_id, year=self.scenario.year)
            expenses = self.services.financial.other_expenses(self.scenario.id, self.program_id)
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        rows = other_expense_rows(expenses, self.scenario.year)
        self.model.set_rows(rows)
        total = sum(row["annual_total"] for row in rows)
        self.summary_label.setText(f"{len(rows)} gastos; total anual planificado: {format_clp(total)}.")
        self._update_buttons()

    def _selected(self) -> dict[str, Any] | None:
        records = selected_records(self.view, self.model)
        return records[0] if records else None

    def _update_buttons(self) -> None:
        has_selection = self._selected() is not None and self.scenario is not None
        self.edit_button.setEnabled(has_selection)
        self.duplicate_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def _edit_current(self) -> None:
        row = self._selected()
        if row is not None:
            self.edit_expense(row)

    def _delete_selected(self) -> None:
        row = self._selected()
        if row is None:
            return
        if not dialogs.ask_confirmation(
            self, f"Se eliminará el gasto «{row['description']}».", "Eliminar gasto", "Eliminar"
        ):
            return
        try:
            self.services.financial.delete_other_expense(row["id"])
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        self._after_change("Gasto eliminado")

    def _after_change(self, message: str) -> None:
        self.refresh()
        self.changed.emit(message)

    def expense_dialog(self, row: dict[str, Any] | None = None) -> FormDialog | None:
        scenario = self.scenario
        if scenario is None:
            return None
        non_hr = [item for item in self.budget_items if item.item_type is not BudgetItemType.HUMAN_RESOURCES]
        if not non_hr:
            dialogs.show_info(
                self,
                "El programa no tiene ítems de operación, inversión u otro en este año. Cárguelos primero en "
                "Estructura del programa.",
                "Sin ítems disponibles",
            )
            return None
        dialog = FormDialog("Otro gasto planificado" if row is None else "Editar gasto", self)
        description = QLineEdit()
        description.setMaxLength(200)
        item_combo = combo([(item.label, item.id) for item in non_hr])
        expense_type = combo([(item.label, item.value) for item in OtherExpenseType])
        start = date_edit(date(scenario.year, 1, 1))
        end_row = QHBoxLayout()
        open_end = QCheckBox("Sin término (hasta fin de año)")
        end = date_edit(date(scenario.year, 12, 31))
        end_row.addWidget(end, 1)
        end_row.addWidget(open_end)
        amount = MoneyEdit()
        if row is not None:
            description.setText(row["description"])
            select_data(item_combo, row["item_id"])
            select_data(expense_type, row["expense_type_value"])
            set_edit_date(start, row["start"])
            open_end.setChecked(row["end"] is None)
            if row["end"] is not None:
                set_edit_date(end, row["end"])
            amount.setText(format_clp(row["amount"]).replace("$ ", ""))
        else:
            open_end.setChecked(True)

        def sync_end() -> None:
            is_monthly = expense_type.currentData() == OtherExpenseType.MONTHLY.value
            dialog.form.setRowVisible(end_row_widget, is_monthly)

        dialog.add_row("Descripción", description)
        dialog.add_row("Ítem", item_combo)
        dialog.add_row("Tipo", expense_type)
        dialog.add_row("Inicio (o mes, si es único)", start)
        end_row_widget = end_row
        dialog.add_row("Término", end_row)
        dialog.add_row("Monto", amount)
        expense_type.currentIndexChanged.connect(sync_end)
        sync_end()

        def save() -> Any:
            draft = OtherExpenseDraft(
                scenario_id=scenario.id,
                budget_item_id=int(item_combo.currentData()),
                description=description.text(),
                expense_type=OtherExpenseType(expense_type.currentData()),
                start_date=edit_date(start),
                end_date=None if open_end.isChecked() else edit_date(end),
                amount=amount.amount(),
            )
            if row is None:
                return self.services.financial.add_other_expense(draft)
            return self.services.financial.update_other_expense(row["id"], draft)

        dialog.on_save(save)
        return dialog

    def add_expense(self) -> None:
        dialog = self.expense_dialog()
        if dialog is not None and dialogs.run_dialog(dialog):
            self._after_change("Gasto agregado")

    def edit_expense(self, row: dict[str, Any]) -> None:
        try:
            expense = self.services.financial.other_expense(row["id"])
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        full_row = {
            "id": expense.id,
            "description": expense.description,
            "item_id": expense.budget_item.id,
            "expense_type_value": expense.expense_type.value,
            "start": expense.start_date,
            "end": expense.end_date,
            "amount": expense.amount,
        }
        dialog = self.expense_dialog(full_row)
        if dialog is not None and dialogs.run_dialog(dialog):
            self._after_change("Gasto actualizado")

    def duplicate_expense(self) -> None:
        row = self._selected()
        if row is None:
            return
        try:
            self.services.financial.duplicate_other_expense(row["id"])
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        self._after_change("Gasto duplicado")
