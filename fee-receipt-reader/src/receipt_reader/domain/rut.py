"""RUT chileno: dígito verificador módulo 11, forma canónica y búsqueda en texto.

La forma canónica es el cuerpo sin puntos ni ceros a la izquierda, un guion
y el dígito verificador en mayúscula: '41234567-8'. Los puntos se agregan
solo al mostrar ('41.234.567-8').
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from receipt_reader.domain.text import normalize_text
from receipt_reader.errors import FieldFormatError

# Cuerpo de 7 u 8 dígitos (con o sin puntos de miles), guion con espacios opcionales y DV.
RUT_PATTERN = r"(?<![\d.])(\d{1,2}(?:\.?\d{3}){2})\s*-\s*([\dkK])(?![\dA-Za-z])"
_RUT_RE = re.compile(RUT_PATTERN)
_RUT_FULL_RE = re.compile(r"\s*" + RUT_PATTERN + r"\s*")


def compute_dv(body: int) -> str:
    """Dígito verificador módulo 11 con factores cíclicos 2 a 7."""
    if body <= 0:
        raise FieldFormatError("El cuerpo del RUT debe ser un número positivo.")
    total = 0
    factor = 2
    while body:
        total += (body % 10) * factor
        body //= 10
        factor = 2 if factor == 7 else factor + 1
    result = 11 - total % 11
    if result == 11:
        return "0"
    if result == 10:
        return "K"
    return str(result)


def make_rut(body: int) -> str:
    """Forma canónica de un cuerpo con su dígito verificador correcto."""
    return f"{body}-{compute_dv(body)}"


def normalize_rut(text: str) -> str | None:
    """Lleva un RUT escrito de cualquier forma a la canónica, sin validar el DV.

    Devuelve None si el texto no tiene forma de RUT.
    """
    match = _RUT_FULL_RE.fullmatch(normalize_text(text))
    if match is None:
        return None
    body = int(match.group(1).replace(".", ""))
    return f"{body}-{match.group(2).upper()}"


def is_valid_rut(rut: str | None) -> bool:
    """True si el RUT tiene forma válida y su dígito verificador es correcto."""
    if not rut:
        return False
    canonical = normalize_rut(rut)
    if canonical is None:
        return False
    body, dv = canonical.split("-")
    return compute_dv(int(body)) == dv


def parse_rut(text: str) -> str:
    """Valida y devuelve la forma canónica; lanza FieldFormatError con un mensaje claro."""
    canonical = normalize_rut(text)
    if canonical is None:
        raise FieldFormatError(f"'{text.strip()}' no es un RUT (use el formato 41.234.567-3).")
    body, dv = canonical.split("-")
    expected = compute_dv(int(body))
    if dv != expected:
        raise FieldFormatError(f"El dígito verificador del RUT {format_rut(canonical)} no es válido.")
    return canonical


def format_rut(rut: str | None) -> str:
    """'41234567-8' -> '41.234.567-8'. Un texto sin forma de RUT se devuelve igual."""
    if not rut:
        return ""
    canonical = normalize_rut(rut)
    if canonical is None:
        return rut
    body, dv = canonical.split("-")
    return f"{int(body):,}".replace(",", ".") + f"-{dv}"


@dataclass(frozen=True)
class RutMatch:
    """RUT encontrado en un texto: forma canónica, validez y posición."""

    rut: str
    valid: bool
    start: int
    end: int


def find_ruts(text: str) -> list[RutMatch]:
    """Todos los RUT con forma válida del texto, en orden de aparición."""
    normalized = normalize_text(text)
    found: list[RutMatch] = []
    for match in _RUT_RE.finditer(normalized):
        body = int(match.group(1).replace(".", ""))
        dv = match.group(2).upper()
        found.append(RutMatch(f"{body}-{dv}", compute_dv(body) == dv, match.start(), match.end()))
    return found
