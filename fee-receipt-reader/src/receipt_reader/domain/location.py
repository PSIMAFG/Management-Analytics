"""Pistas que entrega la ubicación del archivo dentro de la carpeta de entrada.

Convención opcional: `<raíz>/<AAAA-MM>/<NNN Nombre del programa>/<archivo>`.
La carpeta del mes indica el período de pago y el código de 3 dígitos, el
programa. En el nombre del archivo, 'B <n>' sugiere el folio y 'MES <mes>'
el mes de servicio. Si la ruta no sigue la convención, las pistas quedan
vacías y el archivo se procesa igual.

El folio y el nombre que salen del nombre del archivo nunca reemplazan a lo
leído en el documento: solo se ofrecen como sugerencia o como respaldo
marcado con su origen.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from receipt_reader.domain.dates import Period
from receipt_reader.domain.text import MONTH_PATTERN, fold, month_from_name
from receipt_reader.errors import FieldFormatError

_PERIOD_DIR = re.compile(r"(\d{4})-(\d{1,2})(?:\b.*)?")
_PROGRAM_DIR = re.compile(r"(\d{3})(?:\s+.*)?")
_FOLIO_HINT = re.compile(r"(?<![A-Z0-9])B\s*(\d{1,10})(?!\d)")
_MONTH_HINT = re.compile(r"\bMES\s+(?:DE\s+)?(" + MONTH_PATTERN + r")\b")
_NAME_TOKEN = re.compile(r"[A-ZÁÉÍÓÚÜÑa-záéíóúüñ]{2,}")


@dataclass(frozen=True)
class LocationHints:
    """Datos deducidos de la ruta relativa del archivo."""

    payment_period: Period | None = None
    program_code: str | None = None
    folio_hint: int | None = None
    service_month_hint: int | None = None
    name_hint: str | None = None


def _name_from_stem(stem: str) -> str | None:
    folded = fold(stem)
    cut = len(stem)
    for pattern in (_FOLIO_HINT, _MONTH_HINT):
        match = pattern.search(folded)
        if match is not None:
            cut = min(cut, match.start())
    tokens = [
        token
        for token in _NAME_TOKEN.findall(stem[:cut])
        if month_from_name(token) is None and fold(token) not in {"MES", "BOLETA", "BHE"}
    ]
    if not 2 <= len(tokens) <= 5:
        return None
    return " ".join(token.capitalize() for token in tokens)


def parse_location(relative_parts: Sequence[str]) -> LocationHints:
    """Interpreta las partes de la ruta relativa (carpetas y nombre del archivo)."""
    if not relative_parts:
        return LocationHints()
    *folders, filename = relative_parts
    payment_period: Period | None = None
    program_code: str | None = None
    for folder in folders:
        text = folder.strip()
        if match := _PERIOD_DIR.fullmatch(text):
            try:
                payment_period = Period(int(match.group(1)), int(match.group(2)))
            except FieldFormatError:
                payment_period = None
        elif match := _PROGRAM_DIR.fullmatch(text):
            program_code = match.group(1)
    stem = filename.rsplit(".", 1)[0]
    folded = fold(stem)
    folio_match = _FOLIO_HINT.search(folded)
    month_match = _MONTH_HINT.search(folded)
    return LocationHints(
        payment_period=payment_period,
        program_code=program_code,
        folio_hint=int(folio_match.group(1)) if folio_match and int(folio_match.group(1)) > 0 else None,
        service_month_hint=month_from_name(month_match.group(1)) if month_match else None,
        name_hint=_name_from_stem(stem),
    )
