"""Diálogo de parámetros y catálogos.

Pestañas: parámetros de validación (editables, con validación en vivo),
programas y alias (crear, editar y desactivar; código de carpeta de 3
dígitos y alias con prioridad), tasas de retención (editables por año),
valores hora de referencia y prestadores con nombre confirmado. Programas,
valores hora y prestadores se pueden importar desde Excel (la importación
valida fila por fila y no guarda nada si la planilla no se puede leer) y
ofrecen una planilla modelo para completar.

Cada cambio guardado emite `catalog_changed` para que la ventana se
actualice aunque el diálogo siga abierto. Mientras una tarea corre, el
diálogo no se puede cerrar: así su resultado siempre llega a la ventana.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from receipt_reader.domain.forms import parse_field, validate_alias_fields, validate_program_fields, validate_settings
from receipt_reader.domain.models import Program, Settings
from receipt_reader.domain.money import parse_clp
from receipt_reader.domain.rut import format_rut, parse_rut
from receipt_reader.domain.text import format_percent_bp, format_thousands
from receipt_reader.errors import AppError, FieldFormatError, FormError
from receipt_reader.services.app_services import AppServices
from receipt_reader.services.catalog import MAX_RATE_YEAR, MIN_RATE_YEAR, ImportReport
from receipt_reader.ui.dialogs import ImportReportDialog, notify, open_path, show_error
from receipt_reader.ui.formatting import format_datetime, format_int, plural
from receipt_reader.ui.widgets import (
    Column,
    RecordTableModel,
    fit_columns_once,
    make_table_view,
    muted_label,
    source_row_of,
)
from receipt_reader.ui.workers import TaskRunner, Worker

log = logging.getLogger(__name__)

EXCEL_FILTER = "Planillas Excel (*.xlsx)"
BUSY_MESSAGE = "Espere a que termine la tarea en curso antes de cerrar."


def settings_from_form(
    base: Settings, texts: dict[str, str], numbers: dict[str, int], dates: dict[str, date]
) -> Settings:
    """Arma la configuración desde el formulario.

    El formulario solo interpreta el texto (montos y RUT); las reglas de los parámetros
    son las de `validate_settings`, las mismas que aplica el servicio al guardar. Lanza
    FormError con el mensaje de cada campo que no es válido.
    """
    errors: dict[str, str] = {}
    amounts: dict[str, int] = {}
    for name in ("amount_min", "amount_max"):
        try:
            amounts[name] = parse_clp(texts[name])
        except FieldFormatError as error:
            errors[name] = error.user_message
    rut_text = texts["organization_rut"].strip()
    rut = ""
    if rut_text:
        try:
            rut = parse_rut(rut_text)
        except FieldFormatError as error:
            errors["organization_rut"] = error.user_message
    if errors:
        raise FormError(errors)
    settings = replace(
        base,
        organization_name=" ".join(texts["organization_name"].split()),
        organization_rut=rut,
        date_window_start=dates["date_window_start"],
        date_window_end=dates["date_window_end"],
        window_months_before=numbers["window_months_before"],
        window_months_after=numbers["window_months_after"],
        amount_min=amounts["amount_min"],
        amount_max=amounts["amount_max"],
        retention_tolerance=numbers["retention_tolerance"],
        folder_month_tolerance=numbers["folder_month_tolerance"],
        ocr_min_confidence=numbers["ocr_min_confidence"] / 100,
        ocr_dpi=numbers["ocr_dpi"],
    )
    validate_settings(settings)
    return settings


def form_error_text(error: AppError) -> str:
    """Mensajes de un error de formulario, uno por línea."""
    if isinstance(error, FormError):
        return "\n".join(dict.fromkeys(error.field_errors.values()))
    return error.user_message


def stretch_column(view: QTableView, section: int | None) -> None:
    """Deja que una columna de texto absorba el ancho sobrante (None: ninguna, el sobrante queda a la derecha).

    Así una columna numérica no se estira hasta el borde y su valor queda cerca de su rótulo.
    """
    header = view.horizontalHeader()
    header.setStretchLastSection(False)
    if section is not None:
        header.setSectionResizeMode(section, QHeaderView.ResizeMode.Stretch)


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _pydate(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


class ParametersDialog(QDialog):
    """Parámetros de validación y catálogos.

    `catalog_changed` se emite cada vez que se guarda algo que cambia la validación o los
    catálogos; `data_changed` queda en True si ocurrió al menos una vez.
    """

    catalog_changed = Signal()

    def __init__(self, services: AppServices, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.data_changed = False
        self.unattended = False
        self.tasks = TaskRunner(self)
        self._settings = services.catalog.settings()
        self.setWindowTitle("Parámetros y catálogos")
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_settings_tab(), "Validación")
        self.tabs.addTab(self._build_programs_tab(), "Programas")
        self.tabs.addTab(self._build_rates_tab(), "Tasas de retención")
        self.tabs.addTab(self._build_reference_tab(), "Valores hora de referencia")
        self.tabs.addTab(self._build_providers_tab(), "Prestadores")
        layout.addWidget(self.tabs, 1)
        self.status_label = QLabel("", objectName="muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox()
        self.close_button = buttons.addButton("Cerrar", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_catalogs()

    # Validación

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addWidget(
            muted_label(
                "Al guardar se vuelven a validar todas las boletas no descartadas con estos parámetros. "
                "Las correcciones manuales se conservan."
            )
        )
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        settings = self._settings
        self.name_edit = QLineEdit(settings.organization_name)
        self.rut_edit = QLineEdit(format_rut(settings.organization_rut) if settings.organization_rut else "")
        self.rut_edit.setPlaceholderText("RUT de la organización receptora")
        self.start_edit = self._date_edit(settings.date_window_start)
        self.end_edit = self._date_edit(settings.date_window_end)
        self.before_spin = self._spin(0, 24, settings.window_months_before, ("mes antes", "meses antes"))
        self.after_spin = self._spin(0, 24, settings.window_months_after, ("mes después", "meses después"))
        self.min_edit = QLineEdit(format_thousands(settings.amount_min))
        self.max_edit = QLineEdit(format_thousands(settings.amount_max))
        self.tolerance_spin = self._spin(0, 1000, settings.retention_tolerance, ("peso", "pesos"))
        self.month_spin = self._spin(0, 12, settings.folder_month_tolerance, ("mes", "meses"))
        self.confidence_spin = self._spin(50, 100, round(settings.ocr_min_confidence * 100), ("%", "%"))
        self.dpi_spin = self._spin(100, 400, settings.ocr_dpi, ("ppp", "ppp"))
        self.dpi_spin.setSingleStep(25)
        relative = QHBoxLayout()
        relative.setSpacing(10)
        relative.addWidget(self.before_spin)
        relative.addWidget(QLabel("y"))
        relative.addWidget(self.after_spin)
        relative.addStretch(1)
        window = QHBoxLayout()
        window.setSpacing(10)
        window.addWidget(self.start_edit)
        window.addWidget(QLabel("hasta"))
        window.addWidget(self.end_edit)
        window.addStretch(1)
        amounts = QHBoxLayout()
        amounts.setSpacing(10)
        for edit in (self.min_edit, self.max_edit):
            edit.setMaximumWidth(150)
            edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        amounts.addWidget(self.min_edit)
        amounts.addWidget(QLabel("hasta"))
        amounts.addWidget(self.max_edit)
        amounts.addStretch(1)
        form.addRow("Organización receptora", self.name_edit)
        form.addRow("RUT de la organización", self.rut_edit)
        form.addRow("Emisión según la carpeta del mes", relative)
        form.addRow("Emisión sin carpeta de mes", window)
        form.addRow("Monto bruto aceptado (pesos)", amounts)
        form.addRow("Tolerancia de la retención", self.tolerance_spin)
        form.addRow("Tolerancia del mes de la carpeta", self.month_spin)
        form.addRow("Confianza mínima del OCR", self.confidence_spin)
        form.addRow("Resolución para el OCR", self.dpi_spin)
        outer.addLayout(form)
        outer.addWidget(
            muted_label(
                "La fecha de emisión se compara con el mes de la carpeta de pago (por ejemplo, 2026-09): se "
                "aceptan los meses indicados antes y después. Las fechas fijas solo se usan para archivos que "
                "no están en una carpeta de mes."
            )
        )
        self.settings_error = QLabel("", objectName="fieldError")
        self.settings_error.setWordWrap(True)
        outer.addWidget(self.settings_error)
        row = QHBoxLayout()
        row.addStretch(1)
        self.save_settings_button = QPushButton("Guardar parámetros", objectName="primary")
        self.save_settings_button.clicked.connect(self.save_settings)
        row.addWidget(self.save_settings_button)
        outer.addLayout(row)
        outer.addStretch(1)
        for edit in (self.name_edit, self.rut_edit, self.min_edit, self.max_edit):
            edit.textChanged.connect(self._validate_settings)
        for date_edit in (self.start_edit, self.end_edit):
            date_edit.dateChanged.connect(self._validate_settings)
        for spin in (self.before_spin, self.after_spin):
            spin.valueChanged.connect(self._validate_settings)
        self._validate_settings()
        return page

    @staticmethod
    def _date_edit(value: date) -> QDateEdit:
        edit = QDateEdit(_qdate(value))
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("dd-MM-yyyy")
        return edit

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, units: tuple[str, str]) -> QSpinBox:
        """Número entero con su unidad en singular o plural según el valor."""
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setMinimumWidth(130)
        spin.setMaximumWidth(180)

        def update_suffix(current: int) -> None:
            spin.setSuffix(" " + (units[0] if current == 1 else units[1]))

        spin.valueChanged.connect(update_suffix)
        spin.setValue(value)
        update_suffix(value)
        return spin

    def form_settings(self) -> Settings:
        """Configuración que resulta del formulario (lanza FormError si algo no es válido)."""
        texts = {
            "organization_name": self.name_edit.text(),
            "organization_rut": self.rut_edit.text(),
            "amount_min": self.min_edit.text(),
            "amount_max": self.max_edit.text(),
        }
        numbers = {
            "window_months_before": self.before_spin.value(),
            "window_months_after": self.after_spin.value(),
            "retention_tolerance": self.tolerance_spin.value(),
            "folder_month_tolerance": self.month_spin.value(),
            "ocr_min_confidence": self.confidence_spin.value(),
            "ocr_dpi": self.dpi_spin.value(),
        }
        dates = {"date_window_start": _pydate(self.start_edit.date()), "date_window_end": _pydate(self.end_edit.date())}
        return settings_from_form(self._settings, texts, numbers, dates)

    def _validate_settings(self) -> bool:
        try:
            self.form_settings()
        except AppError as error:
            message = form_error_text(error)
        else:
            message = ""
        self.settings_error.setText(message)
        self.save_settings_button.setEnabled(not message and not self.tasks.is_busy())
        return not message

    # Tareas en segundo plano

    def _run(self, worker: Worker, on_done: Callable[[object], None], message: str) -> None:
        """Ejecuta una tarea que guarda datos; mientras corre, el diálogo no se puede cerrar."""
        self.status_label.setText(message)
        self.close_button.setEnabled(False)
        self.save_settings_button.setEnabled(False)
        self.save_rate_button.setEnabled(False)
        self.save_program_button.setEnabled(False)
        self.toggle_program_button.setEnabled(False)
        self.add_alias_button.setEnabled(False)
        self.remove_alias_button.setEnabled(False)

        def finished(result: object) -> None:
            self._task_ended()
            on_done(result)

        self.tasks.start(worker, finished, self._on_failed)

    def _task_ended(self) -> None:
        # Se actualiza en la próxima vuelta del ciclo de eventos, cuando la tarea ya no cuenta como activa.
        QTimer.singleShot(0, self._refresh_actions)

    def _refresh_actions(self) -> None:
        self.close_button.setEnabled(not self.tasks.is_busy())
        self._validate_settings()
        self._validate_rate()
        self._validate_program()
        self._validate_alias()

    def _catalog_saved(self) -> None:
        self.data_changed = True
        self.catalog_changed.emit()

    def reject(self) -> None:
        if self.tasks.is_busy():
            self.status_label.setText(BUSY_MESSAGE)
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tasks.is_busy():
            self.status_label.setText(BUSY_MESSAGE)
            event.ignore()
            return
        super().closeEvent(event)

    def save_settings(self) -> None:
        if not self._validate_settings():
            return
        settings = self.form_settings()
        self._run(
            Worker(self.services.catalog.update_settings, settings),
            self._on_settings_saved,
            "Guardando parámetros y revalidando las boletas...",
        )

    def _on_settings_saved(self, changed: object) -> None:
        self._settings = self.services.catalog.settings()
        self._validate_settings()
        self.status_label.setText("Parámetros guardados. " + changed_text(changed))
        self._catalog_saved()

    # Catálogos

    def _build_programs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            muted_label(
                "El código de carpeta de 3 dígitos identifica el programa en la ruta de los archivos. "
                "Un programa desactivado ya no se ofrece para carpetas o alias nuevos, pero las boletas que "
                "ya lo tienen asignado lo conservan. Los alias se buscan como palabras completas en la glosa; "
                "gana el de menor prioridad."
            )
        )
        self.programs_model = RecordTableModel(
            [
                Column("folder_code", "Código"),
                Column("name", "Nombre"),
                Column("short_name", "Nombre corto"),
                Column("active", "Activo", lambda value: "Sí" if value else "No"),
            ],
            self,
        )
        self.programs_view = make_table_view(self.programs_model, self)
        self.programs_view.clicked.connect(lambda _index: self._pick_program())
        layout.addWidget(self.programs_view, 1)
        layout.addLayout(self._build_program_form())
        self.aliases_model = RecordTableModel(
            [
                Column("program", "Programa"),
                Column("alias", "Alias en la glosa"),
                Column("priority", "Prioridad", format_int, numeric=True),
            ],
            self,
        )
        self.aliases_view = make_table_view(self.aliases_model, self)
        self.aliases_view.clicked.connect(lambda _index: self._pick_alias())
        layout.addWidget(self.aliases_view, 1)
        layout.addLayout(self._build_alias_form())
        layout.addLayout(
            self._import_row(
                "programas",
                self.services.catalog.import_programs,
                self.services.catalog.write_program_template,
                "programas.xlsx",
            )
        )
        return page

    def _build_program_form(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        self._selected_program: int | None = None
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Código"))
        self.program_code_edit = QLineEdit()
        self.program_code_edit.setPlaceholderText("110")
        self.program_code_edit.setMaximumWidth(70)
        row.addWidget(self.program_code_edit)
        row.addWidget(QLabel("Nombre"))
        self.program_name_edit = QLineEdit()
        row.addWidget(self.program_name_edit, 1)
        row.addWidget(QLabel("Nombre corto"))
        self.program_short_edit = QLineEdit()
        self.program_short_edit.setMaximumWidth(160)
        row.addWidget(self.program_short_edit)
        outer.addLayout(row)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.new_program_button = QPushButton("Nuevo programa")
        self.new_program_button.clicked.connect(self._clear_program_form)
        actions.addWidget(self.new_program_button)
        self.save_program_button = QPushButton("Guardar programa", objectName="primary")
        self.save_program_button.clicked.connect(self.save_program)
        actions.addWidget(self.save_program_button)
        self.toggle_program_button = QPushButton("Desactivar")
        self.toggle_program_button.setEnabled(False)
        self.toggle_program_button.clicked.connect(self.toggle_program_active)
        actions.addWidget(self.toggle_program_button)
        actions.addStretch(1)
        outer.addLayout(actions)
        self.program_error = QLabel("", objectName="fieldError")
        self.program_error.setWordWrap(True)
        outer.addWidget(self.program_error)
        for edit in (self.program_code_edit, self.program_name_edit, self.program_short_edit):
            edit.textChanged.connect(self._validate_program)
        self._validate_program()
        return outer

    def _build_alias_form(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        self._selected_alias: tuple[int, str] | None = None
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Programa"))
        self.alias_program_combo = QComboBox()
        self.alias_program_combo.setMinimumWidth(220)
        row.addWidget(self.alias_program_combo)
        row.addWidget(QLabel("Alias en la glosa"))
        self.alias_text_edit = QLineEdit()
        row.addWidget(self.alias_text_edit, 1)
        row.addWidget(QLabel("Prioridad"))
        self.alias_priority_spin = QSpinBox()
        self.alias_priority_spin.setRange(0, 1000)
        self.alias_priority_spin.setValue(100)
        self.alias_priority_spin.setMaximumWidth(90)
        row.addWidget(self.alias_priority_spin)
        outer.addLayout(row)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.add_alias_button = QPushButton("Agregar alias")
        self.add_alias_button.clicked.connect(self.add_alias)
        actions.addWidget(self.add_alias_button)
        self.remove_alias_button = QPushButton("Quitar alias")
        self.remove_alias_button.setEnabled(False)
        self.remove_alias_button.clicked.connect(self.remove_alias)
        actions.addWidget(self.remove_alias_button)
        actions.addStretch(1)
        outer.addLayout(actions)
        self.alias_error = QLabel("", objectName="fieldError")
        self.alias_error.setWordWrap(True)
        outer.addWidget(self.alias_error)
        self.alias_text_edit.textChanged.connect(self._validate_alias)
        self._validate_alias()
        return outer

    def _build_rates_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            muted_label(
                "Tasa de retención de las boletas de honorarios según el año de emisión. La validación "
                "compara la tasa impresa y la retención con la tasa del año; después del último año "
                "registrado rige la última tasa. Si la ley cambia una tasa, verifíquela en la tabla "
                "publicada por el SII y corríjala aquí: al guardar se revalidan todas las boletas."
            )
        )
        self.rates_model = RecordTableModel(
            [Column("year", "Año", str, numeric=True), Column("rate_bp", "Tasa", format_percent_bp, numeric=True)],
            self,
        )
        self.rates_view = make_table_view(self.rates_model, self)
        self.rates_view.clicked.connect(lambda _index: self._pick_rate())
        layout.addWidget(self.rates_view, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Año"))
        self.rate_year_spin = QSpinBox()
        self.rate_year_spin.setRange(MIN_RATE_YEAR, MAX_RATE_YEAR)
        self.rate_year_spin.setValue(date.today().year)
        self.rate_year_spin.setMinimumWidth(90)
        row.addWidget(self.rate_year_spin)
        row.addSpacing(8)
        row.addWidget(QLabel("Tasa (%)"))
        self.rate_edit = QLineEdit()
        self.rate_edit.setPlaceholderText("Por ejemplo 15,25")
        self.rate_edit.setMaximumWidth(120)
        self.rate_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.rate_edit)
        self.save_rate_button = QPushButton("Guardar tasa")
        self.save_rate_button.setToolTip("Guarda la tasa del año y vuelve a validar todas las boletas")
        self.save_rate_button.clicked.connect(self.save_rate)
        row.addWidget(self.save_rate_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.rate_error = QLabel("", objectName="fieldError")
        layout.addWidget(self.rate_error)
        self.rate_edit.textChanged.connect(self._validate_rate)
        self._validate_rate()
        return page

    def _pick_rate(self) -> None:
        """Lleva al formulario el año y la tasa de la fila elegida, para corregirla."""
        row_index = source_row_of(self.rates_view)
        if row_index is None:
            return
        row = self.rates_model.row(row_index)
        self.rate_year_spin.setValue(int(row["year"]))
        self.rate_edit.setText(format_percent_bp(int(row["rate_bp"])).removesuffix(" %"))

    def _form_rate(self) -> int:
        """Tasa escrita en puntos básicos (lanza FieldFormatError si no es válida)."""
        rate = parse_field("printed_rate_bp", self.rate_edit.text())
        if rate is None:
            raise FieldFormatError("Escriba la tasa del año, por ejemplo 15,25.")
        return int(rate)

    def _validate_rate(self) -> bool:
        message = ""
        if self.rate_edit.text().strip():
            try:
                self._form_rate()
            except AppError as error:
                message = error.user_message
        self.rate_error.setText(message)
        valid = bool(self.rate_edit.text().strip()) and not message
        self.save_rate_button.setEnabled(valid and not self.tasks.is_busy())
        return valid

    def save_rate(self) -> None:
        if not self._validate_rate():
            return
        year = self.rate_year_spin.value()
        self._run(
            Worker(self.services.catalog.update_retention_rate, year, self._form_rate()),
            lambda changed: self._on_rate_saved(year, changed),
            f"Guardando la tasa de {year} y revalidando las boletas...",
        )

    def _on_rate_saved(self, year: int, changed: object) -> None:
        self.load_catalogs()
        self.rate_edit.clear()
        self.status_label.setText(f"Tasa de {year} guardada. " + changed_text(changed))
        self._catalog_saved()

    def _pick_program(self) -> None:
        """Lleva al formulario el programa de la fila elegida, para editarlo."""
        row_index = source_row_of(self.programs_view)
        if row_index is None:
            return
        program: Program = self.programs_model.row(row_index)
        self._selected_program = program.id
        self.program_code_edit.setText(program.folder_code)
        self.program_name_edit.setText(program.name)
        self.program_short_edit.setText(program.short_name)
        self.toggle_program_button.setText("Activar" if not program.active else "Desactivar")
        self.toggle_program_button.setEnabled(not self.tasks.is_busy())

    def _clear_program_form(self) -> None:
        self._selected_program = None
        self.program_code_edit.clear()
        self.program_name_edit.clear()
        self.program_short_edit.clear()
        self.toggle_program_button.setEnabled(False)
        self.programs_view.clearSelection()
        self._validate_program()

    def _form_program(self) -> tuple[str, str, str]:
        return (self.program_code_edit.text(), self.program_name_edit.text(), self.program_short_edit.text())

    def _validate_program(self) -> bool:
        message = ""
        code, name, short_name = self._form_program()
        if code.strip() or name.strip() or short_name.strip():
            try:
                validate_program_fields(code.strip().zfill(3) if code.strip() else code, name, short_name)
            except AppError as error:
                message = form_error_text(error)
        self.program_error.setText(message)
        valid = bool(code.strip() and name.strip() and short_name.strip()) and not message
        self.save_program_button.setEnabled(valid and not self.tasks.is_busy())
        return valid

    def save_program(self) -> None:
        if not self._validate_program():
            return
        code, name, short_name = self._form_program()
        if self._selected_program is None:
            worker = Worker(self.services.catalog.create_program, code, name, short_name)
        else:
            current = next((p for p in self.programs_model.rows() if p.id == self._selected_program), None)
            active = current.active if current is not None else True
            worker = Worker(
                self.services.catalog.update_program, self._selected_program, code, name, short_name, active=active
            )
        self._run(worker, self._on_program_saved, "Guardando el programa y revalidando las boletas...")

    def toggle_program_active(self) -> None:
        if self._selected_program is None:
            return
        current = next((p for p in self.programs_model.rows() if p.id == self._selected_program), None)
        if current is None:
            return
        self._run(
            Worker(self.services.catalog.set_program_active, self._selected_program, not current.active),
            self._on_program_saved,
            "Guardando el programa y revalidando las boletas...",
        )

    def _on_program_saved(self, changed: object) -> None:
        self._clear_program_form()
        self.load_catalogs()
        self.status_label.setText("Programa guardado. " + changed_text(changed))
        self._catalog_saved()

    def _pick_alias(self) -> None:
        row_index = source_row_of(self.aliases_view)
        if row_index is None:
            return
        row = self.aliases_model.row(row_index)
        self._selected_alias = (row["program_id"], row["alias"])
        index = self.alias_program_combo.findData(row["program_id"])
        if index >= 0:
            self.alias_program_combo.setCurrentIndex(index)
        self.alias_text_edit.setText(row["alias"])
        self.alias_priority_spin.setValue(int(row["priority"]))
        self.remove_alias_button.setEnabled(not self.tasks.is_busy())

    def _validate_alias(self) -> bool:
        message = ""
        alias = self.alias_text_edit.text()
        if alias.strip():
            try:
                validate_alias_fields(alias, self.alias_priority_spin.value())
            except AppError as error:
                message = form_error_text(error)
        self.alias_error.setText(message)
        valid = bool(alias.strip()) and self.alias_program_combo.count() > 0 and not message
        self.add_alias_button.setEnabled(valid and not self.tasks.is_busy())
        return valid

    def add_alias(self) -> None:
        if not self._validate_alias():
            return
        program_id = self.alias_program_combo.currentData()
        self._run(
            Worker(
                self.services.catalog.add_alias,
                program_id,
                self.alias_text_edit.text(),
                self.alias_priority_spin.value(),
            ),
            self._on_alias_saved,
            "Guardando el alias y revalidando las boletas...",
        )

    def remove_alias(self) -> None:
        if self._selected_alias is None:
            return
        program_id, alias = self._selected_alias
        self._run(
            Worker(self.services.catalog.remove_alias, program_id, alias),
            self._on_alias_saved,
            "Quitando el alias y revalidando las boletas...",
        )

    def _on_alias_saved(self, changed: object) -> None:
        self._selected_alias = None
        self.alias_text_edit.clear()
        self.remove_alias_button.setEnabled(False)
        self.load_catalogs()
        self.status_label.setText("Alias guardado. " + changed_text(changed))
        self._catalog_saved()

    def _build_reference_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            muted_label(
                "Rango del valor hora por programa y año. Una boleta fuera del rango solo recibe una "
                "advertencia: el monto nunca se corrige."
            )
        )
        self.reference_model = RecordTableModel(
            [
                Column("program", "Programa"),
                Column("year", "Año", str, numeric=True),
                Column("min_hourly", "Mínimo por hora", format_int, numeric=True),
                Column("max_hourly", "Máximo por hora", format_int, numeric=True),
            ],
            self,
        )
        self.reference_view = make_table_view(self.reference_model, self)
        layout.addWidget(self.reference_view, 1)
        layout.addLayout(
            self._import_row(
                "valores de referencia",
                self.services.catalog.import_reference_rates,
                self.services.catalog.write_reference_template,
                "valores_hora_referencia.xlsx",
            )
        )
        return page

    def _build_providers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            muted_label(
                "Nombre canónico de cada prestador por RUT. Se usa en los informes y se ofrece como "
                "sugerencia al revisar; nunca reemplaza solo lo que dice la boleta."
            )
        )
        self.providers_model = RecordTableModel(
            [
                Column("rut", "RUT", format_rut),
                Column("canonical_name", "Nombre confirmado"),
                Column("confirmed_at", "Confirmado", format_datetime),
            ],
            self,
        )
        self.providers_view = make_table_view(self.providers_model, self)
        layout.addWidget(self.providers_view, 1)
        layout.addLayout(
            self._import_row(
                "prestadores",
                self.services.catalog.import_providers,
                self.services.catalog.write_provider_template,
                "prestadores.xlsx",
            )
        )
        return page

    def _import_row(
        self,
        what: str,
        importer: Callable[[Path], ImportReport],
        template_writer: Callable[[Path], Path],
        template_name: str,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        template_button = QPushButton("Descargar planilla modelo...")
        template_button.setToolTip(
            f"Guarda una planilla con los {what} actuales para completarla y volver a importarla"
        )
        template_button.clicked.connect(lambda: self.save_template(template_writer, template_name))
        import_button = QPushButton(f"Importar {what}...")
        import_button.clicked.connect(lambda: self.choose_import(what, importer))
        row.addWidget(template_button)
        row.addWidget(import_button)
        return row

    def load_catalogs(self) -> None:
        catalog = self.services.catalog
        programs = catalog.programs()
        names = {program.id: f"{program.folder_code} {program.short_name}" for program in programs}
        self.programs_model.set_rows(programs)
        self.aliases_model.set_rows(
            [
                {
                    "program_id": alias.program_id,
                    "program": names.get(alias.program_id, str(alias.program_id)),
                    "alias": alias.alias,
                    "priority": alias.priority,
                }
                for alias in catalog.aliases()
            ]
        )
        self.alias_program_combo.clear()
        for program in programs:
            if program.active or program.id == self.alias_program_combo.currentData():
                self.alias_program_combo.addItem(f"{program.folder_code} {program.short_name}", program.id)
        self.rates_model.set_rows(
            [{"year": year, "rate_bp": rate} for year, rate in sorted(catalog.retention_rates().items())]
        )
        self.reference_model.set_rows(
            [
                {
                    "program": names.get(rate.program_id, str(rate.program_id)),
                    "year": rate.year,
                    "min_hourly": rate.min_hourly,
                    "max_hourly": rate.max_hourly,
                }
                for rate in catalog.reference_rates()
            ]
        )
        self.providers_model.set_rows(catalog.providers())
        stretched: tuple[tuple[QTableView, int | None], ...] = (
            (self.programs_view, 1),
            (self.aliases_view, 1),
            (self.rates_view, None),
            (self.reference_view, 0),
            (self.providers_view, 1),
        )
        for view, section in stretched:
            fit_columns_once(view)
            stretch_column(view, section)
        self._validate_program()
        self._validate_alias()

    def choose_import(self, what: str, importer: Callable[[Path], ImportReport]) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, f"Importar {what}", str(self.services.export_dir), EXCEL_FILTER
        )
        if path:
            self.import_file(what, importer, Path(path))

    def import_file(self, what: str, importer: Callable[[Path], ImportReport], path: Path) -> None:
        self._run(
            Worker(importer, path),
            lambda report: self._on_imported(what, report),
            f"Importando {what} desde {path.name}...",
        )

    def _on_imported(self, what: str, report: object) -> None:
        if not isinstance(report, ImportReport):
            return
        self.load_catalogs()
        message = f"Importación de {what}: {report.message()}"
        self.status_label.setText(message)
        if report.imported > 0:
            self._catalog_saved()
        if not self.unattended:
            dialog = ImportReportDialog(f"Importación de {what}", message, report.rejected, self)
            dialog.open()

    def save_template(self, writer: Callable[[Path], Path], default_name: str) -> None:
        target, _selected = QFileDialog.getSaveFileName(
            self, "Guardar planilla modelo", str(self.services.export_dir / default_name), EXCEL_FILTER
        )
        if not target:
            return
        try:
            path = writer(Path(target))
        except AppError as error:
            self._on_failed(error.user_message)
            return
        self.status_label.setText(f"Planilla guardada en {path}.")
        if not self.unattended:
            notify(
                self, "Planilla modelo", f"Planilla guardada en {path}.", [("Abrir planilla", lambda: open_path(path))]
            )

    def _on_failed(self, message: str) -> None:
        self._task_ended()
        self.status_label.setText("La acción no se completó.")
        log.warning("%s", message)
        if not self.unattended:
            show_error(self, message)


def changed_text(changed: object) -> str:
    """Resultado de una revalidación para la línea de estado del diálogo."""
    count = changed if isinstance(changed, int) else 0
    if not count:
        return "Ninguna boleta cambió de estado."
    return plural(count, "boleta cambió de estado", "boletas cambiaron de estado") + "."
