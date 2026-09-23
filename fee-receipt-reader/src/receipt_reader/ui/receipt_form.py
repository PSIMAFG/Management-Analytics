"""Formulario de revisión de una boleta.

Cada campo muestra de dónde salió el dato (texto nativo, OCR con su
confianza, carpeta o usuario), se valida mientras se escribe con los mismos
parsers que usa la lectura y marca las incidencias que lo afectan. Las
sugerencias se muestran junto al campo y solo se aplican si el usuario pulsa
"Usar"; aun así, no se guardan hasta pulsar "Guardar corrección".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from PySide6.QtCore import QDate, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from receipt_reader.domain.forms import EDITABLE_FIELDS, parse_field
from receipt_reader.domain.models import FIELD_LABELS, FieldSource, FieldTrace, Program, ReceiptStatus, display_value
from receipt_reader.domain.retention import RetentionTable, expected_retention
from receipt_reader.errors import AppError
from receipt_reader.services.review import ReceiptDetail
from receipt_reader.ui.formatting import format_clp, format_pct, keep_amounts_together

FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Prestador", ("issuer_rut", "issuer_name")),
    ("Documento", ("folio", "issue_date", "receiver_rut", "receiver_name")),
    ("Montos", ("gross", "retention", "net", "printed_rate_bp")),
    ("Imputación", ("program_id", "service_period", "payment_period", "hours", "workday_type", "decree", "gloss")),
)
FORM_LABELS = {
    **FIELD_LABELS,
    "issuer_name": "Nombre",
    "receiver_name": "Nombre receptor",
    "printed_rate_bp": "Tasa impresa (%)",
}
PLACEHOLDERS = {
    "issuer_rut": "RUT con dígito verificador",
    "receiver_rut": "RUT con dígito verificador",
    "folio": "Número de la boleta",
    "issue_date": "dd-mm-aaaa",
    "service_period": "aaaa-mm",
    "payment_period": "aaaa-mm",
    "gross": "Pesos, con punto de miles",
    "retention": "Pesos, con punto de miles",
    "net": "Pesos, con punto de miles",
    "printed_rate_bp": "Por ejemplo 14,50",
    "hours": "Por ejemplo 22 o 22,5",
    "decree": "número/año",
}
WORKDAY_OPTIONS = (("", "Sin dato"), ("semanal", "Semanal"), ("mensual", "Mensual"))
AMOUNT_FIELDS = frozenset({"gross", "retention", "net", "issue_date", "printed_rate_bp"})
LABEL_WIDTH = 108


def normalize_form_text(name: str, text: str | None) -> str:
    """Texto comparable de un campo: sin espacios de más (la glosa conserva sus líneas)."""
    raw = text or ""
    if name == "gloss":
        return "\n".join(" ".join(line.split()) for line in raw.splitlines() if line.strip())
    return " ".join(raw.split())


def changed_fields(original: Mapping[str, str], current: Mapping[str, str]) -> dict[str, str]:
    """Campos cuyo texto cambió respecto de lo cargado (solo esos se envían y se auditan)."""
    return {
        name: text
        for name, text in current.items()
        if normalize_form_text(name, text) != normalize_form_text(name, original.get(name, ""))
    }


def field_error(name: str, text: str | None) -> str | None:
    """Mensaje de error del campo o None si el texto es válido (vacío también es válido: sin valor)."""
    try:
        parse_field(name, text)
    except AppError as error:
        return error.user_message
    except ValueError:
        return "El valor no tiene un formato válido."
    return None


def source_caption(trace: FieldTrace | None, low_threshold: float) -> tuple[str, bool]:
    """Origen breve de un dato y si su confianza es baja (solo el OCR tiene confianza variable)."""
    if trace is None:
        return "Sin lectura", False
    if trace.source is FieldSource.OCR and trace.confidence is not None:
        return f"OCR {format_pct(trace.confidence, 0)}", trace.confidence < low_threshold
    return trace.source.label, False


def amount_check(values: Mapping[str, str], rates: RetentionTable, tolerance: int = 1) -> tuple[str, str]:
    """Control en vivo de la tripleta: retención y líquido que corresponden al bruto con la tasa del año.

    La tasa sale de la misma tabla que usa la validación (después del último año registrado
    rige la última tasa). Devuelve el texto y su tono ("good", "warning" o "muted").
    """
    try:
        gross = parse_field("gross", values.get("gross"))
        issued = parse_field("issue_date", values.get("issue_date"))
    except AppError:
        return "", "muted"
    if gross is None:
        return "Sin monto bruto no se puede controlar la retención.", "muted"
    if not isinstance(issued, date):
        return "Falta la fecha de emisión para saber la tasa del año.", "muted"
    rate = rates.rate_for(issued.year)
    if rate is None:
        return f"No hay tasa legal registrada para {issued.year}.", "warning"
    retention_expected = expected_retention(gross, rate)
    net_expected = gross - retention_expected
    rate_text = display_value("printed_rate_bp", rate)
    try:
        retention = parse_field("retention", values.get("retention"))
        net = parse_field("net", values.get("net"))
    except AppError:
        retention = net = None
    if (
        retention is not None
        and net is not None
        and abs(retention - retention_expected) <= tolerance
        and net == gross - retention
    ):
        return f"Montos coherentes con la tasa legal de {issued.year} ({rate_text}).", "good"
    return (
        f"Con la tasa legal de {issued.year} ({rate_text}) corresponden una retención de "
        f"{format_clp(retention_expected)} y un líquido de {format_clp(net_expected)}.",
        "warning",
    )


class _Editor:
    """Adaptador común de los distintos controles de edición."""

    widget: QWidget

    def text(self) -> str:
        raise NotImplementedError

    def set_text(self, text: str) -> None:
        raise NotImplementedError

    def styled_widget(self) -> QWidget:
        return self.widget

    def set_read_only(self, read_only: bool) -> None:
        self.widget.setEnabled(not read_only)

    def set_state(self, state: str) -> None:
        target = self.styled_widget()
        if target.property("state") == state:
            return
        target.setProperty("state", state)
        repolish(target)


def repolish(widget: QWidget) -> None:
    """Vuelve a aplicar la hoja de estilo (los selectores por propiedad no se reevalúan solos)."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class _LineEditor(_Editor):
    def __init__(self, name: str, on_change: Callable[..., None]) -> None:
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(PLACEHOLDERS.get(name, ""))
        self.edit.textChanged.connect(on_change)
        self.widget = self.edit

    def text(self) -> str:
        return self.edit.text()

    def set_text(self, text: str) -> None:
        self.edit.setText(text)
        self.edit.setCursorPosition(0)

    def set_read_only(self, read_only: bool) -> None:
        self.edit.setReadOnly(read_only)
        repolish(self.edit)


class _DateEditor(_Editor):
    """Fecha escrita (dd-mm-aaaa) con un calendario opcional para elegirla."""

    def __init__(self, name: str, on_change: Callable[..., None]) -> None:
        self.widget = QWidget()
        layout = QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(PLACEHOLDERS.get(name, ""))
        self.edit.textChanged.connect(on_change)
        self.button = QToolButton()
        self.button.setText("Elegir")
        self.button.setToolTip("Elegir la fecha en un calendario")
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(False)
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.clicked.connect(self._pick)
        menu = QMenu(self.button)
        action = QWidgetAction(menu)
        action.setDefaultWidget(self.calendar)
        menu.addAction(action)
        menu.aboutToShow.connect(self._sync_calendar)
        self.menu = menu
        self.button.setMenu(menu)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def styled_widget(self) -> QWidget:
        return self.edit

    def text(self) -> str:
        return self.edit.text()

    def set_text(self, text: str) -> None:
        self.edit.setText(text)

    def set_read_only(self, read_only: bool) -> None:
        self.edit.setReadOnly(read_only)
        self.button.setEnabled(not read_only)
        repolish(self.edit)

    def _sync_calendar(self) -> None:
        try:
            current = parse_field("issue_date", self.edit.text())
        except AppError:
            current = None
        if isinstance(current, date):
            self.calendar.setSelectedDate(QDate(current.year, current.month, current.day))

    def _pick(self, value: QDate) -> None:
        self.edit.setText(value.toString("dd-MM-yyyy"))
        self.menu.close()


class _ComboEditor(_Editor):
    def __init__(self, options: Sequence[tuple[str, str]], on_change: Callable[..., None]) -> None:
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(10)
        self.set_options(options)
        self.combo.currentIndexChanged.connect(on_change)
        self.widget = self.combo

    def set_options(self, options: Sequence[tuple[str, str]]) -> None:
        current = self.text() if self.combo.count() else ""
        with QSignalBlocker(self.combo):
            self.combo.clear()
            for value, label in options:
                self.combo.addItem(label, value)
            self._select(current)

    def _select(self, value: str) -> None:
        index = self.combo.findData(value)
        if index < 0 and value:
            self.combo.addItem(f"Valor no reconocido ({value})", value)
            index = self.combo.count() - 1
        self.combo.setCurrentIndex(max(index, 0))

    def text(self) -> str:
        data = self.combo.currentData()
        return "" if data is None else str(data)

    def set_text(self, text: str) -> None:
        self._select(text)


class _TextEditor(_Editor):
    def __init__(self, on_change: Callable[..., None]) -> None:
        self.edit = QPlainTextEdit()
        self.edit.setTabChangesFocus(True)
        self.edit.setFixedHeight(96)
        self.edit.textChanged.connect(on_change)
        self.widget = self.edit

    def text(self) -> str:
        return self.edit.toPlainText()

    def set_text(self, text: str) -> None:
        self.edit.setPlainText(text)

    def set_read_only(self, read_only: bool) -> None:
        self.edit.setReadOnly(read_only)
        repolish(self.edit)


@dataclass
class FieldRow:
    """Controles de un campo del formulario."""

    name: str
    editor: _Editor
    label: QLabel
    source: QLabel
    error: QLabel
    note: QLabel
    suggestion_box: QFrame
    suggestion_label: QLabel
    suggestion_text: str = ""
    issue_text: str = field(default="")


def program_options(programs: Sequence[Program]) -> list[tuple[str, str]]:
    ordered = sorted(programs, key=lambda item: item.folder_code)
    return [("", "Sin programa"), *((str(p.id), f"{p.folder_code} {p.short_name}") for p in ordered)]


class ReceiptForm(QWidget):
    """Formulario editable de una boleta con validación en vivo."""

    state_changed = Signal()
    confirm_name_requested = Signal(str, str)

    def __init__(
        self,
        programs: Sequence[Program],
        rates_bp: Mapping[int, int],
        *,
        ocr_threshold: float = 0.85,
        retention_tolerance: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._programs = list(programs)
        self._rates = RetentionTable(rates_bp)
        self._ocr_threshold = ocr_threshold
        self._tolerance = retention_tolerance
        self._detail: ReceiptDetail | None = None
        self._original: dict[str, str] = dict.fromkeys(EDITABLE_FIELDS, "")
        self._loading = False
        self._read_only = False
        self.rows: dict[str, FieldRow] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(objectName="plain")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("plainViewport")
        self.scroll.viewport().setObjectName("plainViewport")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(6)
        for title, names in FIELD_GROUPS:
            layout.addWidget(QLabel(title, objectName="subsectionTitle"))
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(6)
            grid.setColumnMinimumWidth(0, LABEL_WIDTH)
            grid.setColumnStretch(1, 1)
            for position, name in enumerate(names):
                self._add_row(grid, position, name)
            layout.addLayout(grid)
            if title == "Prestador":
                self.confirm_name_button = QPushButton("Confirmar este nombre", objectName="link")
                self.confirm_name_button.setToolTip(
                    "Fija el nombre canónico del prestador para este RUT. Se usa en los informes y se "
                    "sugiere en sus próximas boletas."
                )
                self.confirm_name_button.clicked.connect(self._request_confirm_name)
                row = QHBoxLayout()
                row.addSpacing(LABEL_WIDTH + 8)
                row.addWidget(self.confirm_name_button, 1)
                layout.addLayout(row)
            if title == "Montos":
                self.amount_label = QLabel("", objectName="muted")
                self.amount_label.setWordWrap(True)
                row = QHBoxLayout()
                row.addSpacing(LABEL_WIDTH + 8)
                row.addWidget(self.amount_label, 1)
                layout.addLayout(row)
        layout.addStretch(1)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll)
        self.clear()

    def _change_handler(self, name: str) -> Callable[..., None]:
        # Las señales de Qt pasan el valor nuevo como argumento; el campo se fija aquí.
        def handler(*_args: object) -> None:
            self._on_edit(name)

        return handler

    def _add_row(self, grid: QGridLayout, position: int, name: str) -> None:
        on_change = self._change_handler(name)
        editor: _Editor
        if name == "program_id":
            editor = _ComboEditor(program_options(self._programs), on_change)
        elif name == "workday_type":
            editor = _ComboEditor(WORKDAY_OPTIONS, on_change)
        elif name == "gloss":
            editor = _TextEditor(on_change)
        elif name == "issue_date":
            editor = _DateEditor(name, on_change)
        else:
            editor = _LineEditor(name, on_change)
        label = QLabel(FORM_LABELS.get(name, name), objectName="fieldLabel")
        label.setWordWrap(True)
        source = QLabel("", objectName="fieldSource")
        left = QVBoxLayout()
        left.setSpacing(0)
        left.setContentsMargins(0, 3, 0, 0)
        left.addWidget(label)
        left.addWidget(source)
        left.addStretch(1)
        error = QLabel("", objectName="fieldError")
        error.setWordWrap(True)
        note = QLabel("", objectName="fieldNote")
        note.setWordWrap(True)
        suggestion_box = QFrame(objectName="suggestion")
        box_layout = QHBoxLayout(suggestion_box)
        box_layout.setContentsMargins(6, 3, 6, 3)
        suggestion_label = QLabel("")
        suggestion_label.setWordWrap(True)
        suggestion_label.setObjectName("small")
        use_button = QPushButton("Usar", objectName="link")
        use_button.setToolTip("Copia la sugerencia al campo (se guarda con Guardar corrección)")
        use_button.clicked.connect(lambda _checked=False, name=name: self.apply_suggestion(name))
        box_layout.addWidget(suggestion_label, 1)
        box_layout.addWidget(use_button, 0, Qt.AlignmentFlag.AlignTop)
        right = QVBoxLayout()
        right.setSpacing(2)
        right.addWidget(editor.widget)
        right.addWidget(error)
        right.addWidget(note)
        right.addWidget(suggestion_box)
        grid.addLayout(left, position, 0)
        grid.addLayout(right, position, 1)
        editor.widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rows[name] = FieldRow(name, editor, label, source, error, note, suggestion_box, suggestion_label)

    # Catálogos

    def set_catalog(
        self, programs: Sequence[Program], rates_bp: Mapping[int, int], ocr_threshold: float, tolerance: int
    ) -> None:
        self._programs = list(programs)
        self._rates = RetentionTable(rates_bp)
        self._ocr_threshold = ocr_threshold
        self._tolerance = tolerance
        editor = self.rows["program_id"].editor
        if isinstance(editor, _ComboEditor):
            editor.set_options(program_options(self._programs))

    # Carga

    def clear(self) -> None:
        self._detail = None
        self._original = dict.fromkeys(EDITABLE_FIELDS, "")
        self._loading = True
        for row in self.rows.values():
            row.editor.set_text("")
            row.source.setText("")
            row.issue_text = ""
            row.suggestion_text = ""
            row.suggestion_box.hide()
        self._loading = False
        self._set_read_only(True)
        self._refresh_states()

    def load(self, detail: ReceiptDetail) -> None:
        """Carga la boleta y deja el formulario sin cambios pendientes."""
        self._detail = detail
        self._original = {name: detail.form_values.get(name, "") for name in EDITABLE_FIELDS}
        record = detail.record
        issues: dict[str, list[str]] = {}
        for issue in record.issues:
            if issue.field:
                issues.setdefault(issue.field, []).append(issue.message)
        suggestions = {suggestion.field: suggestion for suggestion in detail.suggestions}
        self._loading = True
        for name, row in self.rows.items():
            row.editor.set_text(self._original[name])
            caption, low = source_caption(record.traces.get(name), self._ocr_threshold)
            row.source.setText(caption if self._original[name] or name in record.traces else "")
            row.source.setObjectName("fieldSourceLow" if low else "fieldSource")
            repolish(row.source)
            row.issue_text = " ".join(issues.get(name, []))
            suggestion = suggestions.get(name)
            if suggestion is None:
                row.suggestion_text = ""
                row.suggestion_box.hide()
            else:
                row.suggestion_text = suggestion.text
                shown = self._suggestion_display(name, suggestion.text)
                row.suggestion_label.setText(f"Sugerencia: {shown}. {suggestion.reason}")
                row.suggestion_box.show()
        self._loading = False
        self._set_read_only(record.status is ReceiptStatus.DISCARDED)
        self._refresh_states()
        self.scroll.verticalScrollBar().setValue(0)

    def _suggestion_display(self, name: str, text: str) -> str:
        if name == "program_id":
            labels = dict(program_options(self._programs))
            return labels.get(text, text)
        return text

    def _set_read_only(self, read_only: bool) -> None:
        self._read_only = read_only
        for row in self.rows.values():
            row.editor.set_read_only(read_only)
        self.confirm_name_button.setVisible(not read_only and self._detail is not None)

    # Edición

    def _on_edit(self, name: str) -> None:
        if self._loading:
            return
        self._refresh_field(name)
        if name in AMOUNT_FIELDS:
            self._refresh_amount_check()
        if name in ("issuer_rut", "issuer_name"):
            self._refresh_confirm_button()
        self.state_changed.emit()

    def _refresh_states(self) -> None:
        for name in self.rows:
            self._refresh_field(name)
        self._refresh_amount_check()
        self._refresh_confirm_button()
        self.state_changed.emit()

    def _refresh_field(self, name: str, forced_error: str | None = None) -> None:
        row = self.rows[name]
        error = forced_error or (field_error(name, row.editor.text()) if self._detail is not None else None)
        row.error.setText(error or "")
        row.error.setVisible(bool(error))
        row.note.setText(keep_amounts_together(row.issue_text))
        row.note.setVisible(bool(row.issue_text))
        row.editor.set_state("error" if error else ("flag" if row.issue_text else ""))

    def _refresh_amount_check(self) -> None:
        if self._detail is None:
            self.amount_label.setText("")
            return
        text, tone = amount_check(self.values(), self._rates, self._tolerance)
        self.amount_label.setText(keep_amounts_together(text))
        self.amount_label.setObjectName({"good": "fieldSource", "warning": "fieldNote"}.get(tone, "fieldSource"))
        repolish(self.amount_label)

    def _refresh_confirm_button(self) -> None:
        values = self.values()
        valid = (
            bool(values["issuer_rut"].strip())
            and bool(values["issuer_name"].strip())
            and field_error("issuer_rut", values["issuer_rut"]) is None
            and field_error("issuer_name", values["issuer_name"]) is None
        )
        self.confirm_name_button.setEnabled(valid and not self._read_only)

    def _request_confirm_name(self) -> None:
        values = self.values()
        self.confirm_name_requested.emit(values["issuer_rut"], values["issuer_name"])

    def apply_suggestion(self, name: str) -> None:
        row = self.rows[name]
        if row.suggestion_text and not self._read_only:
            row.editor.set_text(row.suggestion_text)

    def apply_errors(self, errors: Mapping[str, str]) -> None:
        """Marca los errores que devolvió el servicio al guardar."""
        for name, message in errors.items():
            if name in self.rows:
                self._refresh_field(name, forced_error=message)

    # Consultas

    @property
    def detail(self) -> ReceiptDetail | None:
        return self._detail

    def values(self) -> dict[str, str]:
        return {name: row.editor.text() for name, row in self.rows.items()}

    def changed_values(self) -> dict[str, str]:
        return changed_fields(self._original, self.values())

    def errors(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for name, text in self.values().items():
            message = field_error(name, text)
            if message:
                found[name] = message
        return found

    def is_dirty(self) -> bool:
        return self._detail is not None and bool(self.changed_values())

    def has_errors(self) -> bool:
        return bool(self.errors())

    def is_read_only(self) -> bool:
        return self._read_only

    def set_field_text(self, name: str, text: str) -> None:
        self.rows[name].editor.set_text(text)

    def field_error_text(self, name: str) -> str:
        return self.rows[name].error.text()
