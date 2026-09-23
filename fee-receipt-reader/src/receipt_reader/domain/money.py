"""Montos y números en formato chileno.

Reglas explícitas y únicas para toda la aplicación:

- El punto separa miles solo si todos los grupos después del primero tienen
  exactamente 3 dígitos ('1.234.567').
- La coma es el separador decimal. En un monto en pesos solo se admite una
  parte decimal nula (',00' o ',0'), que se descarta: '45.000,00' es 45.000.
- Todo lo ambiguo se rechaza en lugar de adivinar: '12,5' no es un monto en
  pesos, '12.5' tampoco y '1,234' no se interpreta como mil doscientos.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from receipt_reader.domain.text import normalize_text
from receipt_reader.errors import FieldFormatError

_GROUPED = re.compile(r"\d{1,3}(?:\.\d{3})+")
_PLAIN = re.compile(r"\d+")
_CURRENCY_PREFIX = re.compile(r"^\$\s*:?\s*")
_DECIMAL = re.compile(r"(\d{1,6})(?:[.,](\d{1,2}))?")


def parse_clp(text: str) -> int:
    """Convierte un monto escrito en pesos chilenos a entero.

    >>> parse_clp("$ 1.234.567")
    1234567
    >>> parse_clp("45.000,00")
    45000
    """
    raw = normalize_text(text).strip()
    cleaned = _CURRENCY_PREFIX.sub("", raw).strip()
    if not cleaned:
        raise FieldFormatError("El monto está vacío.")
    integer_part, separator, decimals = cleaned.partition(",")
    if separator and not re.fullmatch(r"0{1,2}", decimals):
        raise FieldFormatError(
            f"'{raw}' no es un monto en pesos válido: la coma es decimal y los pesos no llevan decimales."
        )
    if _GROUPED.fullmatch(integer_part) or _PLAIN.fullmatch(integer_part):
        return int(integer_part.replace(".", ""))
    raise FieldFormatError(f"'{raw}' no es un monto válido (use punto para los miles, por ejemplo 1.234.567).")


def try_parse_clp(text: str) -> int | None:
    """Como `parse_clp`, pero devuelve None si el texto es ambiguo o inválido."""
    try:
        return parse_clp(text)
    except FieldFormatError:
        return None


def parse_decimal(text: str) -> Decimal:
    """Número con hasta 2 decimales, con coma o punto decimal ('12,5', '14.50', '22').

    Se usa para horas y porcentajes impresos. Un separador seguido de 3
    dígitos ('1.234') es ambiguo y se rechaza.
    """
    raw = normalize_text(text).strip()
    match = _DECIMAL.fullmatch(raw)
    if match is None:
        raise FieldFormatError(f"'{raw}' no es un número válido (use coma decimal, por ejemplo 12,5).")
    value = match.group(1) + ("." + match.group(2) if match.group(2) else "")
    try:
        return Decimal(value)
    except InvalidOperation as error:  # pragma: no cover - el patrón ya garantiza el formato
        raise FieldFormatError(f"'{raw}' no es un número válido.") from error


def parse_percent_bp(text: str) -> int:
    """'14.50' o '14,5' (con o sin '%') -> 1450 puntos básicos."""
    cleaned = normalize_text(text).replace("%", "").strip()
    return round_half_up(parse_decimal(cleaned) * 100)


def round_half_up(value: Decimal) -> int:
    """Redondeo comercial explícito al entero más cercano (0,5 sube)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
