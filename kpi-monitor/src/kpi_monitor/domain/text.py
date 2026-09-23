"""Formato de números y textos según la convención chilena.

Vive en el dominio porque los mensajes de alertas y las descripciones de
metas se redactan aquí; la interfaz reutiliza las mismas funciones.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from kpi_monitor.domain.enums import Scale

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


def format_int(value: float | int | Decimal) -> str:
    """12345678 -> '12.345.678'."""
    rounded = int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{rounded:,}".replace(",", ".")


def plural(count: int, singular: str, plural_form: str) -> str:
    """Cantidad con el sustantivo en el número que corresponde: (1, 'fila', 'filas') -> '1 fila'."""
    return f"{format_int(count)} {singular if count == 1 else plural_form}"


def format_decimal(value: float | Decimal, decimals: int = 1) -> str:
    """1234.56 -> '1.234,6' (redondeo half-up explícito)."""
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    text = f"{rounded:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def format_pct(ratio: float | None, decimals: int = 1) -> str:
    """0.853 -> '85,3 %'. None se muestra como guion."""
    if ratio is None:
        return "-"
    return f"{format_decimal(ratio * 100, decimals)} %"


def format_value(value: float | None, scale: Scale) -> str:
    """Valor de un indicador en su escala natural."""
    if value is None:
        return "-"
    if scale is Scale.PROPORTION:
        return format_pct(value)
    if scale is Scale.DAYS:
        return f"{format_decimal(value, 1)} días"
    return format_decimal(value, 2)


def month_name(month: int) -> str:
    """3 -> 'marzo'."""
    return MONTHS[month - 1]


def period_label(year: int, month: int) -> str:
    """(2026, 8) -> 'agosto 2026'."""
    return f"{MONTHS[month - 1]} {year}"


def join_names(names: list[str] | tuple[str, ...]) -> str:
    """['a', 'b', 'c'] -> 'a, b y c'."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " y " + names[-1]


def join_months(months: list[int] | tuple[int, ...]) -> str:
    """[2, 5, 7] -> 'febrero, mayo y julio'."""
    return join_names([MONTHS[m - 1] for m in months])
