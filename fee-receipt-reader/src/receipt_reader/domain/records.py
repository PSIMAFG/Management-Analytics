"""Registros persistidos tal como los entrega la capa de datos a los servicios y a la interfaz."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePath

from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import FieldTrace, ReadStatus, ReceiptData, ReceiptStatus, TextKind, WorkdayType
from receipt_reader.domain.retention import implied_hourly_rate
from receipt_reader.domain.validation import Issue, IssueCode, ReceiptDraft


@dataclass(frozen=True)
class SourceFileInfo:
    """Archivo de origen de una o más boletas."""

    id: int
    sha256: str
    path: str
    relative_path: str
    page_count: int
    read_error: str | None = None
    payment_period: Period | None = None
    folder_program_code: str | None = None
    folio_hint: int | None = None
    service_month_hint: int | None = None
    duplicate_of: int | None = None
    processed_at: datetime | None = None

    @property
    def file_name(self) -> str:
        return PurePath(self.path).name


@dataclass(frozen=True)
class ReceiptRecord:
    """Boleta registrada con sus trazas, incidencias y estado."""

    id: int
    file: SourceFileInfo
    page_index: int
    read_status: ReadStatus
    text_kind: TextKind
    data: ReceiptData
    status: ReceiptStatus
    traces: Mapping[str, FieldTrace] = field(default_factory=dict)
    issues: tuple[Issue, ...] = ()
    folder_program_id: int | None = None
    text_program_id: int | None = None
    text_program_ambiguous: bool = False
    ocr_confidence: float | None = None
    discard_reason: str | None = None
    accepted_at: datetime | None = None
    accepted_codes: frozenset[IssueCode] = frozenset()
    edited: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def blocking_issues(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def hourly_rate(self) -> int | None:
        factor = self.data.workday_type.weeks_factor if self.data.workday_type else None
        return implied_hourly_rate(self.data.gross, self.data.hours, factor)

    def to_draft(self) -> ReceiptDraft:
        return ReceiptDraft(
            data=self.data,
            traces=dict(self.traces),
            read_status=self.read_status,
            folder_program_id=self.folder_program_id,
            folder_program_code=self.file.folder_program_code,
            text_program_id=self.text_program_id,
            text_program_ambiguous=self.text_program_ambiguous,
            ocr_confidence=self.ocr_confidence,
            edited=self.edited,
            folio_hint=self.file.folio_hint,
        )


@dataclass(frozen=True)
class FolioPeer:
    """Otra boleta registrada con el mismo emisor y folio (para detectar duplicados)."""

    id: int
    issue_date: date | None
    gross: int | None
    relative_path: str


@dataclass(frozen=True)
class ReceiptRow:
    """Fila liviana para tablas y exportaciones."""

    id: int
    status: ReceiptStatus
    file_name: str
    relative_path: str
    page_index: int
    text_kind: TextKind
    issuer_rut: str | None
    issuer_name: str | None
    receiver_rut: str | None
    folio: int | None
    issue_date: date | None
    service_period: Period | None
    payment_period: Period | None
    program_id: int | None
    program_name: str | None
    gross: int | None
    retention: int | None
    net: int | None
    printed_rate_bp: int | None
    hours: Decimal | None
    workday_type: WorkdayType | None
    decree_label: str | None
    ocr_confidence: float | None
    discard_reason: str | None
    issue_codes: tuple[str, ...]
    issue_titles: tuple[str, ...]
    blocking_count: int
    warning_count: int
    sha256: str
    hourly_rate: int | None

    @property
    def issues_text(self) -> str:
        return "; ".join(self.issue_titles)


@dataclass(frozen=True)
class Correction:
    """Registro de auditoría de un cambio hecho por el usuario."""

    receipt_id: int
    field: str
    old_value: str | None
    new_value: str | None
    corrected_at: datetime


@dataclass(frozen=True)
class RejectedRow:
    """Fila de una planilla importada que no se guardó, con su número y el motivo."""

    row_number: int
    reason: str


@dataclass(frozen=True)
class BatchInfo:
    """Corrida de procesamiento de una carpeta."""

    id: int
    root_path: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    files_found: int
    files_processed: int
    files_skipped: int
