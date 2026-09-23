"""Formato de números, montos y fechas según la convención chilena.

Punto como separador de miles y coma decimal: 1.234.567 y 8,3 %.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from staffing_simulator.domain.models import MONTH_NAMES
from staffing_simulator.errors import ValidationError

MILLION = 1_000_000

Number = float | int | Decimal


def format_int(value: Number) -> str:
    """12345678 -> '12.345.678'."""
    rounded = int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{rounded:,}".replace(",", ".")


def format_decimal(value: Number, decimals: int = 1) -> str:
    """1234.56 -> '1.234,6'."""
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    text = f"{rounded:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def format_clp(value: Number) -> str:
    """Monto en pesos chilenos sin decimales: '$ 1.234.567'."""
    sign = "-" if value < 0 else ""
    return f"{sign}$ {format_int(abs(value))}"


def format_signed_clp(value: Number) -> str:
    """Diferencia en pesos con signo explícito: '+$ 1.234', '-$ 1.234', '$ 0'."""
    if value == 0:
        return "$ 0"
    sign = "+" if value > 0 else "-"
    return f"{sign}$ {format_int(abs(value))}"


def format_millions(value: Number, decimals: int = 1) -> str:
    """Monto compacto en millones: 571780835 -> '$ 571,8 millones'."""
    sign = "-" if value < 0 else ""
    return f"{sign}$ {format_decimal(abs(Decimal(str(value))) / MILLION, decimals)} millones"


def format_mm(value: Number, decimals: int = 1) -> str:
    """Millones sin unidad para rótulos de gráficos: 571780835 -> '571,8'."""
    return format_decimal(Decimal(str(value)) / MILLION, decimals)


def format_pct(ratio: Number | None, decimals: int = 1) -> str:
    """0.853 -> '85,3 %'. None se muestra como guion."""
    if ratio is None:
        return "-"
    return f"{format_decimal(Decimal(str(ratio)) * 100, decimals)} %"


def format_signed_pct(ratio: Number | None, decimals: int = 1) -> str:
    """0.083 -> '+8,3 %'; -0.012 -> '-1,2 %'. None se muestra como guion."""
    if ratio is None:
        return "-"
    text = format_pct(ratio, decimals)
    if text.startswith("-") or Decimal(str(ratio)).quantize(Decimal(1).scaleb(-decimals - 2)) == 0:
        return text
    return f"+{text}"


def format_hours(value: Number | None) -> str:
    """Horas con un decimal solo si hace falta: 44 -> '44'; 7.5 -> '7,5'; 1513.4 -> '1.513,4'."""
    if value is None:
        return "-"
    rounded = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        return format_int(rounded)
    return format_decimal(rounded, 1)


def format_date(value: date | None) -> str:
    """date(2026, 3, 5) -> '05-03-2026'."""
    return value.strftime("%d-%m-%Y") if value else "-"


def month_name(month: int) -> str:
    """3 -> 'marzo'."""
    return MONTH_NAMES[month - 1]


def month_range_label(months: tuple[int, ...] | list[int], year: int) -> str:
    """Meses con datos como rango legible: (1..8) -> 'enero a agosto de 2026'."""
    if not months:
        return f"sin meses registrados en {year}"
    first, last = min(months), max(months)
    if first == last:
        return f"{month_name(first)} de {year}"
    return f"{month_name(first)} a {month_name(last)} de {year}"


_AMOUNT_PATTERN = re.compile(r"^\d{1,3}(\.\d{3})+$|^\d+$")


def parse_amount(text: str) -> int:
    """Lee un monto entero de pesos escrito con o sin puntos de miles ('$ 300.700.000')."""
    clean = (text or "").strip().replace("$", "").replace(" ", "")
    if not clean:
        raise ValidationError("Indique un monto en pesos.")
    if clean.startswith("-"):
        raise ValidationError("El monto no puede ser negativo.")
    if not _AMOUNT_PATTERN.match(clean):
        raise ValidationError(
            f"El monto '{text.strip()}' no es válido. Escriba solo números enteros, por ejemplo 300.700.000."
        )
    return int(clean.replace(".", ""))
