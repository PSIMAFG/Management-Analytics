"""Diálogos de edición de datos maestros: ausencias, contratos y reporte de importación.

Todos validan a través de los servicios: si el servicio rechaza el dato, el
motivo se muestra dentro del mismo diálogo y no se guarda nada.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from optibox.domain.models import AbsenceRecord, AbsenceStatus, Staff
from optibox.domain.timegrid import SLOT_MINUTES, format_minute, format_range
from optibox.errors import AppError
from optibox.services.excel_service import ImportReport
from optibox.services.master_data_service import MasterDataService
from optibox.ui.dialogs import ask_confirmation, run_dialog, show_error
from optibox.ui.formatting import format_date, format_decimal, format_int
from optibox.ui.widgets import Column, RecordTableModel, hint_label, make_table_view, selected_source_row

log = logging.getLogger(__name__)

STATUS_LABELS = {
    AbsenceStatus.APPROVED: "Aprobada",
    AbsenceStatus.PENDING: "Pendiente",
    AbsenceStatus.REJECTED: "Rechazada",
}
DAY_START = 7 * 60
DAY_END = 20 * 60


def to_date(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


def to_qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def time_combo(selected: int) -> QComboBox:
    """Selector de hora en pasos de 15 minutos (evita horas mal escritas)."""
    combo = QComboBox()
    for minute in range(DAY_START, DAY_END + 1, SLOT_MINUTES):
        combo.addItem(format_minute(minute), minute)
    combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


def staff_combo(staff: tuple[Staff, ...]) -> QComboBox:
    combo = QComboBox()
    for person in sorted(staff, key=lambda p: p.code):
        combo.addItem(f"{person.code} {person.name}", person.code)
    return combo


def error_label() -> QLabel:
    label = QLabel("", objectName="formError")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


def dialog_buttons(dialog: QDialog, save_text: str = "Guardar") -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText(save_text)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
    buttons.rejected.connect(dialog.reject)
    return buttons


def absence_rows(records: tuple[AbsenceRecord, ...], names: dict[str, str]) -> list[dict[str, Any]]:
    """Filas de la tabla de ausencias con textos legibles."""
    rows: list[dict[str, Any]] = []
    for record in records:
        absence = record.absence
        full_day = absence.start is None or absence.end is None
        rows.append(
            {
                "id": record.id,
                "staff": absence.staff_code,
                "name": names.get(absence.staff_code, ""),
                "day": absence.day,
                "span": "Día completo" if full_day else format_range(absence.start or 0, absence.end or 0),
                "kind": absence.kind,
                "status": STATUS_LABELS[absence.status],
            }
        )
    return rows


class AbsenceFormDialog(QDialog):
    """Registro de una ausencia de día completo o parcial."""

    def __init__(self, service: MasterDataService, default_day: date, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.record: AbsenceRecord | None = None
        self.setWindowTitle("Registrar ausencia")
        self.setMinimumWidth(440)
        self.staff = staff_combo(service.staff())
        self.day = QDateEdit(calendarPopup=True)
        self.day.setDisplayFormat("dd-MM-yyyy")
        self.day.setDate(to_qdate(default_day))
        self.full_day = QCheckBox("Día completo")
        self.full_day.setChecked(True)
        self.start = time_combo(8 * 60)
        self.end = time_combo(13 * 60)
        self.full_day.toggled.connect(self._toggle_span)
        span = QHBoxLayout()
        span.addWidget(QLabel("Desde"))
        span.addWidget(self.start)
        span.addWidget(QLabel("hasta"))
        span.addWidget(self.end)
        span.addStretch(1)
        self.kind = QLineEdit()
        self.kind.setPlaceholderText("Por ejemplo: permiso administrativo, vacaciones")
        self.status = QComboBox()
        for status in (AbsenceStatus.PENDING, AbsenceStatus.APPROVED, AbsenceStatus.REJECTED):
            self.status.addItem(STATUS_LABELS[status], status.value)
        self.error = error_label()

        form = QFormLayout()
        form.addRow("Persona", self.staff)
        form.addRow("Fecha", self.day)
        form.addRow("", self.full_day)
        form.addRow("Franja", span)
        form.addRow("Motivo", self.kind)
        form.addRow("Estado", self.status)
        buttons = dialog_buttons(self)
        buttons.accepted.connect(self.save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint_label("Solo las ausencias aprobadas bloquean la agenda de la persona."))
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self._toggle_span(True)

    def _toggle_span(self, full_day: bool) -> None:
        self.start.setEnabled(not full_day)
        self.end.setEnabled(not full_day)

    def show_problem(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(True)

    def save(self) -> bool:
        """Valida y guarda; si hay un problema lo muestra en el diálogo y no lo cierra."""
        start = None if self.full_day.isChecked() else int(self.start.currentData())
        end = None if self.full_day.isChecked() else int(self.end.currentData())
        if start is not None and end is not None and start >= end:
            self.show_problem("La hora de inicio debe ser anterior a la hora de término.")
            return False
        try:
            self.record = self.service.add_absence(
                str(self.staff.currentData()),
                to_date(self.day.date()),
                self.kind.text(),
                AbsenceStatus(str(self.status.currentData())),
                start,
                end,
            )
        except AppError as error:
            self.show_problem(error.user_message)
            return False
        self.accept()
        return True


class AbsencesDialog(QDialog):
    """Lista de ausencias con alta, cambio de estado y eliminación."""

    def __init__(self, service: MasterDataService, default_day: date, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.default_day = default_day
        self.changed = False
        self.setWindowTitle("Ausencias del personal")
        self.resize(780, 440)
        self.model = RecordTableModel(
            [
                Column("staff", "Persona"),
                Column("name", "Nombre"),
                Column("day", "Fecha", format_date),
                Column("span", "Franja"),
                Column("kind", "Motivo"),
                Column("status", "Estado"),
            ]
        )
        self.table = make_table_view(self.model, self)
        add = QPushButton("Registrar ausencia", objectName="primary")
        add.clicked.connect(self.add_absence)
        approve = QPushButton("Aprobar")
        approve.clicked.connect(lambda: self.set_status(AbsenceStatus.APPROVED))
        pending = QPushButton("Dejar pendiente")
        pending.clicked.connect(lambda: self.set_status(AbsenceStatus.PENDING))
        reject = QPushButton("Rechazar")
        reject.clicked.connect(lambda: self.set_status(AbsenceStatus.REJECTED))
        delete = QPushButton("Eliminar")
        delete.clicked.connect(self.delete_absence)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        for button in (add, approve, pending, reject, delete):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(
            hint_label(
                "Solo las ausencias aprobadas bloquean la agenda. Los cambios se consideran en la próxima "
                "optimización; las corridas guardadas no cambian."
            )
        )
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        try:
            names = {person.code: person.name for person in self.service.staff()}
            self.model.set_rows(absence_rows(self.service.absences(), names))
        except AppError as error:
            show_error(self, error.user_message)

    def _selected_id(self) -> int | None:
        row = selected_source_row(self.table)
        if row is None:
            show_error(self, "Seleccione una ausencia de la lista.", "Ausencias")
            return None
        return int(self.model.row(row)["id"])

    def add_absence(self) -> None:
        dialog = AbsenceFormDialog(self.service, self.default_day, self)
        if run_dialog(dialog):
            self.changed = True
            self.reload()

    def set_status(self, status: AbsenceStatus) -> None:
        absence_id = self._selected_id()
        if absence_id is None:
            return
        try:
            self.service.set_absence_status(absence_id, status)
        except AppError as error:
            show_error(self, error.user_message)
            return
        self.changed = True
        self.reload()

    def delete_absence(self) -> None:
        absence_id = self._selected_id()
        if absence_id is None or not ask_confirmation(self, "¿Eliminar la ausencia seleccionada?"):
            return
        try:
            self.service.delete_absence(absence_id)
        except AppError as error:
            show_error(self, error.user_message)
            return
        self.changed = True
        self.reload()


def contract_history(person: Staff) -> str:
    """Resumen de las versiones de contrato de una persona, de la más antigua a la vigente."""
    if not person.contracts:
        return "Sin contratos registrados."
    lines = []
    for contract in sorted(person.contracts, key=lambda c: c.valid_from):
        until = f"hasta {format_date(contract.valid_to)}" if contract.valid_to else "sin término"
        hours = format_decimal(contract.weekly_minutes / 60, 2).removesuffix(",00")
        lines.append(f"Desde {format_date(contract.valid_from)}, {until}: {hours} h semanales")
    return "\n".join(lines)


class ContractDialog(QDialog):
    """Nueva versión de contrato: la versión abierta anterior se cierra el día previo."""

    def __init__(self, service: MasterDataService, default_day: date, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.saved = False
        self.setWindowTitle("Nueva versión de contrato")
        self.setMinimumWidth(480)
        self.people = {person.code: person for person in service.staff()}
        self.staff = staff_combo(tuple(self.people.values()))
        self.staff.currentIndexChanged.connect(self._show_history)
        self.history = QLabel("")
        self.history.setWordWrap(True)
        self.valid_from = QDateEdit(calendarPopup=True)
        self.valid_from.setDisplayFormat("dd-MM-yyyy")
        self.valid_from.setDate(to_qdate(default_day))
        self.hours = QDoubleSpinBox()
        self.hours.setRange(0.25, 60.0)
        self.hours.setSingleStep(0.25)
        self.hours.setDecimals(2)
        self.hours.setSuffix(" h")
        self.hours.setValue(44.0)
        self.error = error_label()
        form = QFormLayout()
        form.addRow("Persona", self.staff)
        form.addRow("Contratos", self.history)
        form.addRow("Vigente desde", self.valid_from)
        form.addRow("Horas semanales", self.hours)
        buttons = dialog_buttons(self)
        buttons.accepted.connect(self.save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(
            hint_label(
                "La planificación usa el contrato vigente en la semana. Las horas deben equivaler a múltiplos de "
                "15 minutos."
            )
        )
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self._show_history()

    def _show_history(self) -> None:
        person = self.people.get(str(self.staff.currentData()))
        self.history.setText(contract_history(person) if person else "")
        if person and person.contracts:
            latest = max(person.contracts, key=lambda c: c.valid_from)
            self.hours.setValue(latest.weekly_minutes / 60)

    def save(self) -> bool:
        try:
            self.service.add_contract(
                str(self.staff.currentData()), to_date(self.valid_from.date()), self.hours.value()
            )
        except AppError as error:
            self.error.setText(error.user_message)
            self.error.setVisible(True)
            return False
        self.saved = True
        self.accept()
        return True


class ImportReportDialog(QDialog):
    """Detalle de una importación rechazada: hoja, fila y motivo de cada rechazo."""

    def __init__(self, report: ImportReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Importación rechazada")
        self.resize(760, 420)
        self.model = RecordTableModel(
            [
                Column("sheet", "Hoja"),
                Column("row", "Fila", lambda r: format_int(r) if r else "Toda la hoja", numeric=True),
                Column("reason", "Motivo"),
            ],
            self,
        )
        self.model.set_rows(report.rejected)
        table = make_table_view(self.model, self)
        summary = QLabel(report.summary)
        summary.setWordWrap(True)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("Cerrar")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(table, 1)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
