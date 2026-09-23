"""Revisión manual: cola de pendientes, detalle, corrección auditada, aprobación y descarte.

Reglas:

- Guardar una corrección cambia solo la boleta editada (nunca se propagan
  montos a otras boletas), deja un registro de auditoría por campo y vuelve
  a validar. Los montos que se habían deducido (retención o líquido) se
  recalculan con los valores corregidos. Queda "corregida" si ya no tiene
  incidencias bloqueantes.
- Aprobar da por revisadas las incidencias bloqueantes aceptables (monto
  fuera de rango, fecha fuera de ventana, programa de carpeta distinto del
  texto, entre otras) y guarda sus códigos: una incidencia aceptable que
  aparezca después vuelve a dejar la boleta pendiente. Si queda alguna no
  aceptable, la aprobación se rechaza.
- Descartar exige un motivo; la boleta sigue en la base y en el Excel.
- Las sugerencias (nombre confirmado, programa habitual, bruto desde líquido
  más retención) solo se muestran: aplicarlas es una corrección más.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from receipt_reader.data import catalog_repo, db, receipt_repo
from receipt_reader.data.documents import render_page_png
from receipt_reader.domain.forms import EDITABLE_FIELDS, form_text, parse_receipt_form
from receipt_reader.domain.models import (
    FIELD_LABELS,
    REVIEW_STATUSES,
    FieldSource,
    FieldTrace,
    Provider,
    ReadStatus,
    ReceiptData,
    ReceiptStatus,
    value_to_text,
)
from receipt_reader.domain.programs import habitual_program
from receipt_reader.domain.records import Correction, ReceiptRecord, ReceiptRow
from receipt_reader.domain.reporting import ReceiptFilter
from receipt_reader.domain.retention import complete_triple
from receipt_reader.domain.rut import parse_rut
from receipt_reader.domain.suggestions import HabitualProgram, Suggestion, build_suggestions
from receipt_reader.domain.text import collapse_spaces
from receipt_reader.domain.validation import ISSUE_CATALOG, Issue, acceptable_codes, acceptance_blockers
from receipt_reader.errors import ValidationError
from receipt_reader.services.context import Clock, evaluate, load_snapshot, revalidate, system_clock

STATUS_FIELD = "status"
DERIVABLE_FIELDS = ("retention", "net")


@dataclass(frozen=True)
class ReceiptDetail:
    """Todo lo que la vista de revisión necesita de una boleta."""

    record: ReceiptRecord
    program_name: str | None
    form_values: dict[str, str]
    suggestions: tuple[Suggestion, ...]
    corrections: tuple[Correction, ...]
    approval_blockers: tuple[Issue, ...]
    canonical_name: str | None

    @property
    def can_approve(self) -> bool:
        record = self.record
        if record.status in (ReceiptStatus.DISCARDED, ReceiptStatus.APPROVED, ReceiptStatus.CORRECTED):
            return False
        if record.read_status is not ReadStatus.OK and not record.edited:
            return False
        return not self.approval_blockers


@dataclass(frozen=True)
class IssueOption:
    """Opción del filtro por incidencia de la cola de revisión."""

    code: str
    title: str
    count: int


class ReviewService:
    """Casos de uso de la revisión manual."""

    def __init__(self, db_path: Path, *, clock: Clock = system_clock) -> None:
        self.db_path = db_path
        self._clock = clock

    def queue(self, filt: ReceiptFilter | None = None) -> list[ReceiptRow]:
        """Boletas que esperan revisión (pendientes y con error), filtrables por incidencia."""
        base = filt or ReceiptFilter()
        statuses = base.statuses or REVIEW_STATUSES
        query = ReceiptFilter(
            program_id=base.program_id,
            statuses=frozenset(statuses),
            period=base.period,
            axis=base.axis,
            issue_code=base.issue_code,
            text=base.text,
        )
        with db.session(self.db_path) as conn:
            return receipt_repo.list_rows(conn, query)

    def issue_options(self, filt: ReceiptFilter | None = None) -> list[IssueOption]:
        """Incidencias presentes en la cola (con su programa, período y eje), con las boletas afectadas."""
        with db.session(self.db_path) as conn:
            counts = receipt_repo.issue_code_counts(conn, filt or ReceiptFilter())
        return [
            IssueOption(code.value, spec.title, counts[code.value])
            for code, spec in ISSUE_CATALOG.items()
            if counts.get(code.value)
        ]

    def _detail(self, conn: sqlite3.Connection, receipt_id: int) -> ReceiptDetail:
        snapshot = load_snapshot(conn)
        record = receipt_repo.load_receipt(conn, receipt_id)
        data = record.data
        provider = catalog_repo.get_provider(conn, data.issuer_rut) if data.issuer_rut else None
        habitual = None
        if data.issuer_rut and data.program_id is None:
            history = receipt_repo.valid_program_ids_for_rut(conn, data.issuer_rut, exclude_id=record.id)
            found = habitual_program(history)
            if found is not None:
                habitual = HabitualProgram(found[0], found[1], len(history))
        suggestions = build_suggestions(
            data,
            canonical_name=provider.canonical_name if provider else None,
            habitual=habitual,
            program_names=snapshot.program_names,
            folio_hint=record.file.folio_hint,
            service_month_hint=record.file.service_month_hint,
        )
        current = evaluate(conn, snapshot, record)
        return ReceiptDetail(
            record=record,
            program_name=snapshot.program_names.get(data.program_id) if data.program_id else None,
            form_values={name: form_text(name, data.get(name)) for name in EDITABLE_FIELDS},
            suggestions=suggestions,
            corrections=tuple(receipt_repo.list_corrections(conn, receipt_id)),
            approval_blockers=tuple(acceptance_blockers(current.issues)),
            canonical_name=provider.canonical_name if provider else None,
        )

    def detail(self, receipt_id: int) -> ReceiptDetail:
        with db.session(self.db_path) as conn:
            return self._detail(conn, receipt_id)

    def page_image(self, receipt_id: int, dpi: int = 110) -> bytes:
        """PNG de la página exacta de la boleta, para la vista previa."""
        with db.session(self.db_path) as conn:
            record = receipt_repo.load_receipt(conn, receipt_id)
        return render_page_png(Path(record.file.path), record.page_index, dpi)

    def save_correction(self, receipt_id: int, values: Mapping[str, str | None]) -> ReceiptDetail:
        """Aplica los campos editados (texto del formulario), audita cada cambio y revalida."""
        unknown = [name for name in values if name not in EDITABLE_FIELDS]
        if unknown:
            raise ValidationError(f"Campos no editables: {', '.join(unknown)}.")
        parsed = parse_receipt_form(values)
        now = self._clock()
        with db.session(self.db_path) as conn, db.transaction(conn):
            record = receipt_repo.load_receipt(conn, receipt_id)
            if record.status is ReceiptStatus.DISCARDED:
                raise ValidationError("La boleta está descartada; restáurela antes de corregirla.")
            changes: dict[str, Any] = {name: value for name, value in parsed.items() if value != record.data.get(name)}
            if not changes:
                raise ValidationError("No hay cambios que guardar.")
            data = record.data.with_changes(**changes)
            traces: dict[str, FieldTrace] = {
                name: FieldTrace(value_to_text(value), 1.0, FieldSource.USER) for name, value in changes.items()
            }
            derived = _rederive(record, data, edited=set(changes))
            data = data.with_changes(**derived)
            traces |= {
                name: FieldTrace(value_to_text(value), None, FieldSource.DERIVED) for name, value in derived.items()
            }
            corrections = [
                Correction(receipt_id, name, value_to_text(record.data.get(name)), value_to_text(value), now)
                for name, value in (changes | derived).items()
            ]
            # Queda pendiente mientras se revalida: el estado final lo decide la validación. La
            # aprobación anterior no se conserva: los datos cambiaron y hay que volver a revisarlos.
            receipt_repo.update_receipt(
                conn,
                receipt_id,
                data=data,
                status=ReceiptStatus.PENDING,
                edited=True,
                accepted_at=None,
                discard_reason=None,
                updated_at=now,
            )
            receipt_repo.replace_acceptance(conn, receipt_id, ())
            receipt_repo.save_traces(conn, receipt_id, traces)
            receipt_repo.add_corrections(conn, corrections)
            affected = [receipt_id]
            for rut, folio in {(record.data.issuer_rut, record.data.folio), (data.issuer_rut, data.folio)}:
                affected += receipt_repo.receipt_ids_with_key(conn, rut, folio)
            revalidate(conn, load_snapshot(conn), affected, now)
            return self._detail(conn, receipt_id)

    def approve(self, receipt_id: int) -> ReceiptDetail:
        """Aprobación explícita: acepta las incidencias aceptables; rechaza si quedan otras."""
        now = self._clock()
        with db.session(self.db_path) as conn, db.transaction(conn):
            snapshot = load_snapshot(conn)
            record = receipt_repo.load_receipt(conn, receipt_id)
            if record.status is ReceiptStatus.DISCARDED:
                raise ValidationError("La boleta está descartada; restáurela antes de aprobarla.")
            if record.read_status is not ReadStatus.OK and not record.edited:
                raise ValidationError(
                    "La boleta no tiene datos leídos: complete los campos y guarde la corrección, o descártela."
                )
            if record.status.is_valid:
                raise ValidationError("La boleta ya está aprobada.")
            issues = evaluate(conn, snapshot, record).issues
            blockers = acceptance_blockers(issues)
            if blockers:
                titles = ", ".join(issue.title.lower() for issue in blockers)
                raise ValidationError(f"No se puede aprobar mientras tenga incidencias por corregir: {titles}.")
            # Se aceptan solo las incidencias presentes ahora; si luego aparece otra, vuelve a la cola.
            receipt_repo.replace_acceptance(conn, receipt_id, acceptable_codes(issues))
            receipt_repo.update_receipt(
                conn,
                receipt_id,
                data=record.data,
                status=record.status,
                edited=record.edited,
                accepted_at=now,
                discard_reason=None,
                updated_at=now,
            )
            results = revalidate(conn, snapshot, [receipt_id], now)
            receipt_repo.add_corrections(
                conn,
                [Correction(receipt_id, STATUS_FIELD, record.status.value, results[0].status.value, now)],
            )
            return self._detail(conn, receipt_id)

    def discard(self, receipt_id: int, reason: str) -> ReceiptDetail:
        """Descarta la boleta con un motivo; no se borra y sigue apareciendo en la hoja Base."""
        cleaned = collapse_spaces(reason or "")
        if len(cleaned) < 3:
            raise ValidationError("Indique el motivo del descarte.")
        now = self._clock()
        with db.session(self.db_path) as conn, db.transaction(conn):
            record = receipt_repo.load_receipt(conn, receipt_id)
            if record.status is ReceiptStatus.DISCARDED:
                raise ValidationError("La boleta ya está descartada.")
            receipt_repo.update_receipt(
                conn,
                receipt_id,
                data=record.data,
                status=ReceiptStatus.DISCARDED,
                edited=record.edited,
                accepted_at=record.accepted_at,
                discard_reason=cleaned,
                updated_at=now,
            )
            receipt_repo.add_corrections(
                conn, [Correction(receipt_id, STATUS_FIELD, record.status.value, ReceiptStatus.DISCARDED.value, now)]
            )
            peers = receipt_repo.receipt_ids_with_key(conn, record.data.issuer_rut, record.data.folio)
            revalidate(conn, load_snapshot(conn), [receipt_id, *peers], now)
            return self._detail(conn, receipt_id)

    def restore(self, receipt_id: int) -> ReceiptDetail:
        """Deshace un descarte y vuelve a validar la boleta."""
        now = self._clock()
        with db.session(self.db_path) as conn, db.transaction(conn):
            record = receipt_repo.load_receipt(conn, receipt_id)
            if record.status is not ReceiptStatus.DISCARDED:
                raise ValidationError("Solo se pueden restaurar boletas descartadas.")
            receipt_repo.update_receipt(
                conn,
                receipt_id,
                data=record.data,
                status=ReceiptStatus.PENDING,
                edited=record.edited,
                accepted_at=record.accepted_at,
                discard_reason=None,
                updated_at=now,
            )
            peers = receipt_repo.receipt_ids_with_key(conn, record.data.issuer_rut, record.data.folio)
            results = revalidate(conn, load_snapshot(conn), [*peers, receipt_id], now)
            final = next(result.status for result in results if result.record.id == receipt_id)
            receipt_repo.add_corrections(
                conn, [Correction(receipt_id, STATUS_FIELD, ReceiptStatus.DISCARDED.value, final.value, now)]
            )
            return self._detail(conn, receipt_id)

    def confirm_provider_name(self, rut: str, name: str) -> Provider:
        """Fija el nombre canónico de un prestador (se usa en informes y como sugerencia)."""
        canonical_rut = parse_rut(rut)
        cleaned = collapse_spaces(name or "")
        if sum(char.isalpha() for char in cleaned) < 3:
            raise ValidationError("Escriba el nombre del prestador.")
        provider = Provider(canonical_rut, cleaned, self._clock())
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.upsert_providers(conn, [provider])
        return provider


def field_label(name: str) -> str:
    """Rótulo en español de un campo (para la auditoría y los mensajes)."""
    return FIELD_LABELS.get(name, name)


def _rederive(record: ReceiptRecord, data: ReceiptData, *, edited: set[str]) -> dict[str, Any]:
    """Montos deducidos que cambian con la corrección (B09: los derivados se recalculan al guardar).

    Solo se recalculan la retención o el líquido que no venían impresos (origen deducido) y que
    el usuario no escribió; el bruto nunca se deduce. Devuelve los campos cuyo valor cambia.
    """
    names = [
        name
        for name in DERIVABLE_FIELDS
        if name not in edited and (trace := record.traces.get(name)) is not None and trace.source is FieldSource.DERIVED
    ]
    if not names or data.gross is None:
        return {}
    base = {name: (None if name in names else data.get(name)) for name in DERIVABLE_FIELDS}
    triple = complete_triple(data.gross, base["retention"], base["net"])
    recalculated = {"retention": triple.retention, "net": triple.net}
    # Un deducido negativo (bruto menor que la retención) no se guarda: la validación lo marcará.
    return {
        name: value
        for name in triple.derived
        if name in names and (value := recalculated[name]) is not None and value >= 0 and value != data.get(name)
    }
