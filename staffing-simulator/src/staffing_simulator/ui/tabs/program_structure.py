"""Pestaña Estructura del programa: monto total del convenio e ítems presupuestarios por programa y año.

Muestra la estructura financiera del año del escenario seleccionado (los
ítems presupuestarios están definidos por programa y año). El asignado, lo
planificado y lo ejecutado vienen del informe por ítem del escenario; el
monto total del convenio y la edición de ítems se guardan directamente con
los servicios, igual que en la pestaña Parámetros.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from staffing_simulator.domain.budget import ItemReport
from staffing_simulator.domain.models import BudgetItemDraft, BudgetItemType, Program
from staffing_simulator.errors import AppError
from staffing_simulator.services import Services
from staffing_simulator.ui import dialogs
from staffing_simulator.ui.formatting import format_clp, format_pct
from staffing_simulator.ui.forms import FormDialog, MoneyEdit, combo, date_edit, edit_date, select_data
from staffing_simulator.ui.presenters import item_report_rows
from staffing_simulator.ui.tabs.parameters import TablePage
from staffing_simulator.ui.widgets import Column, muted_label

ITEM_COLUMNS = (
    Column("item", "Ítem"),
    Column("item_type", "Tipo"),
    Column("assigned", "Asignado", format_clp, numeric=True),
    Column("planned_hr", "Recurso humano", format_clp, numeric=True),
    Column("planned_other", "Otros gastos", format_clp, numeric=True),
    Column("planned", "Planificado", format_clp, numeric=True),
    Column("balance", "Saldo", format_clp, numeric=True),
    Column("used_share", "% usado", format_pct, numeric=True),
    Column("executed_to_date", "Ejecutado a la fecha", format_clp, numeric=True),
)


class ProgramStructureTab(QWidget):
    changed = Signal(str)

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self._programs: list[Program] = []
        self._report: ItemReport | None = None
        self._year: int = date.today().year
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(QLabel("Programa"))
        self.program_combo = combo([])
        self.program_combo.currentIndexChanged.connect(self._fill_items)
        header.addWidget(self.program_combo)
        self.year_label = muted_label("", wrap=False)
        header.addWidget(self.year_label)
        header.addStretch(1)
        self.edit_total_button = QPushButton("Editar total del convenio...")
        self.edit_total_button.clicked.connect(self.edit_total)
        header.addWidget(self.edit_total_button)
        layout.addLayout(header)

        self.total_label = muted_label("", wrap=True)
        layout.addWidget(self.total_label)
        self.warning_label = QLabel("", objectName="warningLink")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        self.items_page = TablePage("", ITEM_COLUMNS, stretch_column=0)
        # La tabla de ítems tiene más columnas que las de Parámetros: no la limita el ancho máximo de TablePage.
        self.items_page.view.setMaximumWidth(16_777_215)
        self.items_page.add_button("Agregar ítem...", self.add_item, primary=True)
        self.items_page.add_button("Editar ítem...", self.edit_item, needs_selection=True)
        self.items_page.add_button("Eliminar ítem", self.delete_item, needs_selection=True)
        self.items_page.finish_toolbar()
        self.items_page.view.doubleClicked.connect(lambda _index: self.edit_item())
        layout.addWidget(self.items_page, 1)
        self.model = self.items_page.model

    # Carga

    def show_data(self, report: ItemReport, programs: list[Program], year: int) -> None:
        self._report = report
        self._year = year
        current = self.program_combo.currentData()
        self.program_combo.blockSignals(True)
        self.program_combo.clear()
        self._programs = programs
        for program in programs:
            self.program_combo.addItem(program.name, program.id)
        if not select_data(self.program_combo, current) and programs:
            self.program_combo.setCurrentIndex(0)
        self.program_combo.blockSignals(False)
        self.year_label.setText(f"Año {year}")
        self._fill_items()

    def clear(self, message: str) -> None:
        self._report = None
        self.items_page.set_rows([])
        self.total_label.setText("")
        self.warning_label.setText(message)

    def _fill_items(self) -> None:
        program_id = self.program_combo.currentData()
        if program_id is None or self._report is None:
            self.items_page.set_rows([])
            self.total_label.setText("")
            self.warning_label.setText("")
            return
        try:
            budgets = {item.program_id: item for item in self.services.financial.program_budgets(self._year)}
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        budget = budgets.get(int(program_id))
        rows = [
            {**row, "item": row["item_name"]}
            for row in item_report_rows(self._report)
            if row.get("program_id") == program_id
        ]
        item_total = sum(row["assigned"] for row in rows)
        if budget is None:
            self.total_label.setText(f"Sin monto total del convenio cargado para {self._year}.")
        else:
            self.total_label.setText(f"Monto total del convenio {self._year}: {format_clp(budget.amount)}.")
        if budget is not None and budget.amount != item_total:
            self.warning_label.setText(
                f"La suma de los ítems ({format_clp(item_total)}) no coincide con el total del convenio "
                f"({format_clp(budget.amount)})."
            )
        else:
            self.warning_label.setText("")
        self.items_page.set_rows(rows)

    # Acciones

    def _after_change(self, message: str) -> None:
        self.changed.emit(message)

    def edit_total(self) -> None:
        program_id = self.program_combo.currentData()
        if program_id is None:
            return
        year = self._year
        try:
            current = next(
                (item for item in self.services.financial.program_budgets(year) if item.program_id == program_id),
                None,
            )
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        dialog = FormDialog("Total del convenio", self, intro="Monto total anual del convenio, en pesos.")
        amount = MoneyEdit(current.amount if current else None)
        start = date_edit(current.start_date if current and current.start_date else date(year, 1, 1))
        end = date_edit(current.end_date if current and current.end_date else date(year, 12, 31))
        reference = QLineEdit(current.reference if current else "")
        reference.setMaxLength(200)
        reference.setPlaceholderText("Opcional: referencia (por ejemplo, número de resolución)")
        dialog.add_row("Monto total", amount)
        dialog.add_row("Vigencia desde", start)
        dialog.add_row("Vigencia hasta", end)
        dialog.add_row("Referencia", reference)
        dialog.on_save(
            lambda: self.services.financial.set_program_budget(
                int(program_id), year, amount.amount(), edit_date(start), edit_date(end), reference.text()
            )
        )
        if dialogs.run_dialog(dialog):
            self._after_change("Total del convenio guardado")

    def item_dialog(self, row: dict[str, Any] | None = None) -> FormDialog:
        dialog = FormDialog("Ítem presupuestario", self)
        code = QLineEdit()
        code.setMaxLength(20)
        code.setPlaceholderText("Código, por ejemplo RRHH-1")
        name = QLineEdit()
        name.setMaxLength(80)
        name.setPlaceholderText("Nombre del ítem")
        item_type = combo([(item.label, item.value) for item in BudgetItemType])
        amount = MoneyEdit()
        if row is not None:
            code.setText(row["code"])
            name.setText(row["name"])
            select_data(item_type, row["item_type"])
            amount.setText(format_clp(row["amount"]).replace("$ ", ""))
        dialog.add_row("Código", code)
        dialog.add_row("Nombre", name)
        dialog.add_row("Tipo", item_type)
        dialog.add_row("Monto asignado", amount)
        program_id = int(self.program_combo.currentData())
        year = self._year

        def save() -> None:
            draft = BudgetItemDraft(
                program_id=program_id,
                year=year,
                code=code.text(),
                name=name.text(),
                item_type=BudgetItemType(item_type.currentData()),
                amount=amount.amount(),
            )
            if row is None:
                self.services.financial.add_item(draft)
            else:
                self.services.financial.update_item(row["id"], draft)

        dialog.on_save(save)
        return dialog

    def add_item(self) -> None:
        if self.program_combo.currentData() is None:
            return
        if dialogs.run_dialog(self.item_dialog()):
            self._after_change("Ítem agregado")

    def edit_item(self) -> None:
        row = self.items_page.selected()
        if row is None:
            return
        try:
            item = self.services.financial.item(row["item_id"])
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        full_row = {
            "item_id": item.id,
            "code": item.code,
            "name": item.name,
            "item_type": item.item_type.value,
            "amount": item.amount,
        }
        if dialogs.run_dialog(self.item_dialog(full_row)):
            self._after_change("Ítem actualizado")

    def delete_item(self) -> None:
        row = self.items_page.selected()
        if row is None:
            return
        if not dialogs.ask_confirmation(
            self, f"Se eliminará el ítem «{row['item']}».", "Confirmar eliminación", "Eliminar"
        ):
            return
        try:
            self.services.financial.delete_item(row["item_id"])
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        self._after_change("Ítem eliminado")
