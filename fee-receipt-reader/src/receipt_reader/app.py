"""Arranque de la aplicación: argumentos, logging, base de datos y ventana."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from receipt_reader import APP_NAME, __version__
from receipt_reader.errors import UNEXPECTED_ERROR, AppError
from receipt_reader.logging_setup import configure_logging
from receipt_reader.paths import AppPaths, resolve_paths
from receipt_reader.services.app_services import AppServices
from receipt_reader.services.demo import ensure_database

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME, description="Lectura, validación y revisión de boletas de honorarios."
    )
    parser.add_argument("--datos", type=Path, help="carpeta de datos alternativa")
    parser.add_argument(
        "--reiniciar-datos", action="store_true", help="borra la base y la regenera con datos de ejemplo"
    )
    parser.add_argument("--autotest", action="store_true", help="abre la ventana, recorre las pestañas y sale")
    parser.add_argument(
        "--capturas",
        type=Path,
        help="guarda capturas de cada pestaña en la carpeta indicada y sale; sin --datos usa datos de ejemplo "
        "recién generados en una carpeta temporal",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.capturas is None or args.datos is not None:
        return run(args, resolve_paths(args.datos))
    # Las capturas de la documentación se toman siempre sobre datos de ejemplo nuevos, nunca sobre la
    # base de trabajo, que puede contener boletas reales. El log sigue en la carpeta de datos habitual.
    with tempfile.TemporaryDirectory(prefix="capturas_", ignore_cleanup_errors=True) as sandbox:
        return run(args, resolve_paths(Path(sandbox)), log_dir=resolve_paths().log_dir)


def run(args: argparse.Namespace, paths: AppPaths, *, log_dir: Path | None = None) -> int:
    """Abre la aplicación sobre `paths` según los argumentos ya interpretados."""
    configure_logging(log_dir or paths.log_dir)
    log.info("Iniciando %s %s (datos en %s)", APP_NAME, __version__, paths.data_dir)

    from PySide6.QtWidgets import QApplication

    from receipt_reader.ui.dialogs import install_excepthook, show_error, startup_notice
    from receipt_reader.ui.main_window import MainWindow
    from receipt_reader.ui.style import apply_style

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_style(app)
    unattended = bool(args.autotest or args.capturas)
    install_excepthook(show_dialog=not unattended)
    first_run = args.reiniciar_datos or not paths.db_path.exists()
    notice = startup_notice("Preparando los datos de ejemplo. Esto ocurre solo la primera vez.") if first_run else None
    try:
        created = ensure_database(paths, reset=args.reiniciar_datos)
        if created is not None:
            log.info("Datos de ejemplo listos en %s", paths.data_dir)
        window = MainWindow(AppServices.create(paths))
        window.set_unattended(unattended)
    except AppError as error:
        log.error("No se pudo iniciar: %s", error.user_message)
        if not unattended:
            show_error(None, error.user_message, "No se pudo iniciar la aplicación")
        return 1
    except Exception:
        # En modo desatendido no se abre ningún diálogo: el detalle queda en el log y se sale con error.
        log.exception("Error inesperado al iniciar")
        if not unattended:
            show_error(None, UNEXPECTED_ERROR, "No se pudo iniciar la aplicación")
        return 1
    finally:
        if notice is not None:
            notice.close()

    if args.capturas:
        from receipt_reader.ui.capture import capture_tabs

        capture_tabs(window, window.tabs, args.capturas, settle=window.wait_idle)
        return 0
    if args.autotest:
        window.show()
        app.processEvents()
        try:
            problems = window.autotest()
        except Exception:
            log.exception("Error inesperado durante la autoprueba")
            problems = ["La autoprueba terminó con un error inesperado (ver el log)."]
        for problem in problems:
            log.error("Autoprueba: %s", problem)
        log.info("Autoprueba %s", "fallida" if problems else "correcta")
        return 1 if problems else 0

    window.show()
    return app.exec()
