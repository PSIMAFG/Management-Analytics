"""Configuración del registro de eventos (archivo rotativo y consola)."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Manejadores creados por una configuración anterior en el mismo proceso.
_installed: list[logging.Handler] = []


def configure_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Envía los eventos a un archivo rotativo y, si hay consola, también a ella.

    En el ejecutable sin consola `sys.stderr` es None, por eso se verifica
    antes de agregar el manejador de consola. Si se llama más de una vez en
    el mismo proceso, cierra los manejadores que instaló antes para no dejar
    el archivo de log abierto.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "aplicacion.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    while _installed:
        _installed.pop().close()
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)
    _installed.append(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(console)
        _installed.append(console)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    return log_file
