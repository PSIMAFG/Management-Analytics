"""Arranque de la aplicación: argumentos, logging, base de datos y ventana."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from kpi_monitor import APP_NAME, APP_TITLE, __version__
from kpi_monitor.data import db
from kpi_monitor.data.seed import seed_demo_data
from kpi_monitor.errors import AppError, DataError
from kpi_monitor.logging_setup import configure_logging
from kpi_monitor.paths import AppPaths, resolve_paths

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, description=APP_TITLE)
    parser.add_argument("--datos", type=Path, help="carpeta de datos alternativa")
    parser.add_argument(
        "--reiniciar-datos", action="store_true", help="borra la base y la regenera con datos de ejemplo"
    )
    parser.add_argument("--autotest", action="store_true", help="abre la ventana, recorre las pestañas y sale")
    parser.add_argument("--capturas", type=Path, help="guarda capturas de cada pestaña en la carpeta indicada y sale")
    return parser.parse_args(argv)


def prepare_database(paths: AppPaths, reset: bool) -> None:
    """Crea la base en la primera ejecución y la puebla con datos sintéticos."""
    if reset and paths.db_path.exists():
        try:
            paths.db_path.unlink()
        except OSError as error:
            raise DataError(
                "No se pudo borrar la base de datos para regenerarla. Cierre las otras ventanas de la aplicación "
                "y vuelva a intentarlo."
            ) from error
        log.info("Base de datos eliminada para regenerarla")
    try:
        conn = db.connect(paths.db_path)
    except sqlite3.Error as error:
        raise DataError(f"No se pudo abrir la base de datos en {paths.db_path}.") from error
    try:
        if db.initialize(conn):
            seed_demo_data(conn)
            log.info("Base de datos creada y poblada con datos de ejemplo en %s", paths.db_path)
    except sqlite3.Error as error:
        raise DataError("No se pudo crear la base de datos de ejemplo.") from error
    finally:
        conn.close()


def use_system_fonts_offscreen() -> None:
    """Con la plataforma sin ventanas de Qt en Windows, apunta a las fuentes del sistema.

    Sin esto, las capturas y pruebas sin ventana muestran cuadros en lugar de letras.
    """
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen" or os.name != "nt" or os.environ.get("QT_QPA_FONTDIR"):
        return
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if fonts.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(fonts)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = resolve_paths(args.datos)
    configure_logging(paths.log_dir)
    log.info("Iniciando %s %s (datos en %s)", APP_NAME, __version__, paths.data_dir)

    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QApplication

    from kpi_monitor.services import build_services
    from kpi_monitor.ui.dialogs import install_excepthook, show_error
    from kpi_monitor.ui.main_window import MainWindow
    from kpi_monitor.ui.style import apply_style

    use_system_fonts_offscreen()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    QLocale.setDefault(QLocale(QLocale.Language.Spanish, QLocale.Country.Chile))
    apply_style(app)
    install_excepthook()
    try:
        prepare_database(paths, args.reiniciar_datos)
        window = MainWindow(build_services(paths.db_path), paths.export_dir)
    except AppError as error:
        log.error("No se pudo iniciar: %s", error.user_message)
        show_error(None, error.user_message, "No se pudo iniciar la aplicación")
        return 1

    if args.capturas:
        from kpi_monitor.ui.capture import capture_tabs

        if not window.wait_until_idle():
            log.error("El cálculo inicial no terminó; no se generaron capturas")
            return 1
        capture_tabs(window, window.tabs, args.capturas)
        return 0
    if args.autotest:
        window.show()
        app.processEvents()
        problems = window.autotest()
        for problem in problems:
            log.error("Autoprueba: %s", problem)
        log.info("Autoprueba %s", "fallida" if problems else "correcta")
        return 1 if problems else 0

    window.show()
    return app.exec()
