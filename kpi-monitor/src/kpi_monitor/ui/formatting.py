"""Formato de números, montos y fechas según la convención chilena.

Los números se formatean con las mismas funciones del dominio para que la
interfaz, las alertas y los reportes muestren exactamente las mismas cifras.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from kpi_monitor.domain.text import (
    MONTHS,
    MONTHS_SHORT,
    format_decimal,
    format_int,
    format_pct,
    format_value,
    join_months,
    month_name,
    period_label,
    plural,
)

__all__ = [
    "MONTHS",
    "MONTHS_SHORT",
    "WEEKDAYS",
    "format_clp",
    "format_date",
    "format_decimal",
    "format_int",
    "format_pct",
    "format_value",
    "join_months",
    "month_label",
    "month_name",
    "period_label",
    "plural",
]

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def format_clp(value: float | int | Decimal) -> str:
    """Monto en pesos chilenos sin decimales: '$ 1.234.567'."""
    sign = "-" if value < 0 else ""
    return f"{sign}$ {format_int(abs(value))}"


def format_date(value: date | None) -> str:
    """date(2026, 3, 5) -> '05-03-2026'."""
    return value.strftime("%d-%m-%Y") if value else "-"


def month_label(year: int, month: int) -> str:
    """(2026, 3) -> 'mar 2026'."""
    return f"{MONTHS_SHORT[month - 1]} {year}"
