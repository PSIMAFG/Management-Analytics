"""Piezas compartidas por los servicios: catálogos vigentes, reloj y revalidación."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from receipt_reader.data import catalog_repo, receipt_repo
from receipt_reader.domain.models import Program, ReceiptData, ReceiptStatus, Settings
from receipt_reader.domain.programs import ProgramMatcher
from receipt_reader.domain.records import ReceiptRecord
from receipt_reader.domain.validation import (
    DuplicateInfo,
    Issue,
    ValidationContext,
    derive_status,
    validate_receipt,
)

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """Hora local sin microsegundos (las marcas de tiempo se guardan al segundo)."""
    return datetime.now().replace(microsecond=0)


@dataclass(frozen=True)
class CatalogSnapshot:
    """Catálogos y parámetros leídos una vez para validar un conjunto de boletas."""

    settings: Settings
    programs: tuple[Program, ...]
    validation: ValidationContext
    matcher: ProgramMatcher
    program_by_code: dict[str, int]
    program_names: dict[int, str]


def load_snapshot(conn: sqlite3.Connection) -> CatalogSnapshot:
    """Catálogos vigentes. Los programas desactivados siguen nombrando boletas antiguas,
    pero ya no se ofrecen para nuevas carpetas ni alias (`program_by_code` y `matcher`
    solo usan los activos)."""
    settings = catalog_repo.load_settings(conn)
    programs = tuple(catalog_repo.list_programs(conn))
    active_programs = tuple(program for program in programs if program.active)
    names = {program.id: program.name for program in programs}
    rates = {(rate.program_id, rate.year): rate for rate in catalog_repo.list_reference_rates(conn)}
    validation = ValidationContext(
        settings=settings,
        retention=catalog_repo.load_retention_table(conn),
        reference_rates=rates,
        program_names=names,
    )
    return CatalogSnapshot(
        settings=settings,
        programs=programs,
        validation=validation,
        matcher=ProgramMatcher(catalog_repo.list_aliases(conn, active_only=True)),
        program_by_code={program.folder_code: program.id for program in active_programs},
        program_names=names,
    )


def duplicate_info(
    conn: sqlite3.Connection,
    receipt_id: int | None,
    data: ReceiptData,
    original_file: str | None,
    *,
    self_valid: bool = False,
) -> DuplicateInfo:
    """Regla única de duplicados, para el primer registro (`receipt_id` None) y para revalidar.

    `original_file` es la ruta del archivo idéntico ya registrado, si lo hay. Un folio repetido
    con la misma fecha y el mismo bruto se informa como probable reenvío.
    """
    peer = receipt_repo.folio_peer(conn, receipt_id, data.issuer_rut, data.folio, self_valid=self_valid)
    if peer is None:
        return DuplicateInfo(original_file=original_file)
    same = peer.issue_date == data.issue_date and peer.gross == data.gross
    return DuplicateInfo(
        original_file=original_file,
        folio_peer_id=peer.id,
        folio_peer_same_content=same,
        folio_peer_file=peer.relative_path,
    )


def duplicates_for(conn: sqlite3.Connection, record: ReceiptRecord) -> DuplicateInfo:
    """Duplicados de una boleta registrada: copia exacta de archivo y folio repetido."""
    original_file = None
    if record.file.duplicate_of is not None:
        original_file = receipt_repo.get_source_file(conn, record.file.duplicate_of).relative_path
    return duplicate_info(conn, record.id, record.data, original_file, self_valid=record.status.is_valid)


@dataclass(frozen=True)
class Revalidation:
    record: ReceiptRecord
    issues: tuple[Issue, ...]
    status: ReceiptStatus


def evaluate(conn: sqlite3.Connection, snapshot: CatalogSnapshot, record: ReceiptRecord) -> Revalidation:
    issues = validate_receipt(record.to_draft(), snapshot.validation, duplicates_for(conn, record))
    status = derive_status(
        issues,
        read_status=record.read_status,
        edited=record.edited,
        accepted=record.accepted_codes,
        discarded=record.status is ReceiptStatus.DISCARDED,
    )
    return Revalidation(record, issues, status)


def revalidate(
    conn: sqlite3.Connection, snapshot: CatalogSnapshot, receipt_ids: Iterable[int], now: datetime
) -> list[Revalidation]:
    """Recalcula incidencias y estado de las boletas indicadas.

    Primero se escriben las que dejan de ser válidas y después las que pasan a
    serlo, para respetar la restricción de folio único entre boletas válidas.
    """
    results = [evaluate(conn, snapshot, receipt_repo.load_receipt(conn, rid)) for rid in dict.fromkeys(receipt_ids)]
    for result in sorted(results, key=lambda item: item.status.is_valid):
        receipt_repo.replace_issues(conn, result.record.id, result.issues)
        if result.status is not result.record.status:
            receipt_repo.set_status(conn, result.record.id, result.status, now)
    return results
