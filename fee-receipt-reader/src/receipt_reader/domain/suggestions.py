"""Sugerencias para la revisión manual.

Una sugerencia nunca se aplica sola: se muestra junto al campo y el usuario
decide si la usa. Así, por ejemplo, el nombre canónico de un prestador o su
programa habitual no se imponen sobre lo que dice el documento, y ningún
monto se copia de otra boleta.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from receipt_reader.domain.dates import Period, infer_year
from receipt_reader.domain.forms import form_text
from receipt_reader.domain.models import ReceiptData
from receipt_reader.domain.rut import format_rut
from receipt_reader.domain.text import fold, format_thousands


@dataclass(frozen=True)
class Suggestion:
    """Valor propuesto para un campo, con el texto para el formulario y su justificación."""

    field: str
    value: Any
    text: str
    reason: str


@dataclass(frozen=True)
class HabitualProgram:
    program_id: int
    count: int
    total: int


def build_suggestions(
    data: ReceiptData,
    *,
    canonical_name: str | None = None,
    habitual: HabitualProgram | None = None,
    program_names: Mapping[int, str] | None = None,
    folio_hint: int | None = None,
    service_month_hint: int | None = None,
) -> tuple[Suggestion, ...]:
    """Sugerencias aplicables a los campos vacíos o dudosos de una boleta."""
    names = program_names or {}
    found: list[Suggestion] = []
    if canonical_name and (not data.issuer_name or fold(canonical_name) != fold(data.issuer_name)):
        found.append(
            Suggestion(
                "issuer_name",
                canonical_name,
                canonical_name,
                f"Nombre confirmado para el RUT {format_rut(data.issuer_rut)}.",
            )
        )
    if data.program_id is None and habitual is not None:
        label = names.get(habitual.program_id, str(habitual.program_id))
        found.append(
            Suggestion(
                "program_id",
                habitual.program_id,
                str(habitual.program_id),
                f"Programa habitual del prestador: {label} ({habitual.count} de {habitual.total} boletas válidas).",
            )
        )
    if data.gross is None and data.retention is not None and data.net is not None:
        gross = data.net + data.retention
        found.append(
            Suggestion(
                "gross",
                gross,
                form_text("gross", gross),
                f"Líquido más retención impresos ({format_thousands(data.net)} + {format_thousands(data.retention)}).",
            )
        )
    if data.folio is None and folio_hint is not None:
        found.append(Suggestion("folio", folio_hint, str(folio_hint), "Folio indicado en el nombre del archivo."))
    if data.service_period is None and data.issue_date is not None:
        if service_month_hint is not None:
            period = Period(infer_year(service_month_hint, data.issue_date), service_month_hint)
            reason = "Mes indicado en el nombre del archivo."
        else:
            period = Period.of(data.issue_date).shift(-1)
            reason = "Mes anterior a la emisión (lo habitual)."
        found.append(Suggestion("service_period", period, period.iso(), reason))
    return tuple(found)
