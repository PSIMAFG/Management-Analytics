"""Reconstrucción de líneas de texto a partir de cajas con coordenadas.

Tanto el texto nativo de un PDF (segmentos con su rectángulo) como el OCR
(cajas de texto con su confianza) llegan como cajas sueltas. Las cajas se
agrupan en líneas por solapamiento vertical y dentro de cada línea se
ordenan de izquierda a derecha. Nunca se colapsa el documento en una sola
línea: los extractores trabajan línea por línea.

Las coordenadas usan el origen arriba a la izquierda (y crece hacia abajo).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from receipt_reader.domain.text import collapse_spaces, normalize_text

# Proporción mínima de solapamiento vertical (respecto de la caja más baja) para unir dos cajas.
MIN_VERTICAL_OVERLAP = 0.5
# Separación horizontal (en alturas de línea) bajo la cual dos cajas se unen sin espacio.
TIGHT_GAP_RATIO = 0.2


@dataclass(frozen=True)
class TextBox:
    """Fragmento de texto con su rectángulo y la confianza del reconocimiento (1,0 si es nativo)."""

    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float = 1.0

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.0)

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class TextLine:
    """Línea reconstruida: texto normalizado, confianza media y rectángulo que la contiene."""

    text: str
    confidence: float
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0


def _same_line(top: float, bottom: float, box: TextBox) -> bool:
    line_height = bottom - top
    smaller = min(line_height, box.height)
    if smaller <= 0:
        # Cajas degeneradas (un guion suelto, por ejemplo): basta con que el centro caiga dentro.
        return top <= box.center_y <= bottom or (box.top <= (top + bottom) / 2 <= box.bottom)
    overlap = min(bottom, box.bottom) - max(top, box.top)
    return overlap >= MIN_VERTICAL_OVERLAP * smaller


def _join(boxes: Sequence[TextBox]) -> str:
    ordered = sorted(boxes, key=lambda b: b.left)
    line_height = max((b.height for b in ordered), default=0.0)
    parts: list[str] = []
    previous: TextBox | None = None
    for box in ordered:
        if previous is not None:
            gap = box.left - previous.right
            tight = gap < TIGHT_GAP_RATIO * line_height
            if not tight:
                parts.append(" ")
        parts.append(box.text)
        previous = box
    return collapse_spaces(normalize_text("".join(parts)))


def _confidence(boxes: Sequence[TextBox]) -> float:
    weights = [max(len(b.text.strip()), 1) for b in boxes]
    return sum(b.confidence * w for b, w in zip(boxes, weights, strict=True)) / sum(weights)


def build_lines(boxes: Iterable[TextBox]) -> list[TextLine]:
    """Agrupa las cajas en líneas (de arriba abajo) y une cada línea de izquierda a derecha."""
    pending = sorted((b for b in boxes if b.text.strip()), key=lambda b: (b.center_y, b.left))
    groups: list[list[TextBox]] = []
    spans: list[tuple[float, float]] = []
    for box in pending:
        if groups and _same_line(*spans[-1], box):
            groups[-1].append(box)
            top, bottom = spans[-1]
            spans[-1] = (min(top, box.top), max(bottom, box.bottom))
        else:
            groups.append([box])
            spans.append((box.top, box.bottom))
    lines: list[TextLine] = []
    for group in groups:
        text = _join(group)
        if not text:
            continue
        lines.append(
            TextLine(
                text=text,
                confidence=_confidence(group),
                left=min(b.left for b in group),
                top=min(b.top for b in group),
                right=max(b.right for b in group),
                bottom=max(b.bottom for b in group),
            )
        )
    return lines


def lines_from_text(text: str, confidence: float = 1.0) -> list[TextLine]:
    """Líneas a partir de texto plano (una por salto de línea, sin líneas vacías)."""
    lines: list[TextLine] = []
    for index, raw in enumerate(normalize_text(text).splitlines()):
        cleaned = collapse_spaces(raw)
        if cleaned:
            lines.append(TextLine(cleaned, confidence, 0.0, float(index), 0.0, float(index) + 1))
    return lines


def mean_confidence(lines: Sequence[TextLine]) -> float | None:
    """Confianza media de una página ponderada por el largo de cada línea."""
    if not lines:
        return None
    weights = [len(line.text) for line in lines]
    return sum(line.confidence * w for line, w in zip(lines, weights, strict=True)) / sum(weights)
