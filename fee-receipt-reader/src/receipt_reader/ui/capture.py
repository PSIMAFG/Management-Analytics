"""Capturas de pantalla reproducibles para la documentación."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget

log = logging.getLogger(__name__)

CAPTURE_SIZE = (1600, 1000)


def _slug(text: str) -> str:
    text = re.sub(r"\s*\(\d+\)", "", text)  # sin contadores como 'Revisión (8)'
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def capture_tabs(
    window: QMainWindow,
    tabs: QTabWidget,
    out_dir: Path,
    *,
    size: tuple[int, int] = CAPTURE_SIZE,
    settle: Callable[[], None] | None = None,
) -> list[Path]:
    """Guarda una imagen de la ventana completa por cada pestaña.

    `settle` espera las tareas en segundo plano (por ejemplo, la vista previa) antes de cada captura.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    window.resize(*size)
    window.show()
    saved: list[Path] = []
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        for _ in range(5):
            QApplication.processEvents()
        if settle is not None:
            settle()
        for _ in range(5):
            QApplication.processEvents()
        path = out_dir / f"{index + 1:02d}_{_slug(tabs.tabText(index))}.png"
        window.grab().save(str(path))
        saved.append(path)
        log.info("Captura guardada: %s", path)
    return saved
