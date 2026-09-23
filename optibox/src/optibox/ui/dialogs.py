"""Mensajes al usuario y captura global de errores no controlados."""

from __future__ import annotations

import logging
import sys
from types import TracebackType

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

log = logging.getLogger(__name__)


def show_error(parent: QWidget | None, message: str, title: str = "No se pudo completar la acción") -> None:
    log.info("Aviso al usuario: %s", message)
    QMessageBox.warning(parent, title, message)


def show_info(parent: QWidget | None, message: str, title: str = "Información") -> None:
    QMessageBox.information(parent, title, message)


def ask_confirmation(parent: QWidget | None, message: str, title: str = "Confirmar") -> bool:
    answer = QMessageBox.question(parent, title, message)
    return answer == QMessageBox.StandardButton.Yes


def run_dialog(dialog: QDialog) -> bool:
    """Muestra un diálogo modal y devuelve True si el usuario lo aceptó."""
    return dialog.exec() == QDialog.DialogCode.Accepted


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
