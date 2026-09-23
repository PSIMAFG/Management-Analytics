"""Diálogos de edición validados: dato mensual, parámetros del semáforo y revisión de una importación.

Los diálogos no escriben en la base por su cuenta: reciben funciones del servicio
y muestran dentro del mismo diálogo el motivo de cualquier rechazo, de modo que
el usuario corrige sin perder lo que escribió.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kpi_monitor.domain.models import Indicator, Site, Thresholds
from kpi_monitor.errors import AppError
from kpi_monitor.services import ImportReport, ObservationRow
from kpi_monitor.ui.formatting import MONTHS
from kpi_monitor.ui.presenters import Cell, denominator_field, edit_text, format_number, numerator_hint, plural
from kpi_monitor.ui.widgets import Column, RecordTableModel, make_table_view, set_stretch_column

CHILE = QLocale(QLocale.Language.Spanish, QLocale.Country.Chile)


@dataclass(frozen=True)
class ObservationTarget:
    """Sede, indicador y mes del dato que se registra o corrige."""

    site_code: str
    indicator_code: str
    year: int
    month: int


def _error_label() -> QLabel:
    label = QLabel("", objectName="errorText")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


def _hint(text: str = "") -> QLabel:
    label = QLabel(text, objectName="hint")
    label.setWordWrap(True)
    return label


def current_text(row: ObservationRow) -> str:
    """Describe el dato guardado que se va a corregir."""
    if not row.reported:
        return f"Dato actual: marcado como no informado (origen: {row.origin.lower()})."
    parts = [f"numerador {format_number(row.numerator)}"]
    if row.denominator is not None:
        parts.append(f"denominador {format_number(row.denominator)}")
    return f"Dato actual: {' y '.join(parts)} (origen: {row.origin.lower()})."


class ObservationDialog(QDialog):
    """Registra o corrige el dato mensual de una sede con las mismas reglas de la importación."""

    def __init__(
        self,
        parent: QWidget | None,
        sites: Sequence[Site],
        years: Sequence[int],
        indicators_for_year: Callable[[int], list[Indicator]],
        load: Callable[[ObservationTarget], ObservationRow | None],
        save: Callable[[ObservationTarget, str, str, bool], None],
        initial: ObservationTarget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Registrar o corregir un dato")
        self.setMinimumWidth(500)
        self._indicators_for_year = indicators_for_year
        self._load = load
        self._save = save
        self._indicators: dict[str, Indicator] = {}
        self._loading = False
        self._denominator_enabled = True

        self.site_combo = QComboBox()
        for site in sites:
            self.site_combo.addItem(site.name, site.code)
        self.year_combo = QComboBox()
        for year in years:
            self.year_combo.addItem(str(year), year)
        self.month_combo = QComboBox()
        for number, name in enumerate(MONTHS, start=1):
            self.month_combo.addItem(name.capitalize(), number)
        self.indicator_combo = QComboBox()
        self.indicator_combo.setMinimumContentsLength(28)
        self.reported_check = QCheckBox("La sede informó el dato de este mes")
        self.numerator_edit = QLineEdit()
        self.denominator_edit = QLineEdit()
        for edit in (self.numerator_edit, self.denominator_edit):
            edit.setPlaceholderText("Por ejemplo 1.234 o 12,5")
        self.numerator_hint = _hint()
        self.denominator_hint = _hint()
        self.denominator_label = QLabel("Denominador")
        self.current_label = _hint()
        self.error_label = _error_label()

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow("Sede", self.site_combo)
        form.addRow("Indicador", self.indicator_combo)
        form.addRow("Año", self.year_combo)
        form.addRow("Mes", self.month_combo)
        form.addRow("", self.reported_check)
        form.addRow("Numerador", self.numerator_edit)
        form.addRow("", self.numerator_hint)
        form.addRow(self.denominator_label, self.denominator_edit)
        form.addRow("", self.denominator_hint)

        buttons = QDialogButtonBox()
        self.save_button = QPushButton("Guardar", objectName="primary")
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.try_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.current_label)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

        self._select(self.site_combo, initial.site_code)
        self._select(self.year_combo, initial.year)
        self._select(self.month_combo, initial.month)
        self._fill_indicators(initial.indicator_code)
        self.site_combo.currentIndexChanged.connect(self._reload)
        self.month_combo.currentIndexChanged.connect(self._reload)
        self.indicator_combo.currentIndexChanged.connect(self._reload)
        self.year_combo.currentIndexChanged.connect(self._on_year_changed)
        self.reported_check.toggled.connect(self._update_enabled)
        self._reload()

    @staticmethod
    def _select(combo: QComboBox, data: object) -> None:
        index = combo.findData(data)
        combo.setCurrentIndex(max(0, index))

    def target(self) -> ObservationTarget:
        return ObservationTarget(
            site_code=self.site_combo.currentData(),
            indicator_code=self.indicator_combo.currentData(),
            year=self.year_combo.currentData(),
            month=self.month_combo.currentData(),
        )

    def _fill_indicators(self, preferred: str | None) -> None:
        self._loading = True
        self.indicator_combo.clear()
        self._indicators = {ind.code: ind for ind in self._indicators_for_year(self.year_combo.currentData())}
        for ind in self._indicators.values():
            self.indicator_combo.addItem(f"{ind.code}  {ind.short_name}", ind.code)
        self._select(self.indicator_combo, preferred)
        self._loading = False

    def _on_year_changed(self) -> None:
        self._fill_indicators(self.indicator_combo.currentData())
        self._reload()

    def _reload(self) -> None:
        if self._loading:
            return
        self.error_label.setVisible(False)
        indicator = self._indicators.get(self.indicator_combo.currentData())
        if indicator is None:
            return
        field = denominator_field(indicator)
        self.denominator_label.setText(field.label)
        self.denominator_hint.setText(field.hint)
        self.numerator_hint.setText(numerator_hint(indicator))
        self._denominator_enabled = field.enabled
        current = self._load(self.target())
        if current is None:
            self.current_label.setText("No hay dato registrado para este mes; se creará uno nuevo.")
            self.reported_check.setChecked(True)
            self.numerator_edit.clear()
            self.denominator_edit.clear()
        else:
            self.current_label.setText(current_text(current))
            self.reported_check.setChecked(current.reported)
            self.numerator_edit.setText(edit_text(current.numerator))
            self.denominator_edit.setText(edit_text(current.denominator) if field.enabled else "")
        self._update_enabled()

    def _update_enabled(self) -> None:
        reported = self.reported_check.isChecked()
        self.numerator_edit.setEnabled(reported)
        self.denominator_edit.setEnabled(reported and self._denominator_enabled)

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def try_save(self) -> bool:
        """Guarda el dato; si el servicio lo rechaza, muestra el motivo y deja el diálogo abierto."""
        reported = self.reported_check.isChecked()
        numerator = self.numerator_edit.text().strip() if reported else ""
        denominator = self.denominator_edit.text().strip() if reported and self.denominator_edit.isEnabled() else ""
        if reported and not numerator:
            self.show_error("Ingrese el numerador o desmarque la casilla si la sede no informó el dato.")
            self.numerator_edit.setFocus()
            return False
        try:
            self._save(self.target(), numerator, denominator, reported)
        except AppError as error:
            self.show_error(error.user_message)
            return False
        self.accept()
        return True


class SettingsDialog(QDialog):
    """Umbrales del semáforo oficial y prevalencia usada para la población de referencia."""

    def __init__(
        self,
        parent: QWidget | None,
        thresholds: Thresholds,
        prevalence: float,
        save: Callable[[Thresholds, float], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Parámetros del semáforo")
        self.setMinimumWidth(460)
        self._save = save
        self.green_spin = self._spin(thresholds.green * 100, 50.0, 200.0)
        self.yellow_spin = self._spin(thresholds.yellow * 100, 1.0, 199.0)
        self.prevalence_spin = self._spin(prevalence * 100, 0.1, 100.0)
        self.error_label = _error_label()

        form = QFormLayout()
        form.addRow("Verde desde (cumplimiento)", self.green_spin)
        form.addRow("Amarillo desde (cumplimiento)", self.yellow_spin)
        form.addRow("Prevalencia para la población", self.prevalence_spin)

        buttons = QDialogButtonBox()
        save_button = QPushButton("Guardar y recalcular", objectName="primary")
        buttons.addButton(save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.try_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            _hint(
                "El semáforo se aplica al cumplimiento a la fecha: verde desde el primer umbral, amarillo desde "
                "el segundo y rojo bajo él. La prevalencia convierte la población de referencia de cada sede en "
                "el denominador de los indicadores de cobertura. Al guardar se recalculan todos los indicadores."
            )
        )
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

    @staticmethod
    def _spin(value: float, minimum: float, maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setLocale(CHILE)
        spin.setDecimals(1)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(1.0)
        spin.setSuffix(" %")
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setValue(value)
        return spin

    def values(self) -> tuple[float, float, float]:
        """Umbral verde, umbral amarillo y prevalencia como fracciones."""
        return (
            round(self.green_spin.value() / 100, 6),
            round(self.yellow_spin.value() / 100, 6),
            round(self.prevalence_spin.value() / 100, 6),
        )

    def try_save(self) -> bool:
        green, yellow, prevalence = self.values()
        try:
            self._save(Thresholds(green=green, yellow=yellow), prevalence)
        except AppError as error:
            self.error_label.setText(error.user_message)
            self.error_label.setVisible(True)
            return False
        self.accept()
        return True


class ImportPreviewDialog(QDialog):
    """Resultado de validar un archivo antes de importarlo, con las filas rechazadas y su motivo."""

    IMPORT_ALL = "all"
    IMPORT_VALID = "valid_only"

    def __init__(self, parent: QWidget | None, file_name: str, report: ImportReport) -> None:
        super().__init__(parent)
        self.setWindowTitle("Revisión del archivo a importar")
        self.resize(820, 460 if report.rejected else 220)
        self.choice: str | None = None
        rejected = len(report.rejected)

        title = QLabel(file_name, objectName="infoTitle")
        summary = QLabel(
            f"Filas leídas: {format_number(report.total_rows)}. Válidas: {format_number(report.valid_rows)}. "
            f"Con errores: {format_number(rejected)}."
        )
        message = _hint(report.message)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(summary)
        layout.addWidget(message)

        self.model = RecordTableModel(
            [
                Column("row", "Fila", lambda c: c.text, numeric=True, sort_key=lambda c: c.sort_value),
                Column("site", "Sede", lambda c: c.text),
                Column("indicator", "Indicador", lambda c: c.text),
                Column("period", "Mes-año", lambda c: c.text),
                Column("reason", "Motivo del rechazo", lambda c: c.text, tooltip=lambda c: c.text),
            ]
        )
        self.model.set_rows(
            [
                {
                    "row": Cell(str(r.row_number), float(r.row_number)),
                    "site": Cell(r.site or "-"),
                    "indicator": Cell(r.indicator or "-"),
                    "period": Cell(r.period),
                    "reason": Cell(r.reason),
                }
                for r in report.rejected
            ]
        )
        if rejected:
            layout.addWidget(QLabel(plural(rejected, "fila rechazada", "filas rechazadas"), objectName="tableCaption"))
            view = make_table_view(self.model, self)
            set_stretch_column(view, 4)
            layout.addWidget(view, 1)
        else:
            layout.addStretch(1)

        buttons = QDialogButtonBox()
        self.import_button = QPushButton("Importar", objectName="primary")
        self.import_button.setEnabled(report.valid_rows > 0 and not rejected)
        valid_label = plural(report.valid_rows, "la fila válida", "las filas válidas")
        self.valid_button = QPushButton(f"Importar solo {valid_label}")
        self.valid_button.setVisible(bool(rejected) and report.valid_rows > 0)
        buttons.addButton(self.import_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.valid_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        self.import_button.clicked.connect(lambda: self._choose(self.IMPORT_ALL))
        self.valid_button.clicked.connect(lambda: self._choose(self.IMPORT_VALID))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()
