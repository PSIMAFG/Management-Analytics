"""Fechas y períodos mensuales.

Las fechas de una boleta se aceptan solo en dos formas: 'dd de <mes> de aaaa'
(formato de la boleta electrónica) y 'dd/mm/aaaa' (también con '-' o '.').
La forma numérica se interpreta siempre como día/mes: nunca se prueba
mes/día cuando la primera lectura no es una fecha real.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from receipt_reader.domain.text import MONTH_NAMES, MONTH_PATTERN, fold, month_from_name, normalize_text
from receipt_reader.errors import FieldFormatError

_LONG_DATE = re.compile(r"(?<!\d)(\d{1,2})\s+DE\s+(" + MONTH_PATTERN + r")\s+(?:DE|DEL)\s+(\d{4})(?!\d)")
_NUMERIC_DATE = re.compile(r"(?<![\d.,/-])(\d{1,2})([/.-])(\d{1,2})\2(\d{4})(?!\d)")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_PERIOD_ISO = re.compile(r"(\d{4})-(\d{1,2})")
_PERIOD_MONTH_FIRST = re.compile(r"(\d{1,2})[/-](\d{4})")
_PERIOD_TEXT = re.compile(r"(" + MONTH_PATTERN + r")\s+(?:DE\s+|DEL\s+)?(\d{4})")


@dataclass(frozen=True, order=True)
class Period:
    """Mes calendario (año, mes)."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise FieldFormatError(f"El mes {self.month} no existe.")
        if not 1900 <= self.year <= 2200:
            raise FieldFormatError(f"El año {self.year} está fuera de rango.")

    @classmethod
    def of(cls, value: date) -> Period:
        return cls(value.year, value.month)

    @classmethod
    def parse(cls, text: str) -> Period:
        """Acepta 'aaaa-mm', 'mm-aaaa', 'mm/aaaa' y 'octubre 2025'."""
        raw = normalize_text(text).strip()
        folded = fold(raw)
        if match := _PERIOD_ISO.fullmatch(folded):
            return cls(int(match.group(1)), int(match.group(2)))
        if match := _PERIOD_MONTH_FIRST.fullmatch(folded):
            return cls(int(match.group(2)), int(match.group(1)))
        if match := _PERIOD_TEXT.fullmatch(folded):
            return cls(int(match.group(2)), month_from_name(match.group(1)) or 0)
        raise FieldFormatError(f"'{raw}' no es un período válido (use aaaa-mm, por ejemplo 2025-10).")

    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def label(self) -> str:
        """'octubre 2025'."""
        return f"{MONTH_NAMES[self.month - 1]} {self.year}"

    def shift(self, months: int) -> Period:
        index = self.year * 12 + (self.month - 1) + months
        return Period(index // 12, index % 12 + 1)

    def months_until(self, other: Period) -> int:
        """Cantidad de meses desde este período hasta `other` (negativo si es anterior)."""
        return (other.year * 12 + other.month) - (self.year * 12 + self.month)

    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def __str__(self) -> str:
        return self.iso()


def _make_date(year: int, month: int, day: int, raw: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as error:
        raise FieldFormatError(f"'{raw}' no es una fecha real del calendario.") from error


def find_long_date(text: str) -> date | None:
    """Primera fecha 'dd de <mes> de aaaa' del texto que sea real; None si no hay."""
    folded = fold(normalize_text(text))
    for match in _LONG_DATE.finditer(folded):
        month = month_from_name(match.group(2))
        if month is None:
            continue
        try:
            return _make_date(int(match.group(3)), month, int(match.group(1)), match.group(0))
        except FieldFormatError:
            continue
    return None


def find_numeric_date(text: str) -> date | None:
    """Primera fecha 'dd/mm/aaaa' del texto que sea real, leída siempre como día/mes."""
    for match in _NUMERIC_DATE.finditer(normalize_text(text)):
        try:
            return _make_date(int(match.group(4)), int(match.group(3)), int(match.group(1)), match.group(0))
        except FieldFormatError:
            continue
    return None


def parse_date(text: str) -> date:
    """Fecha escrita por el usuario o leída de la boleta.

    Acepta 'dd de mes de aaaa', 'dd/mm/aaaa', 'dd-mm-aaaa', 'dd.mm.aaaa' y
    'aaaa-mm-dd'. '13/25/2025' se rechaza: nunca se reinterpreta como mes/día.
    """
    raw = normalize_text(text).strip()
    if match := _ISO_DATE.fullmatch(raw):
        return _make_date(int(match.group(1)), int(match.group(2)), int(match.group(3)), raw)
    folded = fold(raw)
    if match := _LONG_DATE.fullmatch(folded):
        month = month_from_name(match.group(2)) or 0
        return _make_date(int(match.group(3)), month, int(match.group(1)), raw)
    if match := re.fullmatch(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4})", raw):
        return _make_date(int(match.group(4)), int(match.group(3)), int(match.group(1)), raw)
    raise FieldFormatError(f"'{raw}' no es una fecha válida (use dd-mm-aaaa).")


def infer_year(month: int, reference: date) -> int:
    """Año de un mes de servicio sin año explícito, a partir de la fecha de emisión.

    Si el mes es posterior al mes de emisión, corresponde al año anterior
    (servicio de diciembre facturado en enero).
    """
    return reference.year - 1 if month > reference.month else reference.year
