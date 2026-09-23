"""Capturas de pantalla reproducibles para la documentación."""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget

from kpi_monitor.ui.widgets import ChartCanvas

log = logging.getLogger(__name__)

CAPTURE_SIZE = (1600, 1000)


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def _settle() -> None:
    for _ in range(5):
        QApplication.processEvents()


def capture_tabs(
    window: QMainWindow, tabs: QTabWidget, out_dir: Path, size: tuple[int, int] = CAPTURE_SIZE, prefix: str = ""
) -> list[Path]:
    """Guarda una imagen de la ventana completa por cada pestaña, con los gráficos ya dibujados."""
    out_dir.mkdir(parents=True, exist_ok=True)
    window.resize(*size)
    window.show()
    _settle()
    saved: list[Path] = []
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        _settle()
        for chart in tabs.currentWidget().findChildren(ChartCanvas):
            chart.draw_now()
        _settle()
        path = out_dir / f"{prefix}{index + 1:02d}_{_slug(tabs.tabText(index))}.png"
        window.grab().save(str(path))
        saved.append(path)
        log.info("Captura guardada: %s", path)
    return saved
