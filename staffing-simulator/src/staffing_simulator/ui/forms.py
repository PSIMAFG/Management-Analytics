"""Diálogos de edición validados: escenarios, posiciones, personas, parámetros e importaciones.

La validación la hacen los servicios: si rechazan los datos, el diálogo
muestra el motivo y queda abierto para corregirlos.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from staffing_simulator.domain.imports import ImportReport
from staffing_simulator.domain.models import (
    BudgetItem,
    BudgetItemType,
    Catalog,
    ContractType,
    CostMethod,
    PartialMonthMethod,
    Person,
    Position,
    PositionDraft,
    Scenario,
    ScenarioDraft,
)
from staffing_simulator.domain.rut import format_rut
from staffing_simulator.domain.text import plural
from staffing_simulator.domain.validation import WorkloadWarning
from staffing_simulator.errors import AppError, ValidationError
from staffing_simulator.services import PositionResult, Services
from staffing_simulator.ui import dialogs
from staffing_simulator.ui.formatting import format_clp, format_date, format_hours, format_int, parse_amount
from staffing_simulator.ui.widgets import Column, RecordTableModel, make_table_view, muted_label
from staffing_simulator.ui.workers import UNEXPECTED_ERROR

log = logging.getLogger(__name__)

NO_BASE = "Sin escenario base"
VACANCY = "Vacante (sin persona asignada)"


def int_spin(minimum: int, maximum: int, value: int, *, suffix: str = "", group: bool = False) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    spin.setSuffix(suffix)
    spin.setGroupSeparatorShown(group)
    spin.setAccelerated(True)
    return spin


def decimal_spin(
    minimum: float, maximum: float, value: float, *, decimals: int = 2, step: float = 0.5, suffix: str = ""
) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setSuffix(suffix)
    return spin


def spin_decimal(spin: QDoubleSpinBox) -> Decimal:
    """Valor exacto de un campo decimal (sin arrastrar el error de coma flotante)."""
    return Decimal(f"{spin.value():.{spin.decimals()}f}")


def percent_spin(fraction: Decimal, maximum: float = 50.0, decimals: int = 2) -> QDoubleSpinBox:
    return decimal_spin(0.0, maximum, float(fraction * 100), decimals=decimals, step=0.5, suffix=" %")


def spin_fraction(spin: QDoubleSpinBox) -> Decimal:
    """Porcentaje del campo como fracción exacta (3,25 % -> 0,0325)."""
    return spin_decimal(spin) / 100


def date_edit(value: date) -> QDateEdit:
    edit = QDateEdit(QDate(value.year, value.month, value.day))
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("dd-MM-yyyy")
    return edit


def edit_date(edit: QDateEdit) -> date:
    value = edit.date()
    return date(value.year(), value.month(), value.day())


def set_edit_date(edit: QDateEdit, value: date) -> None:
    edit.setDate(QDate(value.year, value.month, value.day))


def combo(items: Sequence[tuple[str, Any]], current: Any = None) -> QComboBox:
    box = QComboBox()
    for label, value in items:
        box.addItem(label, value)
    select_data(box, current)
    return box


def select_data(box: QComboBox, value: Any) -> bool:
    """Selecciona el elemento cuyo dato asociado es `value`; devuelve False si no existe."""
    for index in range(box.count()):
        if box.itemData(index) == value:
            box.setCurrentIndex(index)
            return True
    return False


class MoneyEdit(QLineEdit):
    """Campo de monto en pesos: acepta 300.700.000 o 300700000 y muestra puntos de miles."""

    def __init__(self, value: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Por ejemplo, 300.700.000")
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        if value is not None:
            self.setText(format_int(value))
        self.editingFinished.connect(self._reformat)

    def amount(self) -> int:
        return parse_amount(self.text())

    def _reformat(self) -> None:
        try:
            self.setText(format_int(self.amount()))
        except ValidationError:
            return


class FormDialog(QDialog):
    """Formulario que llama a una función al guardar y muestra su error sin cerrarse."""

    def __init__(
        self, title: str, parent: QWidget | None = None, *, intro: str = "", save_text: str = "Guardar"
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)
        self._handler: Callable[[], Any] | None = None
        self.result_value: Any = None
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if intro:
            layout.addWidget(muted_label(intro))
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setHorizontalSpacing(12)
        self.form.setVerticalSpacing(8)
        layout.addLayout(self.form)
        self.extra = QVBoxLayout()
        layout.addLayout(self.extra)
        self.error_label = QLabel(objectName="errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox()
        self.save_button = self.buttons.addButton(save_text, QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        cancel = self.buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setAutoDefault(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def add_row(self, label: str, field: QWidget | QHBoxLayout) -> None:
        self.form.addRow(label, field)

    def on_save(self, handler: Callable[[], Any]) -> None:
        self._handler = handler

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def clear_error(self) -> None:
        self.error_label.setText("")
        self.error_label.setVisible(False)

    @property
    def error_text(self) -> str:
        return self.error_label.text()

    def submit(self) -> bool:
        """Guarda con la función registrada; devuelve False y muestra el motivo si falla."""
        self.clear_error()
        try:
            self.result_value = self._handler() if self._handler is not None else None
        except AppError as error:
            log.info("Formulario rechazado: %s", error.user_message)
            self.show_error(error.user_message)
            return False
        except Exception:
            log.exception("Error inesperado al guardar el formulario %s", self.windowTitle())
            self.show_error(UNEXPECTED_ERROR)
            return False
        return True

    def accept(self) -> None:
        if self.submit():
            super().accept()


def person_label(person: Person) -> str:
    """Nombre de la persona con su RUT, para distinguir personas con el mismo nombre."""
    return f"{person.full_name} ({format_rut(person.rut)})" if person.rut else person.full_name


def scenario_choices(scenarios: Sequence[Scenario], exclude_id: int | None = None) -> list[tuple[str, Any]]:
    """Opciones de escenario base: ninguno o cualquier otro escenario."""
    return [(NO_BASE, None)] + [(item.name, item.id) for item in scenarios if item.id != exclude_id]


class ScenarioDialog(FormDialog):
    """Nuevo escenario con sus supuestos."""

    def __init__(
        self, services: Services, scenarios: Sequence[Scenario], default_year: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Nuevo escenario",
            parent,
            intro="El escenario se crea sin posiciones. Para partir de uno existente use Duplicar.",
            save_text="Crear escenario",
        )
        self.services = services
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(80)
        self.name_edit.setPlaceholderText("Por ejemplo, Refuerzo de invierno")
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("Opcional: qué supone este escenario")
        self.description_edit.setFixedHeight(64)
        self.year_spin = int_spin(2000, 2100, default_year)
        self.method_combo = combo([(item.label, item.value) for item in PartialMonthMethod])
        self.absence_spin = percent_spin(Decimal(0))
        self.base_combo = combo(scenario_choices(scenarios))
        self.add_row("Nombre", self.name_edit)
        self.add_row("Descripción", self.description_edit)
        self.add_row("Año", self.year_spin)
        self.add_row("Meses parciales", self.method_combo)
        self.add_row("Ausentismo esperado", self.absence_spin)
        self.add_row("Escenario base", self.base_combo)
        self.on_save(self._save)

    def draft(self) -> ScenarioDraft:
        return ScenarioDraft(
            name=self.name_edit.text(),
            year=self.year_spin.value(),
            description=self.description_edit.toPlainText(),
            partial_month_method=PartialMonthMethod(self.method_combo.currentData()),
            expected_absence=spin_fraction(self.absence_spin),
            base_scenario_id=self.base_combo.currentData(),
        )

    def _save(self) -> Scenario:
        return self.services.scenarios.create_scenario(self.draft())


class NameDialog(FormDialog):
    """Pide un nombre (y opcionalmente una descripción) y lo entrega a una función que valida y guarda."""

    def __init__(
        self,
        title: str,
        name: str,
        on_save: Callable[[str, str | None], Any],
        parent: QWidget | None = None,
        *,
        description: str | None = None,
        intro: str = "",
        save_text: str = "Guardar",
    ) -> None:
        super().__init__(title, parent, intro=intro, save_text=save_text)
        self.name_edit = QLineEdit(name)
        self.name_edit.setMaxLength(80)
        self.name_edit.selectAll()
        self.add_row("Nombre", self.name_edit)
        self.description_edit: QPlainTextEdit | None = None
        if description is not None:
            self.description_edit = QPlainTextEdit(description)
            self.description_edit.setFixedHeight(64)
            self.add_row("Descripción", self.description_edit)
        self.on_save(lambda: on_save(self.name_edit.text(), self.description()))

    def description(self) -> str | None:
        return None if self.description_edit is None else self.description_edit.toPlainText()


class PersonDialog(FormDialog):
    """Registro de una persona (nombre y RUT opcional)."""

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(
            "Nueva persona",
            parent,
            intro="El RUT es opcional; si se indica debe ser válido y no estar registrado.",
            save_text="Registrar",
        )
        self.services = services
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(80)
        self.rut_edit = QLineEdit()
        self.rut_edit.setPlaceholderText("Por ejemplo, 41.234.567-3")
        self.add_row("Nombre completo", self.name_edit)
        self.add_row("RUT", self.rut_edit)
        self.on_save(self._save)

    def _save(self) -> Person:
        return self.services.scenarios.create_person(self.name_edit.text(), self.rut_edit.text() or None)


class PositionDialog(FormDialog):
    """Alta o edición de una posición, con vista previa del costo anual."""

    def __init__(
        self,
        services: Services,
        scenario: Scenario,
        catalog: Catalog,
        budget_items: Sequence[BudgetItem],
        position: Position | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Editar posición" if position is not None else "Agregar posición",
            parent,
            intro=f"Escenario «{scenario.name}», año {scenario.year}. Deje la persona como vacante si el puesto "
            "aún no está asignado.",
            save_text="Guardar posición",
        )
        self.setMinimumWidth(580)
        self.services = services
        self.scenario = scenario
        self.position = position
        self.contract_types: dict[int, ContractType] = {item.id: item for item in catalog.contract_types}
        self.persons: dict[int, Person] = {item.id: item for item in catalog.persons}
        self.budget_items = list(budget_items)
        self._build_fields(scenario, catalog)
        if position is not None:
            self._load(position)
        else:
            self.open_end.setChecked(True)
        self._connect_preview()
        self._sync_fields()
        self.update_preview()
        self.on_save(self._save)

    def _items_for_program(self, program_id: Any) -> list[tuple[str, Any]]:
        return [
            (item.name, item.id)
            for item in self.budget_items
            if item.program.id == program_id and item.item_type is BudgetItemType.HUMAN_RESOURCES
        ]

    def _build_fields(self, scenario: Scenario, catalog: Catalog) -> None:
        self.role_combo = combo([(f"{item.name} (categoría {item.category})", item.id) for item in catalog.job_roles])
        self.contract_combo = combo([(item.name, item.id) for item in catalog.contract_types])
        self.site_combo = combo([(item.name, item.id) for item in catalog.sites])
        self.program_combo = combo([(item.name, item.id) for item in catalog.programs])
        self.item_combo = combo(self._items_for_program(self.program_combo.currentData()))
        self.program_combo.currentIndexChanged.connect(self._fill_items)
        person_row = self._build_person_row(catalog.persons)
        self.weekly_spin = decimal_spin(0, 48, 44, suffix=" h semanales")
        self.monthly_spin = decimal_spin(0, 220, 40, step=1, suffix=" h al mes")
        self.quantity_spin = int_spin(1, 100, 1)
        self.grade_spin = int_spin(1, 30, 15)
        self.grade_spin.setToolTip(
            "Grado real de la persona: solo informativo. El costo siempre usa el sueldo del grado 15; la "
            "diferencia la asume el municipio."
        )
        self.start_edit = date_edit(date(scenario.year, 1, 1))
        self.open_end = QCheckBox("Sin término (hasta fin de año)")
        self.end_edit = date_edit(date(scenario.year, 12, 31))
        end_row = QHBoxLayout()
        end_row.addWidget(self.end_edit, 1)
        end_row.addWidget(self.open_end)
        self.note_edit = QLineEdit()
        self.note_edit.setMaxLength(500)
        self.note_edit.setPlaceholderText("Opcional")
        self.preview_label = QLabel(objectName="previewLabel")
        self.preview_label.setWordWrap(True)

        self.add_row("Cargo", self.role_combo)
        self.add_row("Tipo de contrato", self.contract_combo)
        self.add_row("Sede", self.site_combo)
        self.add_row("Programa", self.program_combo)
        self.add_row("Ítem de recurso humano", self.item_combo)
        self.add_row("Persona", person_row)
        self.add_row("Jornada semanal", self.weekly_spin)
        self.add_row("Horas mensuales estimadas", self.monthly_spin)
        self.add_row("Cantidad", self.quantity_spin)
        self.add_row("Grado real (informativo)", self.grade_spin)
        self.add_row("Inicio", self.start_edit)
        self.add_row("Término", end_row)
        self.add_row("Nota", self.note_edit)
        self.extra.addWidget(self.preview_label)

    def _fill_items(self) -> None:
        current = self.item_combo.currentData()
        self.item_combo.clear()
        for label, value in self._items_for_program(self.program_combo.currentData()):
            self.item_combo.addItem(label, value)
        if not select_data(self.item_combo, current) and self.item_combo.count():
            self.item_combo.setCurrentIndex(0)

    def _build_person_row(self, persons: Sequence[Person]) -> QHBoxLayout:
        """Persona con búsqueda por texto y botón para registrar una nueva."""
        self.person_combo = QComboBox()
        self.person_combo.setEditable(True)
        self.person_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._fill_persons(sorted(persons, key=lambda item: item.full_name))
        completer = self.person_combo.completer()
        if completer is not None:
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.new_person_button = QPushButton("Nueva persona...")
        self.new_person_button.setAutoDefault(False)
        self.new_person_button.clicked.connect(self._new_person)
        row = QHBoxLayout()
        row.addWidget(self.person_combo, 1)
        row.addWidget(self.new_person_button)
        return row

    def _connect_preview(self) -> None:
        """Recalcula la vista previa del costo (con una breve espera) y ajusta los campos al cambiar un dato."""
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(200)
        self._preview_timer.timeout.connect(self.update_preview)
        for signal in (
            self.role_combo.currentIndexChanged,
            self.contract_combo.currentIndexChanged,
            self.site_combo.currentIndexChanged,
            self.program_combo.currentIndexChanged,
            self.person_combo.currentIndexChanged,
            self.weekly_spin.valueChanged,
            self.monthly_spin.valueChanged,
            self.quantity_spin.valueChanged,
            self.start_edit.dateChanged,
            self.end_edit.dateChanged,
            self.open_end.toggled,
        ):
            signal.connect(self._schedule_preview)
        self.contract_combo.currentIndexChanged.connect(self._sync_fields)
        self.person_combo.currentIndexChanged.connect(self._sync_fields)
        self.open_end.toggled.connect(self._sync_fields)

    def _fill_persons(self, persons: Sequence[Person]) -> None:
        self.person_combo.clear()
        self.person_combo.addItem(VACANCY, None)
        for person in persons:
            self.person_combo.addItem(person_label(person), person.id)

    def _load(self, position: Position) -> None:
        select_data(self.role_combo, position.job_role.id)
        select_data(self.contract_combo, position.contract_type.id)
        select_data(self.site_combo, position.site.id)
        select_data(self.program_combo, position.program.id)
        self._fill_items()
        select_data(self.item_combo, position.budget_item.id)
        select_data(self.person_combo, None if position.person is None else position.person.id)
        if position.weekly_hours is not None:
            self.weekly_spin.setValue(float(position.weekly_hours))
        if position.monthly_hours is not None:
            self.monthly_spin.setValue(float(position.monthly_hours))
        self.quantity_spin.setValue(position.quantity)
        self.grade_spin.setValue(position.grade or 15)
        set_edit_date(self.start_edit, position.start_date)
        self.open_end.setChecked(position.end_date is None)
        if position.end_date is not None:
            set_edit_date(self.end_edit, position.end_date)
        self.note_edit.setText(position.note)

    def _contract_type(self) -> ContractType | None:
        return self.contract_types.get(self.contract_combo.currentData())

    def uses_weekly_hours(self) -> bool:
        contract_type = self._contract_type()
        return contract_type is None or contract_type.cost_method.uses_weekly_hours

    def _sync_fields(self) -> None:
        weekly = self.uses_weekly_hours()
        self.form.setRowVisible(self.weekly_spin, weekly)
        self.form.setRowVisible(self.monthly_spin, not weekly)
        contract_type = self._contract_type()
        self.form.setRowVisible(
            self.grade_spin, contract_type is not None and contract_type.cost_method is CostMethod.SALARIED
        )
        has_person = self.person_combo.currentData() is not None
        if has_person:
            self.quantity_spin.setValue(1)
        self.quantity_spin.setEnabled(not has_person)
        self.quantity_spin.setToolTip(
            "Una posición asignada a una persona tiene cantidad 1."
            if has_person
            else "Cantidad de posiciones idénticas (por ejemplo, vacantes del mismo cargo)."
        )
        self.end_edit.setEnabled(not self.open_end.isChecked())

    def _person_id(self) -> int | None:
        """Persona elegida: la del elemento seleccionado o, si se escribió el texto, la única que coincide.

        Dos personas pueden tener el mismo nombre (solo el RUT es único), por
        eso nunca se busca por el texto cuando coincide con el elemento
        seleccionado, y un texto escrito que coincide con varias personas se
        rechaza.
        """
        box = self.person_combo
        text = box.currentText().strip()
        index = box.currentIndex()
        if index >= 0 and box.itemText(index) == text:
            value = box.itemData(index)
            return None if value is None else int(value)
        key = text.casefold()
        if not text or key == VACANCY.casefold():
            return None
        matches = [
            int(box.itemData(row))
            for row in range(box.count())
            if box.itemData(row) is not None
            and key in (box.itemText(row).casefold(), self.persons[int(box.itemData(row))].full_name.casefold())
        ]
        if len(set(matches)) > 1:
            raise ValidationError(
                f"Hay varias personas llamadas «{text}». Elija la persona en la lista: el RUT las distingue."
            )
        if not matches:
            raise ValidationError(
                f"«{text}» no está en la lista de personas. Elija una persona de la lista, deje la posición "
                "como vacante o use Nueva persona."
            )
        return matches[0]

    def draft(self) -> PositionDraft:
        weekly = self.uses_weekly_hours()
        contract_type = self._contract_type()
        salaried = contract_type is not None and contract_type.cost_method is CostMethod.SALARIED
        item_id = self.item_combo.currentData()
        return PositionDraft(
            job_role_id=int(self.role_combo.currentData()),
            contract_type_id=int(self.contract_combo.currentData()),
            site_id=int(self.site_combo.currentData()),
            program_id=int(self.program_combo.currentData()),
            budget_item_id=None if item_id is None else int(item_id),
            start_date=edit_date(self.start_edit),
            end_date=None if self.open_end.isChecked() else edit_date(self.end_edit),
            person_id=self._person_id(),
            weekly_hours=spin_decimal(self.weekly_spin) if weekly else None,
            monthly_hours=None if weekly else spin_decimal(self.monthly_spin),
            quantity=self.quantity_spin.value(),
            grade=self.grade_spin.value() if salaried else None,
            note=self.note_edit.text(),
        )

    def _schedule_preview(self) -> None:
        self._preview_timer.start()

    def update_preview(self) -> None:
        """Costo anual estimado con los datos actuales del formulario (sin guardar)."""
        try:
            cost = self.services.costs.preview_position(self.scenario.id, self.draft())
        except AppError as error:
            self.preview_label.setText(f"Sin vista previa del costo: {error.user_message}")
            return
        except Exception:
            log.exception("No se pudo calcular la vista previa de la posición")
            self.preview_label.setText("Sin vista previa del costo.")
            return
        self.preview_label.setText(
            f"Costo estimado en {self.scenario.year}: {format_clp(cost.total)} "
            f"({cost.active_months} meses con costo; valor hora {format_clp(cost.hourly_rate)})."
        )

    def _new_person(self) -> None:
        dialog = PersonDialog(self.services, self)
        if dialogs.run_dialog(dialog) and isinstance(dialog.result_value, Person):
            person = dialog.result_value
            self.persons[person.id] = person
            self.person_combo.addItem(person_label(person), person.id)
            select_data(self.person_combo, person.id)

    def _save(self) -> PositionResult:
        draft = self.draft()
        if self.position is None:
            return self.services.scenarios.add_position(self.scenario.id, draft)
        return self.services.scenarios.update_position(self.position.id, draft)


def review_summary(report: ImportReport) -> str:
    """Resumen de la validación de una planilla con concordancia de número."""
    valid = plural(report.accepted, "válida", "válidas")
    rejected = plural(len(report.rejected), "con errores", "con errores")
    summary = f"La planilla tiene {plural(report.total_rows, 'fila', 'filas')} con datos: {valid} y {rejected}. "
    summary += "No se ha guardado nada."
    if report.accepted:
        rows = "la fila válida" if report.accepted == 1 else "solo las filas válidas"
        summary += f" Puede importar {rows} o cancelar para corregir la planilla."
    if report.replaced:
        amounts = plural(report.replaced, "monto ya registrado", "montos ya registrados")
        summary += f" Al importar se reemplazarán {amounts}."
    return summary


def import_button_text(accepted: int) -> str:
    """Texto del botón que importa solo las filas válidas."""
    return "Importar la fila válida" if accepted == 1 else f"Importar solo las {accepted} filas válidas"


class ImportReviewDialog(QDialog):
    """Resultado de validar una planilla: filas válidas y filas rechazadas con su motivo."""

    def __init__(self, title: str, report: ImportReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        valid = report.accepted
        summary = review_summary(report)
        layout.addWidget(muted_label(summary))
        self.model = RecordTableModel(
            [Column("row_number", "Fila", format_int, numeric=True), Column("reason", "Motivo del rechazo")]
        )
        self.model.set_rows(list(report.rejected))
        layout.addWidget(make_table_view(self.model, self, stretch_column=1), 1)
        buttons = QDialogButtonBox()
        self.import_button: QPushButton | None = None
        if valid:
            self.import_button = buttons.addButton(import_button_text(valid), QDialogButtonBox.ButtonRole.AcceptRole)
            self.import_button.setObjectName("primary")
        close = buttons.addButton("Cancelar" if valid else "Cerrar", QDialogButtonBox.ButtonRole.RejectRole)
        close.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class WarningsDialog(QDialog):
    """Advertencias del escenario: jornada por persona y otros avisos (no impiden guardar ni costear)."""

    def __init__(
        self, warnings: Sequence[WorkloadWarning], parent: QWidget | None = None, notices: Sequence[str] = ()
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Advertencias del escenario")
        self.setModal(True)
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        if notices:
            layout.addWidget(QLabel("Posiciones y parámetros", objectName="sectionTitle"))
            self.notices = QPlainTextEdit("\n".join(notices))
            self.notices.setReadOnly(True)
            self.notices.setMaximumHeight(140)
            layout.addWidget(self.notices)
            layout.addWidget(QLabel("Jornada por persona", objectName="sectionTitle"))
        layout.addWidget(
            muted_label(
                "Personas que en algún tramo del año suman más horas semanales (en todas sus posiciones, sin "
                "importar el tipo de contrato) que la jornada completa (Parámetros, Convenciones). Da lo mismo en "
                "cuántos registros esté repartida la jornada. Los contratos por horas suman su equivalente "
                "semanal. Son avisos: no impiden guardar ni costear."
            )
        )
        self.model = RecordTableModel(
            [
                Column("person", "Persona"),
                Column("start", "Desde", format_date),
                Column("end", "Hasta", format_date),
                Column("hours", "Horas semanales", format_hours, numeric=True, tooltip_key="detail"),
                Column("limit", "Jornada completa", format_hours, numeric=True),
            ]
        )
        self.model.set_rows(
            [
                {
                    "person": item.person.full_name,
                    "start": item.start,
                    "end": item.end,
                    "hours": item.weekly_hours,
                    "limit": item.limit_hours,
                    "detail": item.message,
                }
                for item in warnings
            ]
        )
        layout.addWidget(make_table_view(self.model, self, stretch_column=0), 1)
        buttons = QDialogButtonBox()
        buttons.addButton("Cerrar", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
