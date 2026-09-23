"""Mensajes al usuario y captura global de errores no controlados."""

from __future__ import annotations

import logging
import sys
from types import TracebackType

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

log = logging.getLogger(__name__)

_translators: list[QTranslator] = []


def install_translations(app: QApplication) -> None:
    """Carga la traducción al español de los textos propios de Qt (botones estándar, calendarios)."""
    if _translators:
        return
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.Language.Spanish), "qtbase", "_", folder):
        app.installTranslator(translator)
        _translators.append(translator)
    else:
        log.info("No se encontró la traducción de Qt al español en %s", folder)


def _run(box: QMessageBox) -> None:
    box.exec()


def run_dialog(dialog: QDialog) -> bool:
    """Muestra un diálogo modal y devuelve True si el usuario lo aceptó."""
    return dialog.exec() == QDialog.DialogCode.Accepted


def _message_box(
    parent: QWidget | None, icon: QMessageBox.Icon, title: str, message: str, detail: str = ""
) -> QMessageBox:
    box = QMessageBox(icon, title, message, QMessageBox.StandardButton.NoButton, parent)
    if detail:
        box.setInformativeText(detail)
    return box


def show_error(parent: QWidget | None, message: str, title: str = "No se pudo completar la acción") -> None:
    box = _message_box(parent, QMessageBox.Icon.Warning, title, message)
    box.addButton("Aceptar", QMessageBox.ButtonRole.AcceptRole)
    _run(box)


def show_info(parent: QWidget | None, message: str, title: str = "Información", detail: str = "") -> None:
    box = _message_box(parent, QMessageBox.Icon.Information, title, message, detail)
    box.addButton("Aceptar", QMessageBox.ButtonRole.AcceptRole)
    _run(box)


def ask_confirmation(
    parent: QWidget | None,
    message: str,
    title: str = "Confirmar",
    confirm_text: str = "Sí",
    cancel_text: str = "Cancelar",
    detail: str = "",
) -> bool:
    box = _message_box(parent, QMessageBox.Icon.Question, title, message, detail)
    confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    _run(box)
    return box.clickedButton() is confirm


APPLY, DISCARD, CANCEL = "apply", "discard", "cancel"


def ask_apply_changes(parent: QWidget | None, message: str, title: str = "Cambios sin aplicar") -> str:
    """Pregunta qué hacer con cambios pendientes: APPLY, DISCARD o CANCEL (Cancelar también al cerrar)."""
    box = _message_box(parent, QMessageBox.Icon.Question, title, message)
    apply = box.addButton("Aplicar", QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton("Descartar", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(apply)
    box.setEscapeButton(cancel)
    _run(box)
    clicked = box.clickedButton()
    if clicked is apply:
        return APPLY
    if clicked is discard:
        return DISCARD
    return CANCEL


def ask_open_or_close(parent: QWidget | None, message: str, title: str, open_text: str) -> bool:
    """Informa un resultado y ofrece abrirlo; devuelve True si el usuario eligió abrir."""
    box = _message_box(parent, QMessageBox.Icon.Information, title, message)
    open_button = box.addButton(open_text, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cerrar", QMessageBox.ButtonRole.RejectRole)
    _run(box)
    return box.clickedButton() is open_button


def install_excepthook() -> None:
    """Registra cualquier excepción no controlada y la informa sin cerrar la aplicación."""

    def handle(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Error no controlado", exc_info=(exc_type, exc, tb))
        if QApplication.instance() is not None:
            box = QMessageBox(
                QMessageBox.Icon.Critical,
                "Error inesperado",
                "Ocurrió un error inesperado. El detalle quedó registrado en el log de la aplicación.",
            )
            box.addButton("Aceptar", QMessageBox.ButtonRole.AcceptRole)
            _run(box)

    sys.excepthook = handle
