"""RUT chileno: dígito verificador (módulo 11), normalización y formato."""

from __future__ import annotations

import re

from staffing_simulator.errors import ValidationError

_RUT_PATTERN = re.compile(r"^(\d{1,8})-?([\dK])$")


def check_digit(body: int) -> str:
    """Dígito verificador del cuerpo numérico de un RUT."""
    if body <= 0:
        raise ValidationError("El número de RUT debe ser positivo.")
    total, factor = 0, 2
    for digit in reversed(str(body)):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    remainder = 11 - total % 11
    if remainder == 11:
        return "0"
    if remainder == 10:
        return "K"
    return str(remainder)


def normalize_rut(text: str) -> str:
    """Normaliza a la forma '41234567-3' y valida el dígito verificador."""
    compact = re.sub(r"[\s.]", "", text or "").upper()
    match = _RUT_PATTERN.match(compact)
    if match is None:
        raise ValidationError(f"El RUT '{text}' no tiene un formato válido (ejemplo: 41.234.567-3).")
    body, digit = int(match.group(1)), match.group(2)
    if check_digit(body) != digit:
        raise ValidationError(f"El dígito verificador del RUT '{text}' no es correcto.")
    return f"{body}-{digit}"


def format_rut(normalized: str) -> str:
    """'41234567-3' -> '41.234.567-3'."""
    body, digit = normalized.split("-")
    return f"{int(body):,}".replace(",", ".") + f"-{digit}"
