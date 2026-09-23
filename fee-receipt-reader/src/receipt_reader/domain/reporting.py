"""Estructuras y reglas puras de los informes (los agregados se calculan en SQL)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import PeriodAxis, ReceiptStatus

SHEET_NAME_MAX = 31
_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
NO_PERIOD_LABEL = "Sin período"
NO_PROGRAM_LABEL = "Sin programa"


@dataclass(frozen=True)
class Totals:
    """Cantidad de boletas y montos acumulados."""

    count: int = 0
    gross: int = 0
    retention: int = 0
    net: int = 0


@dataclass(frozen=True)
class SummaryRow:
    """Fila de la tabla programa por período."""

    program_id: int | None
    program_name: str
    period: Period | None
    count: int
    gross: int
    retention: int
    net: int


@dataclass(frozen=True)
class SummaryTable:
    """Resumen completo: filas, subtotales por programa y total general."""

    axis: PeriodAxis
    rows: tuple[SummaryRow, ...]
    program_totals: tuple[SummaryRow, ...]
    grand_total: Totals

    def periods(self) -> list[Period | None]:
        ordered = sorted({row.period for row in self.rows if row.period is not None})
        result: list[Period | None] = list(ordered)
        if any(row.period is None for row in self.rows):
            result.append(None)
        return result


@dataclass(frozen=True)
class StatusCount:
    status: ReceiptStatus
    count: int


@dataclass(frozen=True)
class ReceiptFilter:
    """Filtros de las vistas y de los informes."""

    program_id: int | None = None
    statuses: frozenset[ReceiptStatus] | None = None
    period: Period | None = None
    axis: PeriodAxis = PeriodAxis.SERVICE
    issue_code: str | None = None
    text: str | None = None


class HasPeriods(Protocol):
    """Lo que se necesita de una boleta para ubicarla en un eje (sirve para ReceiptData y ReceiptRow)."""

    @property
    def service_period(self) -> Period | None: ...

    @property
    def issue_date(self) -> date | None: ...

    @property
    def payment_period(self) -> Period | None: ...


def period_for_axis(receipt: HasPeriods, axis: PeriodAxis) -> Period | None:
    """Período de una boleta según el eje elegido (la misma regla que aplican las consultas SQL)."""
    if axis is PeriodAxis.SERVICE:
        return receipt.service_period
    if axis is PeriodAxis.ISSUE:
        return Period.of(receipt.issue_date) if receipt.issue_date is not None else None
    return receipt.payment_period


def sanitize_sheet_name(name: str) -> str:
    cleaned = _SHEET_FORBIDDEN.sub(" ", name).strip().strip("'") or "Hoja"
    return re.sub(r"\s+", " ", cleaned)[:SHEET_NAME_MAX]


def unique_sheet_names(names: Sequence[str], reserved: Iterable[str] = ()) -> list[str]:
    """Nombres de hoja válidos y únicos sin distinguir mayúsculas (sufijo ' (2)', ' (3)'...)."""
    taken = {name.casefold() for name in reserved}
    result: list[str] = []
    for name in names:
        base = sanitize_sheet_name(name)
        candidate = base
        counter = 2
        while candidate.casefold() in taken:
            suffix = f" ({counter})"
            candidate = base[: SHEET_NAME_MAX - len(suffix)] + suffix
            counter += 1
        taken.add(candidate.casefold())
        result.append(candidate)
    return result


def safe_filename(text: str, max_length: int = 60) -> str:
    """Nombre de archivo portable: sin tildes, sin caracteres reservados."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip("._")
    return cleaned[:max_length] or "archivo"
