"""Pruebas de humo de la ventana principal y del arranque, sin pantalla (QT_QPA_PLATFORM=offscreen)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from staffing_simulator.services import Services

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def _application() -> Any:
    from staffing_simulator.ui.style import apply_style

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_style(app)
    return app


def test_main_window_autotest_passes_for_every_scenario(services: Services, tmp_path: Path) -> None:
    from staffing_simulator.ui.main_window import MainWindow

    app = _application()
    window = MainWindow(services, tmp_path)
    try:
        window.show()
        app.processEvents()
        assert window.tabs.count() == 8
        for row in range(window.scenario_list.count()):
            window.scenario_list.setCurrentRow(row)
            app.processEvents()
            assert window.autotest() == []
    finally:
        window.close()


def test_application_creates_database_and_passes_autotest(tmp_path: Path) -> None:
    from staffing_simulator.app import main

    _application()
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        assert main(["--datos", str(tmp_path), "--autotest"]) == 0
        assert (tmp_path / "staffing_simulator.db").exists()
        assert main(["--datos", str(tmp_path), "--autotest"]) == 0
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(level)
