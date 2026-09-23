"""Clasificación de páginas: ¿la página es una boleta de honorarios?

Los paquetes PDF traen la boleta junto a páginas anexas (informes,
certificados). Una página se considera boleta cuando contiene al menos tres
de las etiquetas propias del documento y una de ellas es el encabezado
'BOLETA DE HONORARIOS' o la línea 'Total Honorarios'. Así, un anexo que
menciona la boleta de pasada no se confunde con ella. Las etiquetas se
comparan sin tildes y toleran las confusiones típicas del OCR en la palabra
'HONORARIOS'.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from receipt_reader.domain.layout import TextLine
from receipt_reader.domain.text import fold

# Etiqueta impresa que antecede a la glosa (sobre texto plegado: mayúsculas sin tildes).
SERVICE_PATTERN = r"POR\s+ATENCION\s+PROFESIONAL"
# 'HONORARIOS' tolera la S final leída por el OCR como '$' o '5' ('BOLETA DE HONORARIO$').
FEES_PATTERN = r"HONORARIO[S$5]"

MARKER_PATTERNS: dict[str, re.Pattern[str]] = {
    "header": re.compile(r"BOLETA\s+DE\s+" + FEES_PATTERN),
    "gross": re.compile(r"TOTAL\s+" + FEES_PATTERN),
    "retention": re.compile(r"RETENID[OA]"),
    "service": re.compile(SERVICE_PATTERN),
    "receiver": re.compile(r"SENOR\s*\(?\s*ES\s*\)?\s*:"),
}
REQUIRED_MARKERS = 3


def page_markers(lines: Sequence[TextLine]) -> frozenset[str]:
    """Etiquetas de boleta presentes en la página."""
    text = fold("\n".join(line.text for line in lines))
    return frozenset(name for name, pattern in MARKER_PATTERNS.items() if pattern.search(text))


def is_receipt_page(lines: Sequence[TextLine]) -> bool:
    markers = page_markers(lines)
    anchored = "header" in markers or "gross" in markers
    return anchored and len(markers) >= REQUIRED_MARKERS
