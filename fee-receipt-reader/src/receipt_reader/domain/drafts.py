"""Armado de la boleta a validar a partir de lo extraído y de la ubicación del archivo."""

from __future__ import annotations

from typing import Any

from receipt_reader.domain.dates import Period, infer_year
from receipt_reader.domain.extraction import PageExtraction
from receipt_reader.domain.location import LocationHints
from receipt_reader.domain.models import FieldSource, FieldTrace, ReadStatus, ReceiptData, TextKind, value_to_text
from receipt_reader.domain.programs import ProgramMatcher, resolve_program
from receipt_reader.domain.retention import complete_triple
from receipt_reader.domain.validation import ReceiptDraft

FILENAME_CONFIDENCE = 0.5


def _source_for(kind: TextKind) -> FieldSource:
    return FieldSource.NATIVE if kind is TextKind.NATIVE else FieldSource.OCR


def build_draft(
    extraction: PageExtraction,
    *,
    text_kind: TextKind,
    hints: LocationHints,
    folder_program_id: int | None,
    matcher: ProgramMatcher,
    ocr_confidence: float | None,
) -> ReceiptDraft:
    """Combina lo leído en la página con las pistas de la carpeta, sin inventar valores."""
    source = _source_for(text_kind)
    values: dict[str, Any] = {}
    traces: dict[str, FieldTrace] = {}
    for name, extracted in extraction.fields.items():
        values[name] = extracted.value
        shown = extracted.raw if extracted.raw is not None else value_to_text(extracted.value)
        traces[name] = FieldTrace(shown, round(extracted.confidence, 4), source)

    if values.get("issuer_name") is None and hints.name_hint:
        values["issuer_name"] = hints.name_hint
        traces["issuer_name"] = FieldTrace(hints.name_hint, FILENAME_CONFIDENCE, FieldSource.FILENAME)

    if hints.payment_period is not None:
        values["payment_period"] = hints.payment_period
        traces["payment_period"] = FieldTrace(hints.payment_period.iso(), 1.0, FieldSource.FOLDER)

    issue_date = values.get("issue_date")
    if values.get("service_period") is None and hints.service_month_hint and issue_date is not None:
        month = hints.service_month_hint
        period = Period(infer_year(month, issue_date), month)
        values["service_period"] = period
        traces["service_period"] = FieldTrace(period.iso(), FILENAME_CONFIDENCE, FieldSource.FILENAME)

    text_match = matcher.match(extraction.program_text)
    resolution = resolve_program(folder_program_id, text_match, source)
    if resolution.program_id is not None and resolution.source is not None:
        values["program_id"] = resolution.program_id
        confidence = (
            1.0
            if resolution.source is FieldSource.FOLDER
            else traces.get("gloss", FieldTrace(None, 1.0, source)).confidence
        )
        traces["program_id"] = FieldTrace(text_match.alias or str(resolution.program_id), confidence, resolution.source)

    triple = complete_triple(values.get("gross"), values.get("retention"), values.get("net"))
    values["retention"] = triple.retention
    values["net"] = triple.net
    for name in triple.derived:
        traces[name] = FieldTrace(value_to_text(values[name]), None, FieldSource.DERIVED)

    data = ReceiptData(**{name: value for name, value in values.items() if value is not None})
    return ReceiptDraft(
        data=data,
        traces=traces,
        read_status=ReadStatus.OK,
        folder_program_id=folder_program_id,
        folder_program_code=hints.program_code,
        text_program_id=text_match.program_id,
        text_program_ambiguous=text_match.ambiguous,
        ocr_confidence=ocr_confidence,
        folio_hint=hints.folio_hint,
    )


def build_error_draft(read_status: ReadStatus, *, hints: LocationHints, folder_program_id: int | None) -> ReceiptDraft:
    """Boleta de un archivo ilegible o sin página de boleta: conserva lo que dice la carpeta."""
    values: dict[str, Any] = {}
    traces: dict[str, FieldTrace] = {}
    if hints.payment_period is not None:
        values["payment_period"] = hints.payment_period
        traces["payment_period"] = FieldTrace(hints.payment_period.iso(), 1.0, FieldSource.FOLDER)
    if folder_program_id is not None:
        values["program_id"] = folder_program_id
        traces["program_id"] = FieldTrace(hints.program_code, 1.0, FieldSource.FOLDER)
    if hints.name_hint:
        values["issuer_name"] = hints.name_hint
        traces["issuer_name"] = FieldTrace(hints.name_hint, FILENAME_CONFIDENCE, FieldSource.FILENAME)
    return ReceiptDraft(
        data=ReceiptData(**values),
        traces=traces,
        read_status=read_status,
        folder_program_id=folder_program_id,
        folder_program_code=hints.program_code,
        folio_hint=hints.folio_hint,
    )
