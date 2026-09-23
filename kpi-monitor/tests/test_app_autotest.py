"""Arranque completo sin ventana visible: crea la base sintética, abre la ventana y recorre las pestañas."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import matplotlib as mpl
import pytest

from kpi_monitor.app import main, prepare_database
from kpi_monitor.errors import DataError
from kpi_monitor.paths import resolve_paths


@pytest.fixture
def isolated_app_state() -> Iterator[None]:
    """Restaura el logging, el manejador de excepciones y el estilo de matplotlib que cambia la aplicación."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    hook = sys.excepthook
    with mpl.rc_context():
        yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    sys.excepthook = hook


@pytest.mark.usefixtures("isolated_app_state")
def test_autotest_creates_the_database_and_passes(tmp_path: Path) -> None:
    """El arranque con --autotest crea la base sintética y el log en la carpeta de datos, recorre la ventana y
    termina con código 0."""
    assert main(["--autotest", "--datos", str(tmp_path)]) == 0
    assert (tmp_path / "kpi_monitor.db").exists()
    assert (tmp_path / "logs" / "aplicacion.log").exists()


def test_reset_with_a_locked_database_file_raises_a_readable_error(tmp_path: Path) -> None:
    """Regla: si la base no se puede borrar para reiniciarla, se informa un error legible, no una traza cruda."""
    paths = resolve_paths(tmp_path)
    prepare_database(paths, reset=False)
    with paths.db_path.open("r+b"), pytest.raises(DataError, match="Cierre las otras ventanas"):
        prepare_database(paths, reset=True)
