"""Normalización de texto previa a cualquier extracción.

Las boletas electrónicas imprimen el guion del RUT como signo menos Unicode
y el OCR devuelve guiones de distintos tipos; todo se lleva a '-' antes de
buscar patrones. La comparación de etiquetas se hace sobre una versión
"plegada" del texto (mayúsculas sin tildes) que conserva el largo, de modo
que las posiciones encontradas sirven también sobre el texto original.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

# Guiones Unicode que se normalizan a '-' (U+2010 a U+2015, signo menos y variantes).
UNICODE_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"
_DASH_TABLE = str.maketrans(dict.fromkeys(UNICODE_DASHES, "-") | {"\u00a0": " ", "\u202f": " ", "\t": " "})
_SPACES = re.compile(r" {2,}")

MONTHS_BY_NAME = {
    "ENERO": 1,
    "FEBRERO": 2,
    "MARZO": 3,
    "ABRIL": 4,
    "MAYO": 5,
    "JUNIO": 6,
    "JULIO": 7,
    "AGOSTO": 8,
    "SEPTIEMBRE": 9,
    "SETIEMBRE": 9,
    "OCTUBRE": 10,
    "NOVIEMBRE": 11,
    "DICIEMBRE": 12,
}
MONTH_NAMES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
MONTH_PATTERN = "|".join(sorted(MONTHS_BY_NAME, key=len, reverse=True))


def normalize_text(text: str) -> str:
    """Forma NFC, guiones Unicode a '-', espacios especiales a espacio simple."""
    return unicodedata.normalize("NFC", text).translate(_DASH_TABLE)


def collapse_spaces(text: str) -> str:
    """Reduce los espacios repetidos a uno y recorta los extremos."""
    return _SPACES.sub(" ", text).strip()


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    base = unicodedata.normalize("NFD", char)[0]
    upper = base.upper()
    return upper if len(upper) == 1 else base


def fold(text: str) -> str:
    """Mayúsculas sin tildes ni diéresis, con el mismo largo que el texto recibido.

    'Señor(es): atención' se convierte en 'SENOR(ES): ATENCION'. Se asume texto
    en forma NFC (ver `normalize_text`).
    """
    return "".join(_fold_char(char) for char in text)


def month_from_name(name: str) -> int | None:
    """'octubre', 'OCTUBRE' o 'Setiembre' -> número de mes; None si no es un mes."""
    return MONTHS_BY_NAME.get(fold(name.strip()))


def format_thousands(value: int) -> str:
    """1234567 -> '1.234.567' (para mensajes del dominio)."""
    sign = "-" if value < 0 else ""
    return sign + f"{abs(value):,}".replace(",", ".")


def format_int(value: float | Decimal) -> str:
    """Número redondeado al entero con punto de miles: 12345678.4 -> '12.345.678'."""
    rounded = int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return format_thousands(rounded)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """Cantidad con el sustantivo que concuerda: '1 boleta', '1.200 boletas'.

    `plural_form` sirve para frases que no forman el plural con una 's' final
    ('1 archivo ya estaba registrado', '3 archivos ya estaban registrados').
    """
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{format_int(count)} {word}"


def format_percent_bp(rate_bp: int) -> str:
    """1450 -> '14,50 %'."""
    return f"{rate_bp // 100},{rate_bp % 100:02d} %"
