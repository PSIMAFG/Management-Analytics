"""Arranque de la aplicación: argumentos, logging, base de datos y ventana."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from optibox import APP_NAME, __version__
from optibox.errors import AppError
from optibox.logging_setup import configure_logging
from optibox.paths import resolve_paths

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Asignación semanal de turnos de personal.")
    parser.add_argument("--datos", type=Path, help="carpeta de datos alternativa")
    parser.add_argument(
        "--reiniciar-datos", action="store_true", help="borra la base y la regenera con datos de ejemplo"
    )
    parser.add_argument("--autotest", action="store_true", help="abre la ventana, recorre las pestañas y sale")
    parser.add_argument("--capturas", type=Path, help="guarda capturas de cada pestaña en la carpeta indicada y sale")
    return parser.parse_args(argv)


def _use_system_fonts_offscreen() -> None:
    """Sin ventana visible (pruebas, capturas en servidores) Qt no encuentra las fuentes de Windows solo."""
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if (
        os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        and "QT_QPA_FONTDIR" not in os.environ
        and windows_fonts.is_dir()
    ):
        os.environ["QT_QPA_FONTDIR"] = str(windows_fonts)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = resolve_paths(args.datos)
    configure_logging(paths.log_dir)
    log.info("Iniciando %s %s (datos en %s)", APP_NAME, __version__, paths.data_dir)

    _use_system_fonts_offscreen()
    from PySide6.QtWidgets import QApplication

    from optibox.services.bootstrap import prepare_database
    from optibox.services.excel_service import ExcelService
    from optibox.services.master_data_service import MasterDataService
    from optibox.services.planning_service import PlanningService
    from optibox.ui.dialogs import install_excepthook, show_error
    from optibox.ui.main_window import MainWindow
    from optibox.ui.splash import StartupNotice
    from optibox.ui.style import apply_style

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_style(app)
    install_excepthook()
    notice = None
    if args.reiniciar_datos or not paths.db_path.exists():
        # La primera vez se crean los datos de ejemplo y una corrida de demostración: toma unos segundos.
        notice = StartupNotice("Preparando los datos de ejemplo y una corrida de demostración...")
    try:
        prepare_database(paths.db_path, args.reiniciar_datos)
        window = MainWindow(
            PlanningService(paths.db_path),
            MasterDataService(paths.db_path),
            ExcelService(paths.db_path),
            paths.export_dir,
        )
    except AppError as error:
        log.error("No se pudo iniciar: %s", error.user_message)
        if notice is not None:
            notice.close()
        show_error(None, error.user_message, "No se pudo iniciar la aplicación")
        return 1
    if notice is not None:
        notice.close()

    if args.capturas:
        from optibox.ui.capture import capture_tabs

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
