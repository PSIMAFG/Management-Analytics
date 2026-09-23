"""Formato de números, montos y fechas según la convención chilena.

`format_int` y `plural` viven en el dominio (también los usan los mensajes de
los servicios) y se reexportan aquí para la interfaz.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from receipt_reader.domain.text import format_int as format_int
from receipt_reader.domain.text import plural as plural

MONTHS_SHORT = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def format_decimal(value: float | Decimal, decimals: int = 1) -> str:
    """1234.56 -> '1.234,6'."""
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    text = f"{rounded:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def format_clp(value: float | int | Decimal) -> str:
    """Monto en pesos chilenos sin decimales: '$ 1.234.567'."""
    sign = "-" if value < 0 else ""
    return f"{sign}$ {format_int(abs(value))}"


def format_pct(ratio: float | None, decimals: int = 1) -> str:
    """0.853 -> '85,3 %'. None se muestra como guion."""
    if ratio is None:
        return "-"
    return f"{format_decimal(ratio * 100, decimals)} %"


def format_date(value: date | None) -> str:
    """date(2026, 3, 5) -> '05-03-2026'."""
    return value.strftime("%d-%m-%Y") if value else "-"


def format_datetime(value: datetime | None) -> str:
    """datetime(2026, 3, 5, 9, 7) -> '05-03-2026 09:07'."""
    return value.strftime("%d-%m-%Y %H:%M") if value else "-"


def month_label(year: int, month: int) -> str:
    """(2026, 3) -> 'mar 2026'."""
    return f"{MONTHS_SHORT[month - 1]} {year}"


def format_millions(value: float) -> str:
    """Pesos expresados en millones con una coma decimal: 12_430_000 -> '12,4'; 5_000_000 -> '5'."""
    millions = Decimal(str(value)) / Decimal(1_000_000)
    rounded = millions.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        return format_int(rounded)
    return format_decimal(rounded, 1)


def format_quantity(value: Decimal | float | int | None) -> str:
    """Cantidad sin ceros de relleno y con coma decimal: Decimal('22.50') -> '22,5'."""
    if value is None:
        return "-"
    number = Decimal(str(value)).normalize()
    if number == number.to_integral_value():
        return format_int(number)
    exponent = number.as_tuple().exponent
    decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 1
    return format_decimal(number, decimals)


def keep_amounts_together(text: str) -> str:
    """Evita que un salto de línea separe el signo peso de su monto en textos largos."""
    return text.replace("$ ", "$\u00a0")


def fold_search(text: str) -> str:
    """Texto para búsquedas sin distinguir mayúsculas, tildes ni puntos de RUT o montos."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return plain.casefold().replace(".", "")
