"""Modelos del dominio: enumeraciones y registros inmutables."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from receipt_reader.domain.dates import Period
from receipt_reader.domain.rut import format_rut
from receipt_reader.domain.text import format_percent_bp, format_thousands

# Ventana de fechas por defecto cuando no hay una carpeta de mes que la acote: amplia a
# propósito (desde 2018) para no bloquear boletas antiguas legítimas; el límite superior
# se calcula al definir la clase, así que sigue siendo amplio con el paso del tiempo.
DEFAULT_WINDOW_START = date(2018, 1, 1)
DEFAULT_WINDOW_MARGIN_DAYS = 60


def _default_window_end() -> date:
    return date.today() + timedelta(days=DEFAULT_WINDOW_MARGIN_DAYS)


class FieldSource(StrEnum):
    """Origen de un valor: de dónde salió el dato que se guarda."""

    NATIVE = "native"
    OCR = "ocr"
    FOLDER = "folder"
    FILENAME = "filename"
    DERIVED = "derived"
    USER = "user"

    @property
    def label(self) -> str:
        return _SOURCE_LABELS[self]


_SOURCE_LABELS = {
    FieldSource.NATIVE: "Texto nativo",
    FieldSource.OCR: "OCR",
    FieldSource.FOLDER: "Carpeta",
    FieldSource.FILENAME: "Nombre de archivo",
    FieldSource.DERIVED: "Deducido",
    FieldSource.USER: "Usuario",
}


class TextKind(StrEnum):
    """Cómo se obtuvo el texto de una página."""

    NATIVE = "native"
    OCR = "ocr"
    NONE = "none"

    @property
    def label(self) -> str:
        return {"native": "Texto nativo", "ocr": "OCR", "none": "Sin texto"}[self.value]


class ReadStatus(StrEnum):
    """Resultado de la lectura de la página asociada a una boleta."""

    OK = "ok"
    NO_RECEIPT = "no_receipt"
    UNREADABLE = "unreadable"


class ReceiptStatus(StrEnum):
    """Estado persistente de una boleta."""

    PENDING = "pending"
    APPROVED = "approved"
    CORRECTED = "corrected"
    DISCARDED = "discarded"
    ERROR = "error"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]

    @property
    def is_valid(self) -> bool:
        """Cuenta en los totales: aprobada por las validaciones o corregida por el usuario."""
        return self in (ReceiptStatus.APPROVED, ReceiptStatus.CORRECTED)


_STATUS_LABELS = {
    ReceiptStatus.PENDING: "Pendiente",
    ReceiptStatus.APPROVED: "Aprobada",
    ReceiptStatus.CORRECTED: "Corregida",
    ReceiptStatus.DISCARDED: "Descartada",
    ReceiptStatus.ERROR: "Error de lectura",
}
# Cuentan en los totales.
VALID_STATUSES = frozenset({ReceiptStatus.APPROVED, ReceiptStatus.CORRECTED})
# Esperan una decisión del usuario (forman la cola de revisión).
REVIEW_STATUSES = frozenset({ReceiptStatus.PENDING, ReceiptStatus.ERROR})


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"

    @property
    def label(self) -> str:
        return "Bloqueante" if self is Severity.BLOCKING else "Advertencia"


class WorkdayType(StrEnum):
    """Tipo de jornada declarado en la glosa."""

    WEEKLY = "semanal"
    MONTHLY = "mensual"

    @property
    def label(self) -> str:
        return "Semanal" if self is WorkdayType.WEEKLY else "Mensual"

    @property
    def weeks_factor(self) -> int:
        """Semanas por mes que se usan para pasar de horas semanales a mensuales."""
        return 4 if self is WorkdayType.WEEKLY else 1


class PeriodAxis(StrEnum):
    """Eje del período para agrupar informes."""

    SERVICE = "service"
    ISSUE = "issue"
    PAYMENT = "payment"

    @property
    def label(self) -> str:
        return {"service": "Período de servicio", "issue": "Mes de emisión", "payment": "Período de pago"}[self.value]


@dataclass(frozen=True)
class Decree:
    """Decreto que respalda el pago, identificado por número y año."""

    number: int
    year: int | None = None

    def label(self) -> str:
        return f"{self.number}/{self.year}" if self.year else str(self.number)


@dataclass(frozen=True)
class FieldTrace:
    """Trazabilidad de un campo: valor leído (texto), confianza y origen."""

    value: str | None
    confidence: float | None
    source: FieldSource


@dataclass(frozen=True)
class ReceiptData:
    """Campos de negocio de una boleta. Todos opcionales: la validación decide qué falta."""

    issuer_rut: str | None = None
    issuer_name: str | None = None
    receiver_rut: str | None = None
    receiver_name: str | None = None
    folio: int | None = None
    issue_date: date | None = None
    service_period: Period | None = None
    payment_period: Period | None = None
    gross: int | None = None
    retention: int | None = None
    net: int | None = None
    printed_rate_bp: int | None = None
    program_id: int | None = None
    hours: Decimal | None = None
    workday_type: WorkdayType | None = None
    decree: Decree | None = None
    gloss: str | None = None

    def get(self, name: str) -> Any:
        return getattr(self, name)

    def with_changes(self, **changes: Any) -> ReceiptData:
        return dataclasses.replace(self, **changes)


RECEIPT_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(ReceiptData))

FIELD_LABELS: dict[str, str] = {
    "issuer_rut": "RUT emisor",
    "issuer_name": "Nombre del prestador",
    "receiver_rut": "RUT receptor",
    "receiver_name": "Receptor",
    "folio": "Folio",
    "issue_date": "Fecha de emisión",
    "service_period": "Período de servicio",
    "payment_period": "Período de pago",
    "gross": "Monto bruto",
    "retention": "Retención",
    "net": "Líquido",
    "printed_rate_bp": "Tasa impresa",
    "program_id": "Programa",
    "hours": "Horas",
    "workday_type": "Tipo de jornada",
    "decree": "Decreto",
    "gloss": "Glosa",
    "status": "Estado",
}


def value_to_text(value: Any) -> str | None:
    """Representación estable de un valor para trazas y auditoría."""
    if value is None:
        return None
    if isinstance(value, Decree):
        return value.label()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Period):
        return value.iso()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, StrEnum):
        return value.value
    return str(value)


def display_value(name: str, value: Any) -> str:
    """Valor legible para mensajes y listados (formato chileno)."""
    if value is None:
        return "-"
    if name in ("issuer_rut", "receiver_rut"):
        return format_rut(value)
    if name in ("gross", "retention", "net"):
        return "$ " + format_thousands(value)
    if name == "printed_rate_bp":
        return format_percent_bp(value)
    if name == "issue_date" and isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, Period):
        return value.label()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f").replace(".", ",")
    if isinstance(value, WorkdayType):
        return value.label
    if isinstance(value, Decree):
        return value.label()
    return str(value)


@dataclass(frozen=True)
class Program:
    """Programa que financia los pagos; su código de 3 dígitos se usa en las carpetas.

    Un programa inactivo no se ofrece para carpetas o alias nuevos, pero las boletas
    que ya lo tienen asignado lo conservan (nunca se borra ni se reasigna solo).
    """

    id: int
    folder_code: str
    name: str
    short_name: str
    active: bool = True


@dataclass(frozen=True)
class ProgramAlias:
    """Texto que identifica un programa en la glosa. Menor prioridad = gana antes."""

    program_id: int
    alias: str
    priority: int


@dataclass(frozen=True)
class ReferenceRate:
    """Rango de referencia del valor hora de un programa en un año."""

    program_id: int
    year: int
    min_hourly: int
    max_hourly: int


@dataclass(frozen=True)
class Provider:
    """Prestador con nombre canónico confirmado por el usuario."""

    rut: str
    canonical_name: str
    confirmed_at: datetime


@dataclass(frozen=True)
class Settings:
    """Parámetros de validación y de la organización receptora.

    La fecha de emisión se valida contra el mes de la carpeta de pago: desde
    `window_months_before` meses antes hasta `window_months_after` meses
    después. La ventana fija (`date_window_start` a `date_window_end`) solo
    se usa para los archivos que no están en una carpeta de mes.
    """

    organization_rut: str = ""
    organization_name: str = "Organización Ejemplo"
    date_window_start: date = DEFAULT_WINDOW_START
    date_window_end: date = field(default_factory=_default_window_end)
    window_months_before: int = 3
    window_months_after: int = 1
    amount_min: int = 30_000
    amount_max: int = 5_000_000
    retention_tolerance: int = 1
    folder_month_tolerance: int = 1
    ocr_min_confidence: float = 0.85
    ocr_dpi: int = 200
