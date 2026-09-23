"""Pestaña Parámetros: tarifas, cargos, tipos de contrato y aportes, retenciones, jornada, presupuestos y convenciones.

Cada cambio se valida en los servicios; si se rechaza, el diálogo muestra el
motivo y queda abierto. Después de un cambio se recalculan todas las vistas.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from staffing_simulator.domain.models import (
    CATEGORIES,
    CATEGORY_LABELS,
    ContractType,
    CostMethod,
    JobRole,
    RetentionBasis,
)
from staffing_simulator.errors import AppError
from staffing_simulator.services import RateRow, Services
from staffing_simulator.ui import dialogs
from staffing_simulator.ui.formatting import format_clp, format_date, format_pct
from staffing_simulator.ui.forms import (
    FormDialog,
    combo,
    date_edit,
    decimal_spin,
    edit_date,
    int_spin,
    percent_spin,
    select_data,
    set_edit_date,
    spin_decimal,
    spin_fraction,
)
from staffing_simulator.ui.widgets import Column, RecordTableModel, make_table_view, muted_label, selected_records

log = logging.getLogger(__name__)

ALL_YEARS = "Todos"


def _yes_no(value: Any) -> str:
    return "Sí" if value else "No"


class TablePage(QWidget):
    """Página con una explicación, botones de acción y una tabla."""

    def __init__(
        self, intro: str, columns: Sequence[Column], parent: QWidget | None = None, *, stretch_column: int = 0
    ) -> None:
        super().__init__(parent)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(12, 10, 12, 10)
        self.layout_.setSpacing(8)
        if intro:
            self.layout_.addWidget(muted_label(intro))
        self.toolbar = QHBoxLayout()
        self.toolbar.setSpacing(6)
        self.layout_.addLayout(self.toolbar)
        self.model = RecordTableModel(columns, self)
        self.view = make_table_view(self.model, self, stretch_column=stretch_column)
        self.view.setMaximumWidth(900)
        self.layout_.addWidget(self.view, 1)
        self._selection_buttons: list[QPushButton] = []
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)

    def add_button(
        self, text: str, handler: Callable[[], None], *, primary: bool = False, needs_selection: bool = False
    ) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(lambda: handler())
        self.toolbar.addWidget(button)
        if needs_selection:
            self._selection_buttons.append(button)
            button.setEnabled(False)
        return button

    def finish_toolbar(self) -> None:
        self.toolbar.addStretch(1)

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.model.set_rows(rows)
        self._update_buttons()

    def selected(self) -> Any | None:
        records = selected_records(self.view, self.model)
        return records[0] if records else None

    def _update_buttons(self) -> None:
        has_selection = bool(self.view.selectionModel().selectedRows())
        for button in self._selection_buttons:
            button.setEnabled(has_selection)


class ParametersTab(QWidget):
    """Edición validada de los parámetros de costeo."""

    changed = Signal(str)

    def __init__(self, services: Services, year_provider: Callable[[], int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self._year = year_provider
        self._roles: dict[int, JobRole] = {}
        self._contract_types: dict[int, ContractType] = {}
        self._rate_rows: list[RateRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.pages = QTabWidget(self)
        self.pages.setObjectName("subTabs")
        self.pages.setDocumentMode(True)
        layout.addWidget(self.pages)

        self.rates_page = self._build_rates()
        self.roles_page = self._build_roles()
        self.contracts_page, self.contributions_page = self._build_contracts()
        self.retention_page = self._build_retention()
        self.salary_scale_page = self._build_salary_scale()
        self.salary_adjustment_page = self._build_salary_adjustments()
        self.settings_page = self._build_settings()

    def tables(self) -> list[TablePage]:
        return [
            self.rates_page,
            self.roles_page,
            self.contracts_page,
            self.contributions_page,
            self.retention_page,
            self.salary_scale_page,
            self.salary_adjustment_page,
        ]

    # Construcción de páginas

    def _build_rates(self) -> TablePage:
        page = TablePage(
            "Valor hora por categoría y año. Una tarifa específica de un cargo prevalece sobre la de su categoría. "
            "Si falta la tarifa del año de un escenario, el costeo se detiene con un aviso: nunca se asume costo 0.",
            [
                Column("year", "Año", str, numeric=True),
                Column("label", "Aplica a"),
                Column("kind", "Tipo"),
                Column("hourly_rate", "Valor hora", format_clp, numeric=True),
            ],
            stretch_column=1,
        )
        page.toolbar.addWidget(QLabel("Año"))
        self.rate_year_combo = QComboBox()
        self.rate_year_combo.currentIndexChanged.connect(self._fill_rates)
        page.toolbar.addWidget(self.rate_year_combo)
        page.toolbar.addSpacing(12)
        page.add_button("Agregar o modificar tarifa...", self.edit_rate, primary=True)
        page.add_button("Eliminar tarifa", self.delete_rate, needs_selection=True)
        page.finish_toolbar()
        page.view.doubleClicked.connect(lambda _index: self.edit_rate())
        self.pages.addTab(page, "Tarifas")
        return page

    def _build_roles(self) -> TablePage:
        page = TablePage(
            "Cada cargo usa la tarifa de su categoría (A a F), salvo que tenga una tarifa específica.",
            [
                Column("name", "Cargo"),
                Column("category", "Categoría"),
                Column("category_label", "Descripción de la categoría"),
            ],
            stretch_column=2,
        )
        page.add_button("Cambiar categoría...", self.edit_role_category, primary=True, needs_selection=True)
        page.finish_toolbar()
        page.view.doubleClicked.connect(lambda _index: self.edit_role_category())
        self.pages.addTab(page, "Cargos")
        return page

    def _build_contracts(self) -> tuple[TablePage, TablePage]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        contracts = TablePage(
            "Honorarios: el costo es el bruto y se informa la retención. Plazo fijo y planta: sueldo del grado "
            "15 más aporte del empleador; el ausentismo no reduce su costo.",
            [
                Column("name", "Tipo de contrato"),
                Column("method", "Forma de costeo"),
                Column("retention", "Aplica retención", _yes_no),
            ],
        )
        contracts.add_button("Editar tipo de contrato...", self.edit_contract_type, needs_selection=True)
        contracts.finish_toolbar()
        contracts.view.doubleClicked.connect(lambda _index: self.edit_contract_type())
        contributions = TablePage(
            "Aporte del empleador por tipo de contrato dependiente y año, como porcentaje de la remuneración bruta. "
            "Los valores de los datos de ejemplo son referenciales.",
            [
                Column("contract", "Tipo de contrato"),
                Column("year", "Año", str, numeric=True),
                Column("rate", "Aporte del empleador", lambda value: format_pct(value, 2), numeric=True),
            ],
        )
        contributions.add_button("Agregar o modificar aporte...", self.edit_contribution, primary=True)
        contributions.add_button("Eliminar aporte", self.delete_contribution, needs_selection=True)
        contributions.finish_toolbar()
        contributions.view.doubleClicked.connect(lambda _index: self.edit_contribution())
        layout.addWidget(contracts, 2)
        layout.addWidget(contributions, 3)
        self.pages.addTab(container, "Contratos y aportes")
        return contracts, contributions

    def _build_retention(self) -> TablePage:
        page = TablePage(
            "Tasa legal de retención de las boletas de honorarios. Cada tasa rige desde su año hasta la siguiente. "
            "La retención no cambia el costo para la organización: se informa como líquido estimado.",
            [
                Column("year", "Desde el año", str, numeric=True),
                Column("rate", "Tasa de retención", lambda value: format_pct(value, 2), numeric=True),
            ],
        )
        page.add_button("Agregar o modificar tasa...", self.edit_retention, primary=True)
        page.add_button("Eliminar tasa", self.delete_retention, needs_selection=True)
        page.finish_toolbar()
        page.view.doubleClicked.connect(lambda _index: self.edit_retention())
        self.pages.addTab(page, "Retención de honorarios")
        return page

    def _build_salary_scale(self) -> TablePage:
        page = TablePage(
            "Sueldo mensual del grado 15 por categoría, para la jornada completa, vigente desde una fecha. Es "
            "la base del costo de plazo fijo y planta; los reajustes del sector público se aplican sobre este monto.",
            [
                Column("category", "Categoría"),
                Column("category_label", "Descripción"),
                Column("valid_from", "Vigente desde", format_date),
                Column("amount", "Sueldo grado 15", format_clp, numeric=True),
            ],
            stretch_column=1,
        )
        page.add_button("Agregar o modificar escala...", self.edit_salary_scale, primary=True)
        page.add_button("Eliminar escala", self.delete_salary_scale, needs_selection=True)
        page.finish_toolbar()
        page.view.doubleClicked.connect(lambda _index: self.edit_salary_scale())
        self.pages.addTab(page, "Escala grado 15")
        return page

    def _build_salary_adjustments(self) -> TablePage:
        page = TablePage(
            "Reajustes del sector público: aplican solo a plazo fijo y planta, de forma multiplicativa, a los "
            "meses desde su fecha de vigencia, sobre escalas cargadas antes de esa fecha. Los valores de ejemplo "
            "son aproximados: revíselos.",
            [
                Column("valid_from", "Vigente desde", format_date),
                Column("percent", "Reajuste", lambda value: format_pct(value, 2), numeric=True),
                Column("description", "Descripción"),
            ],
            stretch_column=2,
        )
        page.add_button("Agregar o modificar reajuste...", self.edit_salary_adjustment, primary=True)
        page.add_button("Eliminar reajuste", self.delete_salary_adjustment, needs_selection=True)
        page.finish_toolbar()
        page.view.doubleClicked.connect(lambda _index: self.edit_salary_adjustment())
        self.pages.addTab(page, "Reajustes")
        return page

    def _build_settings(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        layout.addWidget(
            muted_label(
                "Semanas por mes: convención de los contratos para pasar de horas semanales a un monto mensual "
                "(bruto = valor hora x horas semanales x semanas por mes). Se usa 4 por defecto y no se mezcla con "
                "las horas del calendario.\nAño de la tasa de retención: con «año del pago», diciembre se paga en "
                "enero y usa la tasa del año siguiente.\nJornada completa: 44 horas semanales en atención primaria. "
                "Se usa para prorratear el sueldo del grado 15 según las horas de cada posición y para advertir "
                "cuando una persona suma más horas semanales que la jornada completa."
            )
        )
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.weeks_spin = decimal_spin(0.5, 5, 4, decimals=2, step=0.25)
        self.weeks_spin.setMaximumWidth(140)
        self.basis_combo = combo([(item.label, item.value) for item in RetentionBasis])
        self.basis_combo.setMaximumWidth(260)
        self.full_time_spin = decimal_spin(1, 60, 44, decimals=1, step=0.5, suffix=" h semanales")
        self.full_time_spin.setMaximumWidth(180)
        form.addRow("Semanas por mes", self.weeks_spin)
        form.addRow("Año de la tasa de retención", self.basis_combo)
        form.addRow("Jornada completa", self.full_time_spin)
        layout.addLayout(form)
        save = QPushButton("Guardar convenciones", objectName="primary")
        save.clicked.connect(lambda: self.save_settings())
        row = QHBoxLayout()
        row.addWidget(save)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.pages.addTab(page, "Convenciones")
        return page

    # Carga de datos

    def refresh(self) -> None:
        """Vuelve a leer todos los parámetros desde los servicios."""
        catalog = self.services.parameters.catalog()
        self._roles = {role.id: role for role in catalog.job_roles}
        self._contract_types = {item.id: item for item in catalog.contract_types}
        self._load_rates()
        self._load_roles(catalog.job_roles)
        self._load_contracts(catalog.contract_types)
        self._load_retention()
        self._load_salary_scale()
        self._load_salary_adjustments()
        self._load_settings()

    def _load_rates(self) -> None:
        self._rate_rows = self.services.parameters.rate_rows()
        years = sorted({row.year for row in self._rate_rows})
        current = self.rate_year_combo.currentData()
        first_time = self.rate_year_combo.count() == 0
        self.rate_year_combo.blockSignals(True)
        self.rate_year_combo.clear()
        self.rate_year_combo.addItem(ALL_YEARS, None)
        for year in years:
            self.rate_year_combo.addItem(str(year), year)
        if first_time or not select_data(self.rate_year_combo, current):
            select_data(self.rate_year_combo, self._year() if self._year() in years else None)
        self.rate_year_combo.blockSignals(False)
        self._fill_rates()

    def _load_roles(self, roles: Sequence[JobRole]) -> None:
        self.roles_page.set_rows(
            [
                {
                    "id": role.id,
                    "name": role.name,
                    "category": role.category,
                    "category_label": CATEGORY_LABELS.get(role.category, ""),
                }
                for role in roles
            ]
        )

    def _load_contracts(self, contract_types: Sequence[ContractType]) -> None:
        self.contracts_page.set_rows(
            [
                {
                    "id": item.id,
                    "name": item.name,
                    "method": item.cost_method.label,
                    "retention": item.applies_retention,
                    "salaried": item.cost_method is CostMethod.SALARIED,
                }
                for item in contract_types
            ]
        )
        self.contributions_page.set_rows(
            [
                {
                    "contract_type_id": item.contract_type_id,
                    "contract": self._contract_types[item.contract_type_id].name,
                    "year": item.year,
                    "rate": item.rate,
                }
                for item in self.services.parameters.contributions()
                if item.contract_type_id in self._contract_types
            ]
        )

    def _load_retention(self) -> None:
        self.retention_page.set_rows(
            [{"year": item.year, "rate": item.rate} for item in self.services.parameters.retention_rates()]
        )

    def _load_salary_scale(self) -> None:
        self.salary_scale_page.set_rows(
            [
                {
                    "category": item.category,
                    "category_label": CATEGORY_LABELS.get(item.category, ""),
                    "valid_from": item.valid_from,
                    "amount": item.monthly_amount,
                }
                for item in self.services.parameters.salary_scales()
            ]
        )

    def _load_salary_adjustments(self) -> None:
        self.salary_adjustment_page.set_rows(
            [
                {"valid_from": item.valid_from, "percent": item.percent, "description": item.description}
                for item in self.services.parameters.salary_adjustments()
            ]
        )

    def _load_settings(self) -> None:
        settings = self.services.parameters.settings()
        self.weeks_spin.setValue(float(settings.weeks_per_month))
        select_data(self.basis_combo, settings.retention_basis.value)
        self.full_time_spin.setValue(float(settings.full_time_weekly_hours))

    def _fill_rates(self) -> None:
        year = self.rate_year_combo.currentData()
        rows = [
            {
                "year": row.year,
                "label": row.label,
                "kind": "Específica del cargo" if row.is_role_specific else "Categoría",
                "hourly_rate": row.hourly_rate,
                "category": row.category,
                "job_role_id": row.job_role_id,
            }
            for row in self._rate_rows
            if year is None or row.year == year
        ]
        self.rates_page.set_rows(rows)

    def _fill_budgets(self) -> None:
        """Muestra los presupuestos del año elegido en el selector (informa el error sin propagarlo)."""
        self._budget_year = self.budget_year_spin.value()
        try:
            self._load_budgets()
        except AppError as error:
            dialogs.show_error(self, error.user_message)

    # Acciones

    def _run_form(self, dialog: FormDialog, message: str) -> bool:
        if not dialogs.run_dialog(dialog):
            return False
        self._after_change(message)
        return True

    def _after_change(self, message: str) -> None:
        try:
            self.refresh()
        except AppError as error:
            dialogs.show_error(self, error.user_message)
        self.changed.emit(message)

    def _confirm_and_run(self, question: str, action: Callable[[], None], message: str) -> None:
        if not dialogs.ask_confirmation(self, question, "Confirmar eliminación", "Eliminar"):
            return
        try:
            action()
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return
        self._after_change(message)

    def rate_dialog(self, row: dict[str, Any] | None = None) -> FormDialog:
        dialog = FormDialog(
            "Tarifa por hora",
            self,
            intro="Si ya existe una tarifa para el mismo año y destino, se reemplaza.",
        )
        targets: list[tuple[str, Any]] = [
            (f"Categoría {code}: {CATEGORY_LABELS[code]}", f"category:{code}") for code in CATEGORIES
        ]
        targets += [
            (f"Cargo {role.name} (tarifa específica)", f"role:{role.id}")
            for role in sorted(self._roles.values(), key=lambda item: item.name)
        ]
        target = combo(targets)
        year = int_spin(2000, 2100, self._year())
        amount = int_spin(1, 1_000_000, 8000, group=True)
        amount.setPrefix("$ ")
        if row is not None:
            key = f"role:{row['job_role_id']}" if row["job_role_id"] is not None else f"category:{row['category']}"
            select_data(target, key)
            year.setValue(row["year"])
            amount.setValue(row["hourly_rate"])
        dialog.add_row("Aplica a", target)
        dialog.add_row("Año", year)
        dialog.add_row("Valor hora", amount)

        def save() -> None:
            kind, _, value = str(target.currentData()).partition(":")
            if kind == "role":
                self.services.parameters.set_role_rate(int(value), year.value(), amount.value())
            else:
                self.services.parameters.set_category_rate(value, year.value(), amount.value())

        dialog.on_save(save)
        return dialog

    def edit_rate(self) -> None:
        self._run_form(self.rate_dialog(self.rates_page.selected()), "Tarifa guardada")

    def delete_rate(self) -> None:
        row = self.rates_page.selected()
        if row is None:
            return

        def action() -> None:
            if row["job_role_id"] is not None:
                self.services.parameters.delete_role_rate(row["job_role_id"], row["year"])
            else:
                self.services.parameters.delete_category_rate(row["category"], row["year"])

        self._confirm_and_run(
            f"Se eliminará la tarifa «{row['label']}» de {row['year']}. Los escenarios de ese año que la usen "
            "dejarán de poder costearse hasta que se cargue otra.",
            action,
            "Tarifa eliminada",
        )

    def role_dialog(self, row: dict[str, Any]) -> FormDialog:
        dialog = FormDialog("Categoría del cargo", self)
        name = QLabel(row["name"])
        category = combo([(f"{code}: {CATEGORY_LABELS[code]}", code) for code in CATEGORIES], row["category"])
        dialog.add_row("Cargo", name)
        dialog.add_row("Categoría", category)
        dialog.on_save(
            lambda: self.services.parameters.update_job_role_category(row["id"], str(category.currentData()))
        )
        return dialog

    def edit_role_category(self) -> None:
        row = self.roles_page.selected()
        if row is not None:
            self._run_form(self.role_dialog(row), "Categoría del cargo actualizada")

    def contract_dialog(self, row: dict[str, Any]) -> FormDialog:
        dialog = FormDialog("Tipo de contrato", self)
        name = QLineEdit(row["name"])
        name.setMaxLength(80)
        retention = QCheckBox("Aplica retención de honorarios")
        retention.setChecked(bool(row["retention"]))
        retention.setEnabled(not row["salaried"])
        if row["salaried"]:
            retention.setToolTip("Los contratos dependientes no tienen retención de honorarios.")
        dialog.add_row("Nombre", name)
        dialog.add_row("Forma de costeo", QLabel(row["method"]))
        dialog.add_row("", retention)
        dialog.on_save(
            lambda: self.services.parameters.update_contract_type(row["id"], name.text(), retention.isChecked())
        )
        return dialog

    def edit_contract_type(self) -> None:
        row = self.contracts_page.selected()
        if row is not None:
            self._run_form(self.contract_dialog(row), "Tipo de contrato actualizado")

    def contribution_dialog(self, row: dict[str, Any] | None = None) -> FormDialog:
        dialog = FormDialog("Aporte del empleador", self, intro="Solo aplica a contratos dependientes.")
        salaried = [
            (item.name, item.id) for item in self._contract_types.values() if item.cost_method is CostMethod.SALARIED
        ]
        contract = combo(salaried)
        year = int_spin(2000, 2100, self._year())
        rate = percent_spin(Decimal("0.05"))
        if row is not None:
            select_data(contract, row["contract_type_id"])
            year.setValue(row["year"])
            rate.setValue(float(row["rate"] * 100))
        dialog.add_row("Tipo de contrato", contract)
        dialog.add_row("Año", year)
        dialog.add_row("Aporte", rate)
        dialog.on_save(
            lambda: self.services.parameters.set_contribution(
                int(contract.currentData()), year.value(), spin_fraction(rate)
            )
        )
        return dialog

    def edit_contribution(self) -> None:
        self._run_form(self.contribution_dialog(self.contributions_page.selected()), "Aporte guardado")

    def delete_contribution(self) -> None:
        row = self.contributions_page.selected()
        if row is None:
            return
        self._confirm_and_run(
            f"Se eliminará el aporte de {row['contract']} para {row['year']}.",
            lambda: self.services.parameters.delete_contribution(row["contract_type_id"], row["year"]),
            "Aporte eliminado",
        )

    def retention_dialog(self, row: dict[str, Any] | None = None) -> FormDialog:
        dialog = FormDialog("Tasa de retención de honorarios", self)
        year = int_spin(2000, 2100, self._year())
        rate = percent_spin(Decimal("0.1525"))
        if row is not None:
            year.setValue(row["year"])
            rate.setValue(float(row["rate"] * 100))
        dialog.add_row("Desde el año", year)
        dialog.add_row("Tasa", rate)
        dialog.on_save(lambda: self.services.parameters.set_retention_rate(year.value(), spin_fraction(rate)))
        return dialog

    def edit_retention(self) -> None:
        self._run_form(self.retention_dialog(self.retention_page.selected()), "Tasa de retención guardada")

    def delete_retention(self) -> None:
        row = self.retention_page.selected()
        if row is None:
            return
        self._confirm_and_run(
            f"Se eliminará la tasa de retención vigente desde {row['year']}.",
            lambda: self.services.parameters.delete_retention_rate(row["year"]),
            "Tasa de retención eliminada",
        )

    def salary_scale_dialog(self, row: dict[str, Any] | None = None) -> FormDialog:
        dialog = FormDialog("Sueldo del grado 15", self, intro="Monto mensual para la jornada completa (44 h).")
        category = combo([(f"{code}: {CATEGORY_LABELS[code]}", code) for code in CATEGORIES])
        valid_from = date_edit(date(self._year(), 1, 1))
        amount = int_spin(1, 20_000_000, 900_000, group=True)
        amount.setPrefix("$ ")
        if row is not None:
            select_data(category, row["category"])
            set_edit_date(valid_from, row["valid_from"])
            amount.setValue(row["amount"])
        dialog.add_row("Categoría", category)
        dialog.add_row("Vigente desde", valid_from)
        dialog.add_row("Sueldo grado 15", amount)
        dialog.on_save(
            lambda: self.services.parameters.set_salary_scale(
                str(category.currentData()), edit_date(valid_from), amount.value()
            )
        )
        return dialog

    def edit_salary_scale(self) -> None:
        self._run_form(self.salary_scale_dialog(self.salary_scale_page.selected()), "Escala de sueldo guardada")

    def delete_salary_scale(self) -> None:
        row = self.salary_scale_page.selected()
        if row is None:
            return
        self._confirm_and_run(
            f"Se eliminará la escala de la categoría {row['category']} vigente desde el "
            f"{format_date(row['valid_from'])}.",
            lambda: self.services.parameters.delete_salary_scale(row["category"], row["valid_from"]),
            "Escala de sueldo eliminada",
        )

    def salary_adjustment_dialog(self, row: dict[str, Any] | None = None) -> FormDialog:
        dialog = FormDialog(
            "Reajuste del sector público", self, intro="Aplica solo a plazo fijo y planta, de forma multiplicativa."
        )
        valid_from = date_edit(date(self._year(), 12, 1))
        percent = percent_spin(Decimal("0.03"))
        description = QLineEdit()
        description.setMaxLength(200)
        description.setPlaceholderText("Opcional, por ejemplo «Reajuste sector público»")
        if row is not None:
            set_edit_date(valid_from, row["valid_from"])
            percent.setValue(float(row["percent"] * 100))
            description.setText(row["description"])
        dialog.add_row("Vigente desde", valid_from)
        dialog.add_row("Reajuste", percent)
        dialog.add_row("Descripción", description)
        dialog.on_save(
            lambda: self.services.parameters.set_salary_adjustment(
                edit_date(valid_from), spin_fraction(percent), description.text()
            )
        )
        return dialog

    def edit_salary_adjustment(self) -> None:
        self._run_form(self.salary_adjustment_dialog(self.salary_adjustment_page.selected()), "Reajuste guardado")

    def delete_salary_adjustment(self) -> None:
        row = self.salary_adjustment_page.selected()
        if row is None:
            return
        self._confirm_and_run(
            f"Se eliminará el reajuste vigente desde el {format_date(row['valid_from'])}.",
            lambda: self.services.parameters.delete_salary_adjustment(row["valid_from"]),
            "Reajuste eliminado",
        )

    def save_settings(self) -> bool:
        try:
            self.services.parameters.update_settings(
                spin_decimal(self.weeks_spin),
                RetentionBasis(self.basis_combo.currentData()),
                spin_decimal(self.full_time_spin),
            )
        except AppError as error:
            dialogs.show_error(self, error.user_message)
            return False
        self._after_change("Convenciones guardadas")
        return True
