"""Mensajes al usuario, diálogos breves y captura global de errores no controlados."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSplashScreen,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from receipt_reader import APP_TITLE
from receipt_reader.domain.records import RejectedRow
from receipt_reader.errors import UNEXPECTED_ERROR
from receipt_reader.ui.style import BORDER, PRIMARY, SURFACE, TEXT_MUTED

log = logging.getLogger(__name__)

# Motivos frecuentes que se ofrecen al descartar (el usuario puede escribir otro).
DISCARD_REASONS = (
    "Copia de una boleta ya registrada",
    "No corresponde a una boleta de honorarios",
    "Boleta anulada por el emisor",
    "Corresponde a otra organización",
)
MIN_REASON_LENGTH = 3


def _message_box(parent: QWidget | None, icon: QMessageBox.Icon, title: str, message: str) -> QMessageBox:
    # Botones con texto propio: los estándar de Qt quedan en inglés si no hay traducciones instaladas.
    return QMessageBox(icon, title, message, QMessageBox.StandardButton.NoButton, parent)


def show_error(parent: QWidget | None, message: str, title: str = "No se pudo completar la acción") -> None:
    box = _message_box(parent, QMessageBox.Icon.Warning, title, message)
    box.addButton("Aceptar", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def ask_confirmation(parent: QWidget | None, message: str, title: str = "Confirmar") -> bool:
    box = _message_box(parent, QMessageBox.Icon.Question, title, message)
    yes = box.addButton("Sí", QMessageBox.ButtonRole.YesRole)
    no = box.addButton("No", QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


def startup_notice(message: str) -> QSplashScreen:
    """Aviso mientras se prepara la base en el primer inicio (la ventana aún no existe)."""
    pixmap = QPixmap(460, 120)
    pixmap.fill(QColor(SURFACE))
    painter = QPainter(pixmap)
    painter.setPen(QColor(BORDER))
    painter.drawRect(0, 0, pixmap.width() - 1, pixmap.height() - 1)
    painter.setPen(QColor(PRIMARY))
    painter.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
    painter.drawText(24, 48, APP_TITLE)
    painter.setPen(QColor(TEXT_MUTED))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(24, 80, message)
    painter.end()
    splash = QSplashScreen(pixmap)
    splash.show()
    QApplication.processEvents()
    return splash


def open_path(path: Path) -> bool:
    """Abre un archivo o una carpeta con la aplicación predeterminada del sistema."""
    opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    if not opened:
        log.warning("No se pudo abrir %s", path)
    return bool(opened)


def notify(
    parent: QWidget,
    title: str,
    text: str,
    actions: Sequence[tuple[str, Callable[[], None]]] = (),
    *,
    detail: str = "",
) -> QMessageBox:
    """Aviso no bloqueante con botones de acción opcionales (por ejemplo 'Abrir archivo')."""
    box = QMessageBox(QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.NoButton, parent)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if detail:
        box.setInformativeText(detail)
    callbacks: dict[QAbstractButton, Callable[[], None]] = {}
    for label, callback in actions:
        button = box.addButton(label, QMessageBox.ButtonRole.ActionRole)
        callbacks[button] = callback
    box.addButton("Cerrar", QMessageBox.ButtonRole.RejectRole)

    def on_click(button: QAbstractButton) -> None:
        callback = callbacks.get(button)
        if callback is not None:
            callback()

    box.buttonClicked.connect(on_click)
    box.open()
    return box


class DiscardDialog(QDialog):
    """Pide el motivo del descarte: se elige uno frecuente o se escribe otro."""

    def __init__(self, receipt_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Descartar boleta")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"{receipt_label} quedará descartada: no suma en los totales, pero sigue en la base "
            "y en la hoja Base del Excel. Se puede restaurar después."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(QLabel("Motivo del descarte"))
        self.reason_combo = QComboBox()
        self.reason_combo.setEditable(True)
        self.reason_combo.addItems(DISCARD_REASONS)
        self.reason_combo.setCurrentIndex(-1)
        self.reason_combo.lineEdit().setPlaceholderText("Elija o escriba el motivo")
        layout.addWidget(self.reason_combo)
        self.error_label = QLabel("", objectName="fieldError")
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox()
        self.accept_button = self.buttons.addButton("Descartar", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.reason_combo.currentTextChanged.connect(self._update)
        self._update()

    def reason(self) -> str:
        return " ".join(self.reason_combo.currentText().split())

    def _update(self) -> None:
        valid = len(self.reason()) >= MIN_REASON_LENGTH
        self.accept_button.setEnabled(valid)
        self.error_label.setText("" if valid or not self.reason() else "El motivo es demasiado corto.")

    def _on_accept(self) -> None:
        if len(self.reason()) >= MIN_REASON_LENGTH:
            self.accept()


class ImportReportDialog(QDialog):
    """Resultado de una importación: filas guardadas y filas rechazadas con su motivo."""

    def __init__(
        self, title: str, message: str, rejected: Sequence[RejectedRow], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(620, 380 if rejected else 160)
        layout = QVBoxLayout(self)
        summary = QLabel(message)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if rejected:
            note = QLabel("Las filas rechazadas no se guardaron. Corríjalas en la planilla y vuelva a importarla.")
            note.setObjectName("muted")
            note.setWordWrap(True)
            layout.addWidget(note)
            table = QTableWidget(len(rejected), 2)
            table.setHorizontalHeaderLabels(["Fila", "Motivo"])
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            for row_index, item in enumerate(rejected):
                number = QTableWidgetItem(str(item.row_number))
                number.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, 0, number)
                table.setItem(row_index, 1, QTableWidgetItem(item.reason))
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(table, 1)
        buttons = QDialogButtonBox()
        buttons.addButton("Cerrar", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def install_excepthook(*, show_dialog: bool = True) -> None:
    """Registra cualquier excepción no controlada y la informa sin cerrar la aplicación.

    En los modos desatendidos (autoprueba, capturas) no se abre el diálogo: nadie podría cerrarlo.
    """

    def handle(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Error no controlado", exc_info=(exc_type, exc, tb))
        if show_dialog and QApplication.instance() is not None:
            show_error(None, UNEXPECTED_ERROR, "Error inesperado")

    sys.excepthook = handle
