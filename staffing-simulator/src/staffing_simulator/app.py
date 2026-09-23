"""Arranque de la aplicación: argumentos, logging, base de datos y ventana."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from staffing_simulator import APP_NAME, __version__
from staffing_simulator.data import db
from staffing_simulator.data.seed import seed_demo_data
from staffing_simulator.errors import AppError, DataError
from staffing_simulator.logging_setup import configure_logging
from staffing_simulator.paths import AppPaths, resolve_paths

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Simulador de costos de dotación.")
    parser.add_argument("--datos", type=Path, help="carpeta de datos alternativa")
    parser.add_argument(
        "--reiniciar-datos", action="store_true", help="borra la base y la regenera con datos de ejemplo"
    )
    parser.add_argument("--autotest", action="store_true", help="abre la ventana, recorre las pestañas y sale")
    parser.add_argument("--capturas", type=Path, help="guarda capturas de cada pestaña en la carpeta indicada y sale")
    return parser.parse_args(argv)


def prepare_database(paths: AppPaths, reset: bool) -> None:
    """Crea la base en la primera ejecución y la puebla con datos sintéticos.

    Los errores de SQLite o del sistema de archivos se informan como DataError
    con un mensaje que indica cómo recuperarse.
    """
    path = paths.db_path
    if reset and path.exists():
        try:
            path.unlink()
        except OSError as error:
            log.exception("No se pudo borrar la base de datos %s", path)
            raise DataError(
                f"No se pudo borrar la base de datos en {path}: está en uso. "
                "Cierre las otras ventanas del simulador e intente nuevamente."
            ) from error
        log.info("Base de datos eliminada para regenerarla")
    try:
        conn = db.connect(path)
        try:
            if db.initialize(conn):
                seed_demo_data(conn)
                log.info("Base de datos creada y poblada con datos de ejemplo en %s", path)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as error:
        log.exception("No se pudo abrir la base de datos %s", path)
        raise DataError(
            f"No se pudo abrir la base de datos en {path}: está dañada o en uso. Cierre las otras ventanas del "
            "simulador o inicie con --reiniciar-datos para regenerarla (se perderán los cambios)."
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = resolve_paths(args.datos)
    configure_logging(paths.log_dir)
    log.info("Iniciando %s %s (datos en %s)", APP_NAME, __version__, paths.data_dir)

    from PySide6.QtWidgets import QApplication

    from staffing_simulator.services import Services
    from staffing_simulator.ui.dialogs import install_excepthook, install_translations, show_error
    from staffing_simulator.ui.main_window import MainWindow
    from staffing_simulator.ui.style import apply_style
    from staffing_simulator.ui.workers import install_main_thread_gc

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_style(app)
    install_translations(app)
    install_excepthook()
    install_main_thread_gc(app)
    try:
        prepare_database(paths, args.reiniciar_datos)
        window = MainWindow(Services.create(paths.db_path), paths.export_dir)
    except AppError as error:
        log.error("No se pudo iniciar: %s", error.user_message)
        show_error(None, error.user_message, "No se pudo iniciar la aplicación")
        return 1

    if args.capturas:
        window.capture_screens(args.capturas)
        window.dispose()
        return 0
    if args.autotest:
        window.show()
        app.processEvents()
        problems = window.autotest()
        for problem in problems:
            log.error("Autoprueba: %s", problem)
        log.info("Autoprueba %s", "fallida" if problems else "correcta")
        window.dispose()
        return 1 if problems else 0

    window.show()
    return app.exec()
