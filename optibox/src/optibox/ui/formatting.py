"""Formato de números, montos y fechas según la convención chilena."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

MONTHS = (
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
MONTHS_SHORT = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def format_int(value: float | int | Decimal) -> str:
    """12345678 -> '12.345.678'."""
    rounded = int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{rounded:,}".replace(",", ".")


def format_count(value: int, singular: str, plural: str) -> str:
    """Cantidad con el sustantivo en el número que corresponde: (1, 'fila', 'filas') -> '1 fila'."""
    return f"{format_int(value)} {singular if value == 1 else plural}"


def format_decimal(value: float | Decimal, decimals: int = 1) -> str:
    """1234.56 -> '1.234,6'."""
    text = f"{float(value):,.{decimals}f}"
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


def month_label(year: int, month: int) -> str:
    """(2026, 3) -> 'mar 2026'."""
    return f"{MONTHS_SHORT[month - 1]} {year}"


def format_hours(minutes: int | float, decimals: int = 1) -> str:
    """Minutos como horas con coma decimal: 450 -> '7,5 h'."""
    return f"{format_decimal(minutes / 60, decimals)} h"


def format_datetime(value: datetime | None) -> str:
    """datetime(2026, 3, 5, 14, 7) -> '05-03-2026 14:07'."""
    return value.strftime("%d-%m-%Y %H:%M") if value else "-"


def format_seconds(seconds: float | None) -> str:
    """12.345 -> '12,3 s'."""
    return "-" if seconds is None else f"{format_decimal(seconds, 1)} s"
