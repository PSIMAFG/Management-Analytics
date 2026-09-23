"""Mensajes al usuario, avance de tareas largas y captura global de errores no controlados."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QProgressBar, QProgressDialog, QWidget

log = logging.getLogger(__name__)

# Las tareas que terminan antes de este tiempo no llegan a mostrar la ventana de avance.
PROGRESS_DELAY_MS = 400


def show_error(parent: QWidget | None, message: str, title: str = "No se pudo completar la acción") -> None:
    QMessageBox.warning(parent, title, message)


def show_info(parent: QWidget | None, message: str, title: str = "Información") -> None:
    QMessageBox.information(parent, title, message)


def ask_confirmation(parent: QWidget | None, message: str, title: str = "Confirmar") -> bool:
    answer = QMessageBox.question(parent, title, message)
    return answer == QMessageBox.StandardButton.Yes


def run_modal(dialog: QDialog) -> bool:
    """Abre un diálogo modal y devuelve verdadero si el usuario lo aceptó."""
    return dialog.exec() == QDialog.DialogCode.Accepted


def show_saved_file(parent: QWidget | None, path: Path, title: str) -> None:
    """Informa dónde quedó un archivo y ofrece abrirlo con la aplicación predeterminada."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(f"El archivo se guardó en:\n{path}")
    open_button = box.addButton("Abrir archivo", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cerrar", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is open_button and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        show_error(parent, "No se encontró una aplicación para abrir el archivo.")


class TaskProgressDialog(QProgressDialog):
    """Ventana de avance de una tarea en segundo plano, con botón para cancelarla.

    Solo aparece si la tarea demora más de `PROGRESS_DELAY_MS`, para no parpadear en
    operaciones rápidas.
    """

    def __init__(
        self, parent: QWidget | None, title: str, label: str, on_cancel: Callable[[], None] | None = None
    ) -> None:
        super().__init__(label, "Cancelar", 0, 100, parent)
        bar = QProgressBar(self)
        bar.setFormat("%p %")  # porcentaje con espacio, como el resto de la interfaz
        self.setBar(bar)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumDuration(PROGRESS_DELAY_MS)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setMinimumWidth(380)
        self.setValue(0)
        if on_cancel is None:
            self.setCancelButton(None)
        else:
            self.canceled.connect(on_cancel)

    def update_progress(self, percent: int, message: str) -> None:
        self.setLabelText(message)
        self.setValue(max(0, min(99, percent)))

    def finish(self) -> None:
        self.blockSignals(True)
        self.reset()
        self.close()
        self.deleteLater()


def install_excepthook() -> None:
    """Registra cualquier excepción no controlada y la informa sin cerrar la aplicación."""

    def handle(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Error no controlado", exc_info=(exc_type, exc, tb))
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None,
                "Error inesperado",
                "Ocurrió un error inesperado. El detalle quedó registrado en el log de la aplicación.",
            )

    sys.excepthook = handle
