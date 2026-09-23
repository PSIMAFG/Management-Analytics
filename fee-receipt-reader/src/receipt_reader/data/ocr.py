"""Motores de OCR.

`RapidOcrEngine` usa RapidOCR con onnxruntime en CPU. Los modelos ONNX
vienen dentro del paquete `rapidocr`, por lo que funciona sin conexión (el
ejecutable los incluye con `--collect-data rapidocr`). El modelo por defecto
reconoce tildes y ñ; aun así, la comparación de etiquetas se hace sobre
texto sin tildes para tolerar errores de reconocimiento.

`SyntheticOcrEngine` devuelve cajas conocidas de antemano: se usa para poblar
la base de ejemplo sin correr el OCR (rápido y reproducible).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from receipt_reader.domain.layout import TextBox
from receipt_reader.errors import OcrUnavailableError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageImageSource:
    """Página que se puede rasterizar a pedido (el motor decide si la necesita)."""

    document: Path
    page_index: int
    render: Callable[[], Image.Image]


class OcrEngine(Protocol):
    def recognize(self, page: PageImageSource) -> list[TextBox]: ...


def boxes_from_polygons(polygons: Sequence[Any], texts: Sequence[str], scores: Sequence[float]) -> list[TextBox]:
    """Convierte los polígonos de 4 puntos del OCR en rectángulos alineados a los ejes."""
    boxes: list[TextBox] = []
    for polygon, text, score in zip(polygons, texts, scores, strict=False):
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
        boxes.append(TextBox(str(text), min(xs), min(ys), max(xs), max(ys), float(score)))
    return boxes


class RapidOcrEngine:
    """Adaptador de RapidOCR. El modelo se carga una sola vez, en el primer uso."""

    def __init__(self, text_score: float = 0.5) -> None:
        self._text_score = text_score
        self._engine: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._engine is None:
                try:
                    from rapidocr import RapidOCR
                except ImportError as error:  # pragma: no cover - depende de la instalación
                    raise OcrUnavailableError(
                        "No se encontró el motor de OCR (RapidOCR). Reinstale la aplicación."
                    ) from error
                logging.getLogger("RapidOCR").setLevel(logging.WARNING)
                self._engine = RapidOCR(params={"Global.log_level": "warning", "Global.text_score": self._text_score})
                log.info("Motor de OCR cargado")
        return self._engine

    def recognize(self, page: PageImageSource) -> list[TextBox]:
        engine = self._load()
        image = page.render().convert("RGB")
        result = engine(image)
        if result is None or getattr(result, "boxes", None) is None:
            return []
        return boxes_from_polygons(result.boxes, result.txts, result.scores)


class SyntheticOcrEngine:
    """Devuelve las cajas registradas para cada (documento, página); sin rasterizar nada."""

    def __init__(self, pages: Mapping[tuple[str, int], list[TextBox]]) -> None:
        self._pages = {(str(Path(doc).resolve()).casefold(), index): boxes for (doc, index), boxes in pages.items()}

    def recognize(self, page: PageImageSource) -> list[TextBox]:
        key = (str(page.document.resolve()).casefold(), page.page_index)
        return list(self._pages.get(key, []))
