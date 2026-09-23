"""Procesamiento de una carpeta de boletas: lectura, extracción, validación y registro.

Cada archivo se registra en su propia transacción: si el usuario cancela, lo
ya procesado queda guardado y el archivo en curso no deja rastros a medias.
El procesamiento nunca escribe informes; estos se generan a pedido desde la
base, de modo que un lote cancelado no produce totales parciales.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from receipt_reader.data import db, receipt_repo
from receipt_reader.data.documents import (
    IMAGE_EXTENSIONS,
    DocumentReading,
    ReadCancelledError,
    list_input_files,
    read_document,
    sha256_of,
)
from receipt_reader.data.ocr import OcrEngine, RapidOcrEngine
from receipt_reader.domain.classify import is_receipt_page
from receipt_reader.domain.drafts import build_draft, build_error_draft
from receipt_reader.domain.extraction import extract_receipt
from receipt_reader.domain.location import LocationHints, parse_location
from receipt_reader.domain.models import REVIEW_STATUSES, ReadStatus, ReceiptStatus, TextKind
from receipt_reader.domain.records import BatchInfo
from receipt_reader.domain.text import format_int, plural
from receipt_reader.domain.validation import Issue, ReceiptDraft, derive_status, validate_receipt
from receipt_reader.errors import AppError
from receipt_reader.services.context import CatalogSnapshot, Clock, duplicate_info, load_snapshot, system_clock

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class BatchResult:
    """Resumen de una corrida de procesamiento."""

    batch_id: int
    files_found: int
    files_processed: int
    files_skipped: int
    receipts_created: int
    status_counts: dict[ReceiptStatus, int] = field(default_factory=dict)
    cancelled: bool = False
    unreadable_files: tuple[str, ...] = ()

    @property
    def files_done(self) -> int:
        """Archivos recorridos: procesados más los que ya estaban registrados."""
        return self.files_processed + self.files_skipped

    @property
    def to_review(self) -> int:
        return sum(self.status_counts.get(status, 0) for status in REVIEW_STATUSES)

    def message(self) -> str:
        files = plural(self.files_found, "archivo procesado", "archivos procesados")
        parts = [
            f"{format_int(self.files_processed)} de {files}",
            plural(self.receipts_created, "boleta registrada", "boletas registradas"),
        ]
        if self.files_skipped:
            parts.append(plural(self.files_skipped, "archivo ya estaba registrado", "archivos ya estaban registrados"))
        if self.to_review:
            parts.append(plural(self.to_review, "requiere revisión", "requieren revisión"))
        text = ", ".join(parts) + "."
        return ("Procesamiento cancelado: " + text) if self.cancelled else text


@dataclass(frozen=True)
class PageAnalysis:
    """Resultado de analizar una página sin registrarla."""

    index: int
    text_kind: TextKind
    is_receipt: bool
    lines: tuple[str, ...]
    draft: ReceiptDraft | None
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class FileAnalysis:
    path: Path
    sha256: str
    page_count: int
    error: str | None
    pages: tuple[PageAnalysis, ...]

    @property
    def receipt_pages(self) -> tuple[PageAnalysis, ...]:
        return tuple(page for page in self.pages if page.is_receipt)


@dataclass(frozen=True)
class _PreparedReceipt:
    page_index: int
    text_kind: TextKind
    draft: ReceiptDraft


class ProcessingService:
    """Casos de uso de lectura de boletas."""

    def __init__(self, db_path: Path, *, ocr: OcrEngine | None = None, clock: Clock = system_clock) -> None:
        self.db_path = db_path
        self._ocr = ocr
        self._clock = clock

    @property
    def ocr(self) -> OcrEngine:
        if self._ocr is None:
            self._ocr = RapidOcrEngine()
        return self._ocr

    def _prepare(
        self, snapshot: CatalogSnapshot, reading: DocumentReading, hints: LocationHints
    ) -> list[_PreparedReceipt]:
        folder_program_id = snapshot.program_by_code.get(hints.program_code or "")
        if reading.error is not None:
            draft = build_error_draft(ReadStatus.UNREADABLE, hints=hints, folder_program_id=folder_program_id)
            return [_PreparedReceipt(0, TextKind.NONE, draft)]
        receipt_pages = [page for page in reading.pages if is_receipt_page(page.lines)]
        if not receipt_pages:
            kind = reading.pages[0].text_kind if reading.pages else TextKind.NONE
            draft = build_error_draft(ReadStatus.NO_RECEIPT, hints=hints, folder_program_id=folder_program_id)
            return [_PreparedReceipt(0, kind, draft)]
        prepared: list[_PreparedReceipt] = []
        for page in receipt_pages:
            extraction = extract_receipt(page.lines, organization_rut=snapshot.settings.organization_rut or None)
            draft = build_draft(
                extraction,
                text_kind=page.text_kind,
                hints=hints,
                folder_program_id=folder_program_id,
                matcher=snapshot.matcher,
                ocr_confidence=page.ocr_confidence,
            )
            prepared.append(_PreparedReceipt(page.index, page.text_kind, draft))
        return prepared

    def _register(
        self,
        conn: sqlite3.Connection,
        snapshot: CatalogSnapshot,
        batch_id: int,
        relative: Path,
        reading: DocumentReading,
    ) -> list[ReceiptStatus]:
        hints = parse_location(relative.parts)
        now = self._clock()
        original = receipt_repo.find_original_file(conn, reading.sha256) if reading.sha256 else None
        file_id = receipt_repo.insert_source_file(
            conn,
            batch_id=batch_id,
            sha256=reading.sha256 or sha256_of(str(reading.path).encode("utf-8")),
            path=str(reading.path),
            relative_path=relative.as_posix(),
            page_count=reading.page_count,
            read_error=reading.error,
            hints=hints,
            duplicate_of=original.id if original else None,
            processed_at=now,
        )
        statuses: list[ReceiptStatus] = []
        for prepared in self._prepare(snapshot, reading, hints):
            data = prepared.draft.data
            duplicates = duplicate_info(conn, None, data, original.relative_path if original else None)
            issues = validate_receipt(prepared.draft, snapshot.validation, duplicates)
            status = derive_status(
                issues, read_status=prepared.draft.read_status, edited=False, accepted=frozenset(), discarded=False
            )
            receipt_id = receipt_repo.insert_receipt(
                conn,
                source_file_id=file_id,
                page_index=prepared.page_index,
                text_kind=prepared.text_kind,
                read_status=prepared.draft.read_status,
                data=data,
                folder_program_id=prepared.draft.folder_program_id,
                text_program_id=prepared.draft.text_program_id,
                text_program_ambiguous=prepared.draft.text_program_ambiguous,
                ocr_confidence=prepared.draft.ocr_confidence,
                status=status,
                created_at=now,
            )
            receipt_repo.save_traces(conn, receipt_id, prepared.draft.traces)
            receipt_repo.replace_issues(conn, receipt_id, issues)
            statuses.append(status)
        return statuses

    def _read(
        self,
        path: Path,
        data: bytes,
        dpi: int,
        cancel: threading.Event | None,
        on_page: Callable[[int, int], None],
    ) -> DocumentReading:
        try:
            return read_document(path, ocr=self.ocr, dpi=dpi, cancel=cancel, on_page=on_page, data=data)
        except ReadCancelledError:
            raise
        except Exception as error:
            # Un archivo que hace fallar al lector no debe detener el lote: queda como ilegible.
            log.exception("Error inesperado al leer %s", path)
            return DocumentReading(path, sha256_of(data), 0, error=f"Error inesperado al leer el archivo ({error}).")

    def process_folder(
        self,
        folder: Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> BatchResult:
        """Procesa todos los archivos admitidos de la carpeta (recursivamente)."""
        root = folder.resolve()
        files = list_input_files(root)
        report = progress or (lambda _pct, _msg: None)
        created: Counter[ReceiptStatus] = Counter()
        processed = skipped = 0
        unreadable: list[str] = []
        cancelled = False
        with db.session(self.db_path) as conn:
            snapshot = load_snapshot(conn)
            with db.transaction(conn):
                batch_id = receipt_repo.start_batch(conn, str(root), self._clock(), len(files))
            for position, path in enumerate(files):
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                relative = path.relative_to(root)
                base_pct = int(position * 100 / max(len(files), 1))
                report(base_pct, f"Archivo {position + 1} de {len(files)}: {relative.as_posix()}")
                try:
                    data = path.read_bytes()
                except OSError as error:
                    log.warning("No se pudo abrir %s: %s", path, error)
                    data = None
                if data is not None and receipt_repo.file_already_registered(conn, sha256_of(data), str(path)):
                    skipped += 1
                    continue

                def on_page(page: int, total: int, position: int = position, relative: Path = relative) -> None:
                    pct = int((position + page / max(total, 1)) * 100 / max(len(files), 1))
                    name = relative.as_posix()
                    report(pct, f"Archivo {position + 1} de {len(files)}: {name} (página {page} de {total})")

                try:
                    if data is None:
                        reading = DocumentReading(
                            path, "", 0, error="No se pudo abrir el archivo (sin permiso o en uso)."
                        )
                    else:
                        reading = self._read(path, data, snapshot.settings.ocr_dpi, cancel, on_page)
                except ReadCancelledError:
                    cancelled = True
                    break
                if reading.error:
                    unreadable.append(relative.as_posix())
                with db.transaction(conn):
                    created.update(self._register(conn, snapshot, batch_id, relative, reading))
                processed += 1
            with db.transaction(conn):
                receipt_repo.finish_batch(
                    conn,
                    batch_id,
                    status="cancelled" if cancelled else "completed",
                    finished_at=self._clock(),
                    processed=processed,
                    skipped=skipped,
                )
        done = processed + skipped
        if cancelled:
            # La barra queda en lo realmente recorrido, no en 100 %.
            report(int(done * 100 / max(len(files), 1)), "Procesamiento cancelado")
        else:
            report(100, "Procesamiento terminado")
        result = BatchResult(
            batch_id=batch_id,
            files_found=len(files),
            files_processed=processed,
            files_skipped=skipped,
            receipts_created=sum(created.values()),
            status_counts=dict(created),
            cancelled=cancelled,
            unreadable_files=tuple(unreadable),
        )
        log.info("Lote %d: %s", batch_id, result.message())
        return result

    def analyze_file(self, path: Path, root: Path | None = None) -> FileAnalysis:
        """Lee y valida un archivo sin registrarlo (sirve para diagnosticar y para la autoprueba)."""
        relative = path.relative_to(root) if root is not None else Path(path.name)
        hints = parse_location(relative.parts)
        with db.session(self.db_path) as conn:
            snapshot = load_snapshot(conn)
        data = path.read_bytes()
        reading = self._read(path, data, snapshot.settings.ocr_dpi, None, lambda _page, _total: None)
        prepared = {item.page_index: item for item in self._prepare(snapshot, reading, hints)}
        pages: list[PageAnalysis] = []
        for page in reading.pages:
            item = prepared.get(page.index) if is_receipt_page(page.lines) else None
            draft = item.draft if item else None
            issues = validate_receipt(draft, snapshot.validation) if draft else ()
            pages.append(
                PageAnalysis(
                    page.index,
                    page.text_kind,
                    item is not None,
                    tuple(line.text for line in page.lines),
                    draft,
                    issues,
                )
            )
        return FileAnalysis(path, reading.sha256, reading.page_count, reading.error, tuple(pages))

    def check_ocr(self, folder: Path) -> list[str]:
        """Autoprueba del OCR: lee con el motor real una imagen de muestra y revisa los campos clave.

        Devuelve la lista de problemas (vacía si el OCR funciona). Sirve para confirmar que
        los modelos quedaron incluidos, por ejemplo en el ejecutable.
        """
        try:
            candidates = [path for path in list_input_files(folder) if path.suffix.lower() in IMAGE_EXTENSIONS]
        except AppError as error:
            return [error.user_message]
        if not candidates:
            return [f"No hay imágenes de muestra en {folder} para probar el OCR."]
        analysis = self.analyze_file(candidates[0], folder)
        name = candidates[0].name
        if analysis.error:
            return [f"El OCR no pudo leer {name}: {analysis.error}"]
        pages = analysis.receipt_pages
        if not pages or pages[0].draft is None:
            return [f"El OCR no encontró una boleta en {name}."]
        data = pages[0].draft.data
        missing = [label for label, value in (("RUT emisor", data.issuer_rut), ("bruto", data.gross)) if value is None]
        if missing:
            return [f"El OCR no leyó {', '.join(missing)} en {name}."]
        return []

    def last_batch(self) -> BatchInfo | None:
        with db.session(self.db_path) as conn:
            return receipt_repo.last_batch(conn)
