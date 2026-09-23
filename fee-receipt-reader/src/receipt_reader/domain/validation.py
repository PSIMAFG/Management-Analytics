"""Validación de boletas con incidencias tipadas y derivación del estado.

Cada regla produce una incidencia con código, campo, severidad y un mensaje
en español. Las bloqueantes dejan la boleta pendiente; las advertencias solo
informan (por ejemplo, que la fecha de emisión cae fuera de la ventana
configurada: nunca bloquea, solo avisa para que se revise si corresponde).
Algunas bloqueantes describen una situación que una persona puede aceptar
después de revisarla (monto fuera de rango, mes distinto al de la carpeta,
programa de la carpeta distinto del texto, un dato leído con baja
confianza): son "aceptables" y el botón Aprobar las da por revisadas. La
aprobación guarda qué códigos se aceptaron; si después aparece otra
incidencia aceptable (por ejemplo, al cambiar los parámetros), la boleta
vuelve a la cola. Las demás (faltan datos, montos que no cuadran,
duplicados) exigen corregir o descartar.

La validación nunca modifica los datos: no corrige ni inventa montos.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import (
    FIELD_LABELS,
    FieldSource,
    FieldTrace,
    ReadStatus,
    ReceiptData,
    ReceiptStatus,
    ReferenceRate,
    Settings,
    Severity,
)
from receipt_reader.domain.retention import RetentionTable, expected_retention, implied_hourly_rate, reference_year
from receipt_reader.domain.rut import format_rut, is_valid_rut
from receipt_reader.domain.text import format_percent_bp, format_thousands

# Campos cuya lectura por OCR con baja confianza manda la boleta a revisión: identifican la
# boleta (RUT y folio forman la clave única) o determinan los montos y el año de la tasa.
KEY_OCR_FIELDS: tuple[str, ...] = ("issuer_rut", "folio", "issue_date", "gross", "retention", "net")


class IssueCode(StrEnum):
    UNREADABLE = "UNREADABLE"
    NOT_A_RECEIPT = "NOT_A_RECEIPT"
    ISSUER_RUT_MISSING = "ISSUER_RUT_MISSING"
    ISSUER_RUT_INVALID = "ISSUER_RUT_INVALID"
    ISSUER_IS_RECEIVER = "ISSUER_IS_RECEIVER"
    ISSUER_NAME_MISSING = "ISSUER_NAME_MISSING"
    RECEIVER_RUT_MISSING = "RECEIVER_RUT_MISSING"
    RECEIVER_MISMATCH = "RECEIVER_MISMATCH"
    FOLIO_MISSING = "FOLIO_MISSING"
    FOLIO_FILENAME_MISMATCH = "FOLIO_FILENAME_MISMATCH"
    DATE_MISSING = "DATE_MISSING"
    DATE_OUT_OF_WINDOW = "DATE_OUT_OF_WINDOW"
    DATE_FOLDER_MISMATCH = "DATE_FOLDER_MISMATCH"
    SERVICE_PERIOD_MISSING = "SERVICE_PERIOD_MISSING"
    SERVICE_AFTER_ISSUE = "SERVICE_AFTER_ISSUE"
    GROSS_MISSING = "GROSS_MISSING"
    AMOUNT_OUT_OF_RANGE = "AMOUNT_OUT_OF_RANGE"
    TRIPLE_INCOMPLETE = "TRIPLE_INCOMPLETE"
    TRIPLE_DERIVED = "TRIPLE_DERIVED"
    NET_MISMATCH = "NET_MISMATCH"
    RETENTION_MISMATCH = "RETENTION_MISMATCH"
    RATE_MISSING = "RATE_MISSING"
    RATE_MISMATCH = "RATE_MISMATCH"
    RATE_UNKNOWN_YEAR = "RATE_UNKNOWN_YEAR"
    PROGRAM_MISSING = "PROGRAM_MISSING"
    PROGRAM_CONFLICT = "PROGRAM_CONFLICT"
    FOLDER_PROGRAM_UNKNOWN = "FOLDER_PROGRAM_UNKNOWN"
    HOURLY_RATE_OUT_OF_RANGE = "HOURLY_RATE_OUT_OF_RANGE"
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    FIELD_LOW_CONFIDENCE = "FIELD_LOW_CONFIDENCE"
    DUPLICATE_FILE = "DUPLICATE_FILE"
    DUPLICATE_FOLIO = "DUPLICATE_FOLIO"


@dataclass(frozen=True)
class IssueSpec:
    """Definición de una incidencia: título corto, severidad, si se puede aceptar y campo."""

    title: str
    severity: Severity
    overridable: bool = False
    field: str | None = None


B = Severity.BLOCKING
W = Severity.WARNING
ISSUE_CATALOG: dict[IssueCode, IssueSpec] = {
    IssueCode.UNREADABLE: IssueSpec("Archivo ilegible", B),
    IssueCode.NOT_A_RECEIPT: IssueSpec("Sin página de boleta", B),
    IssueCode.ISSUER_RUT_MISSING: IssueSpec("Falta el RUT del emisor", B, field="issuer_rut"),
    IssueCode.ISSUER_RUT_INVALID: IssueSpec("RUT del emisor inválido", B, field="issuer_rut"),
    IssueCode.ISSUER_IS_RECEIVER: IssueSpec("Emisor igual al receptor", B, field="issuer_rut"),
    IssueCode.ISSUER_NAME_MISSING: IssueSpec("Falta el nombre del prestador", W, field="issuer_name"),
    IssueCode.RECEIVER_RUT_MISSING: IssueSpec("Falta el RUT del receptor", W, field="receiver_rut"),
    IssueCode.RECEIVER_MISMATCH: IssueSpec("Receptor distinto de la organización", W, field="receiver_rut"),
    IssueCode.FOLIO_MISSING: IssueSpec("Falta el folio", B, field="folio"),
    IssueCode.FOLIO_FILENAME_MISMATCH: IssueSpec(
        "Folio distinto al del nombre del archivo", B, overridable=True, field="folio"
    ),
    IssueCode.DATE_MISSING: IssueSpec("Falta la fecha de emisión", B, field="issue_date"),
    # La ventana de fechas es un parámetro configurable, no una regla de negocio estricta: una
    # fecha fuera de ella solo se advierte, nunca bloquea la aprobación.
    IssueCode.DATE_OUT_OF_WINDOW: IssueSpec("Fecha fuera de la ventana", W, field="issue_date"),
    IssueCode.DATE_FOLDER_MISMATCH: IssueSpec("Mes distinto al de la carpeta", B, overridable=True, field="issue_date"),
    IssueCode.SERVICE_PERIOD_MISSING: IssueSpec(
        "Falta el período de servicio", B, overridable=True, field="service_period"
    ),
    IssueCode.SERVICE_AFTER_ISSUE: IssueSpec("Servicio posterior a la emisión", W, field="service_period"),
    IssueCode.GROSS_MISSING: IssueSpec("Falta el monto bruto", B, field="gross"),
    IssueCode.AMOUNT_OUT_OF_RANGE: IssueSpec("Monto fuera de rango", B, overridable=True, field="gross"),
    IssueCode.TRIPLE_INCOMPLETE: IssueSpec("Faltan retención y líquido", B, field="retention"),
    IssueCode.TRIPLE_DERIVED: IssueSpec("Monto deducido", W, field="net"),
    IssueCode.NET_MISMATCH: IssueSpec("Líquido no cuadra", B, field="net"),
    IssueCode.RETENTION_MISMATCH: IssueSpec("Retención no cuadra", B, field="retention"),
    IssueCode.RATE_MISSING: IssueSpec("Falta la tasa impresa", W, field="printed_rate_bp"),
    IssueCode.RATE_MISMATCH: IssueSpec("Tasa de otro año", B, field="printed_rate_bp"),
    IssueCode.RATE_UNKNOWN_YEAR: IssueSpec("Año sin tasa legal", W, field="issue_date"),
    IssueCode.PROGRAM_MISSING: IssueSpec("Falta el programa", B, field="program_id"),
    IssueCode.PROGRAM_CONFLICT: IssueSpec(
        "Programa de carpeta y texto distintos", B, overridable=True, field="program_id"
    ),
    IssueCode.FOLDER_PROGRAM_UNKNOWN: IssueSpec("Código de carpeta desconocido", W, field="program_id"),
    IssueCode.HOURLY_RATE_OUT_OF_RANGE: IssueSpec("Valor hora fuera de referencia", W, field="gross"),
    IssueCode.LOW_OCR_CONFIDENCE: IssueSpec("Baja confianza de OCR", W),
    IssueCode.FIELD_LOW_CONFIDENCE: IssueSpec("Dato leído con baja confianza", B, overridable=True),
    IssueCode.DUPLICATE_FILE: IssueSpec("Archivo duplicado", B),
    IssueCode.DUPLICATE_FOLIO: IssueSpec("Folio duplicado", B, field="folio"),
}


@dataclass(frozen=True)
class Issue:
    """Incidencia de una boleta."""

    code: IssueCode
    message: str
    severity: Severity
    overridable: bool
    field: str | None

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCKING

    @property
    def title(self) -> str:
        return ISSUE_CATALOG[self.code].title


def make_issue(code: IssueCode, message: str, field_name: str | None = None) -> Issue:
    """Incidencia del catálogo; `field_name` reemplaza el campo cuando el código aplica a varios."""
    spec = ISSUE_CATALOG[code]
    return Issue(code, message, spec.severity, spec.overridable, field_name or spec.field)


@dataclass(frozen=True)
class DuplicateInfo:
    """Duplicados detectados contra otras boletas registradas.

    `folio_peer_id` es el ID interno de la otra boleta con el mismo emisor y folio, y
    `folio_peer_file` la ruta de su archivo.
    """

    original_file: str | None = None
    folio_peer_id: int | None = None
    folio_peer_same_content: bool | None = None
    folio_peer_file: str | None = None


@dataclass(frozen=True)
class ReceiptDraft:
    """Boleta a validar: datos, trazas por campo y contexto de su lectura."""

    data: ReceiptData
    traces: Mapping[str, FieldTrace] = field(default_factory=dict)
    read_status: ReadStatus = ReadStatus.OK
    folder_program_id: int | None = None
    folder_program_code: str | None = None
    text_program_id: int | None = None
    text_program_ambiguous: bool = False
    ocr_confidence: float | None = None
    edited: bool = False
    folio_hint: int | None = None

    def source(self, name: str) -> FieldSource | None:
        trace = self.traces.get(name)
        return None if trace is None else trace.source

    def raw(self, name: str) -> str | None:
        trace = self.traces.get(name)
        return None if trace is None else trace.value


@dataclass(frozen=True)
class ValidationContext:
    """Parámetros y catálogos vigentes para validar."""

    settings: Settings
    retention: RetentionTable
    reference_rates: Mapping[tuple[int, int], ReferenceRate] = field(default_factory=dict)
    program_names: Mapping[int, str] = field(default_factory=dict)


def _money(value: int) -> str:
    return "$ " + format_thousands(value)


def _read_issues(draft: ReceiptDraft) -> tuple[Issue, ...]:
    if draft.read_status is ReadStatus.UNREADABLE:
        return (make_issue(IssueCode.UNREADABLE, "El archivo no se pudo leer. Revise el original o descártelo."),)
    return (
        make_issue(
            IssueCode.NOT_A_RECEIPT,
            "No se encontró ninguna página con las etiquetas de una boleta de honorarios.",
        ),
    )


def _unreadable_hint(draft: ReceiptDraft, name: str) -> str:
    raw = draft.raw(name)
    return f" Se leyó '{raw}', que no es un valor válido." if raw else ""


def _identity_issues(draft: ReceiptDraft, settings: Settings) -> list[Issue]:
    data = draft.data
    issues: list[Issue] = []
    if not data.issuer_rut:
        issues.append(
            make_issue(IssueCode.ISSUER_RUT_MISSING, "No se encontró el RUT del emisor en el encabezado de la boleta.")
        )
    elif not is_valid_rut(data.issuer_rut):
        issues.append(
            make_issue(
                IssueCode.ISSUER_RUT_INVALID,
                f"El dígito verificador del RUT del emisor {format_rut(data.issuer_rut)} no es válido.",
            )
        )
    receiver_ruts = {r for r in (data.receiver_rut, settings.organization_rut) if r}
    if data.issuer_rut and data.issuer_rut in receiver_ruts:
        issues.append(
            make_issue(
                IssueCode.ISSUER_IS_RECEIVER,
                f"El RUT del emisor {format_rut(data.issuer_rut)} es el del receptor; revise el encabezado.",
            )
        )
    if settings.organization_rut:
        if not data.receiver_rut:
            issues.append(make_issue(IssueCode.RECEIVER_RUT_MISSING, "No se encontró el RUT del receptor."))
        elif data.receiver_rut != settings.organization_rut:
            name = f" ({data.receiver_name})" if data.receiver_name else ""
            issues.append(
                make_issue(
                    IssueCode.RECEIVER_MISMATCH,
                    f"La boleta está dirigida a {format_rut(data.receiver_rut)}{name} y no a "
                    f"{settings.organization_name} ({format_rut(settings.organization_rut)}).",
                )
            )
    if not data.issuer_name:
        issues.append(make_issue(IssueCode.ISSUER_NAME_MISSING, "No se encontró el nombre del prestador."))
    if data.folio is None:
        issues.append(
            make_issue(
                IssueCode.FOLIO_MISSING,
                "No se encontró el folio junto a 'BOLETA DE HONORARIOS ELECTRÓNICA N°'."
                + _unreadable_hint(draft, "folio"),
            )
        )
    elif (
        draft.folio_hint is not None
        and data.folio != draft.folio_hint
        and draft.source("folio") is not FieldSource.USER
    ):
        issues.append(
            make_issue(
                IssueCode.FOLIO_FILENAME_MISMATCH,
                f"Se leyó el folio {data.folio}, pero el nombre del archivo indica el {draft.folio_hint}. "
                "Compárelo con la imagen: el folio es parte de la clave que detecta duplicados.",
            )
        )
    return issues


def issue_date_window(payment_period: Period | None, settings: Settings) -> tuple[date, date]:
    """Fechas de emisión aceptadas: alrededor del mes de la carpeta o, sin carpeta, la ventana fija."""
    if payment_period is None:
        return settings.date_window_start, settings.date_window_end
    start = payment_period.shift(-settings.window_months_before).first_day()
    end = payment_period.shift(settings.window_months_after).last_day()
    return start, end


def _date_issues(draft: ReceiptDraft, settings: Settings) -> list[Issue]:
    data = draft.data
    issues: list[Issue] = []
    if data.issue_date is None:
        issues.append(
            make_issue(
                IssueCode.DATE_MISSING, "No se encontró la fecha de emisión." + _unreadable_hint(draft, "issue_date")
            )
        )
    else:
        start, end = issue_date_window(data.payment_period, settings)
        window = (
            f"la ventana de la carpeta de {data.payment_period.label()}"
            if data.payment_period is not None
            else "la ventana fija para archivos sin carpeta de mes"
        )
        if not start <= data.issue_date <= end:
            issues.append(
                make_issue(
                    IssueCode.DATE_OUT_OF_WINDOW,
                    f"La fecha de emisión {data.issue_date:%d-%m-%Y} está fuera de {window} "
                    f"({start:%d-%m-%Y} a {end:%d-%m-%Y}). Revise si se leyó bien o si es una boleta atrasada.",
                )
            )
        elif data.payment_period is not None:
            distance = abs(Period.of(data.issue_date).months_until(data.payment_period))
            if distance > settings.folder_month_tolerance:
                issues.append(
                    make_issue(
                        IssueCode.DATE_FOLDER_MISMATCH,
                        f"La boleta se emitió en {Period.of(data.issue_date).label()} y está en la carpeta de "
                        f"{data.payment_period.label()}. Confirme la fecha contra la imagen.",
                    )
                )
    if data.service_period is None:
        issues.append(
            make_issue(
                IssueCode.SERVICE_PERIOD_MISSING,
                "La glosa no indica el mes del servicio ('MES DE ...'). Use la sugerencia o escriba el período; "
                "si aprueba sin período, la boleta queda en 'Sin período' en los informes por servicio.",
            )
        )
    elif data.issue_date is not None and data.service_period > Period.of(data.issue_date):
        issues.append(
            make_issue(
                IssueCode.SERVICE_AFTER_ISSUE,
                f"El período de servicio ({data.service_period.label()}) es posterior a la emisión.",
            )
        )
    return issues


def _amount_issues(draft: ReceiptDraft, ctx: ValidationContext) -> list[Issue]:
    data = draft.data
    settings = ctx.settings
    issues: list[Issue] = []
    legal = ctx.retention.rate_for(data.issue_date.year) if data.issue_date else None
    if data.issue_date is not None and legal is None:
        issues.append(
            make_issue(
                IssueCode.RATE_UNKNOWN_YEAR,
                f"No hay tasa de retención registrada para {data.issue_date.year}; no se pudo verificar la retención.",
            )
        )
    if data.gross is None:
        issues.append(
            make_issue(
                IssueCode.GROSS_MISSING,
                "No se leyó el monto bruto ('Total Honorarios'). Nunca se calcula a partir de otros datos."
                + _unreadable_hint(draft, "gross"),
            )
        )
    else:
        if not settings.amount_min <= data.gross <= settings.amount_max:
            issues.append(
                make_issue(
                    IssueCode.AMOUNT_OUT_OF_RANGE,
                    f"El bruto {_money(data.gross)} está fuera del rango esperado "
                    f"({_money(settings.amount_min)} a {_money(settings.amount_max)}).",
                )
            )
        if data.retention is None and data.net is None:
            issues.append(
                make_issue(
                    IssueCode.TRIPLE_INCOMPLETE,
                    "No se leyeron la retención ni el líquido: el bruto no se puede verificar.",
                )
            )
        derived = [name for name in ("retention", "net") if draft.source(name) is FieldSource.DERIVED]
        if derived:
            names = " y ".join("la retención" if name == "retention" else "el líquido" for name in derived)
            issues.append(
                make_issue(
                    IssueCode.TRIPLE_DERIVED,
                    f"No se pudo leer {names}; se dedujo de la identidad bruto - retención = líquido.",
                )
            )
        if data.retention is not None and data.net is not None and data.gross - data.retention != data.net:
            issues.append(
                make_issue(
                    IssueCode.NET_MISMATCH,
                    f"Bruto {_money(data.gross)} menos retención {_money(data.retention)} no da el líquido "
                    f"{_money(data.net)}.",
                )
            )
        if legal is not None and data.retention is not None:
            expected = expected_retention(data.gross, legal)
            if abs(data.retention - expected) > settings.retention_tolerance:
                issues.append(
                    make_issue(
                        IssueCode.RETENTION_MISMATCH,
                        f"La retención {_money(data.retention)} no corresponde al {format_percent_bp(legal)} de "
                        f"{data.issue_date.year if data.issue_date else ''} (se esperaba {_money(expected)}).",
                    )
                )
    if data.printed_rate_bp is None:
        issues.append(
            make_issue(
                IssueCode.RATE_MISSING,
                "No se leyó la tasa de retención impresa." + _unreadable_hint(draft, "printed_rate_bp"),
            )
        )
    elif legal is not None and data.printed_rate_bp != legal and data.issue_date is not None:
        issues.append(
            make_issue(
                IssueCode.RATE_MISMATCH,
                f"La tasa impresa ({format_percent_bp(data.printed_rate_bp)}) no es la tasa legal de "
                f"{data.issue_date.year} ({format_percent_bp(legal)}).",
            )
        )
    return issues


def _program_issues(draft: ReceiptDraft, ctx: ValidationContext) -> list[Issue]:
    data = draft.data
    issues: list[Issue] = []
    names = ctx.program_names
    if data.program_id is None:
        detail = " El texto menciona más de un programa." if draft.text_program_ambiguous else ""
        issues.append(
            make_issue(
                IssueCode.PROGRAM_MISSING,
                "No se pudo determinar el programa ni por la carpeta ni por la glosa." + detail,
            )
        )
    elif (
        draft.source("program_id") is not FieldSource.USER
        and draft.folder_program_id is not None
        and draft.text_program_id is not None
        and draft.folder_program_id != draft.text_program_id
    ):
        issues.append(
            make_issue(
                IssueCode.PROGRAM_CONFLICT,
                f"La carpeta indica '{names.get(draft.folder_program_id, draft.folder_program_id)}' y la glosa "
                f"menciona '{names.get(draft.text_program_id, draft.text_program_id)}'. Confirme el programa.",
            )
        )
    if draft.folder_program_code and draft.folder_program_id is None:
        issues.append(
            make_issue(
                IssueCode.FOLDER_PROGRAM_UNKNOWN,
                f"El código de carpeta {draft.folder_program_code} no existe en el catálogo de programas.",
            )
        )
    return issues


def _hourly_issues(draft: ReceiptDraft, ctx: ValidationContext) -> list[Issue]:
    data = draft.data
    factor = data.workday_type.weeks_factor if data.workday_type else None
    hourly = implied_hourly_rate(data.gross, data.hours, factor)
    if hourly is None or data.program_id is None:
        return []
    year = reference_year(data.service_period, data.issue_date)
    if year is None:
        return []
    reference = ctx.reference_rates.get((data.program_id, year))
    if reference is None or reference.min_hourly <= hourly <= reference.max_hourly:
        return []
    return [
        make_issue(
            IssueCode.HOURLY_RATE_OUT_OF_RANGE,
            f"El valor hora implícito ({_money(hourly)}) está fuera de la referencia del programa para "
            f"{year} ({_money(reference.min_hourly)} a {_money(reference.max_hourly)}). Solo es un aviso: "
            "el monto no se modifica.",
        )
    ]


def _confidence_issues(draft: ReceiptDraft, settings: Settings) -> list[Issue]:
    """Confianza del OCR: la media de la página (aviso) y la de cada campo clave (a revisión).

    La media de la página puede ser alta aunque un campo se haya leído mal (por ejemplo,
    'N° 333' leído como 'N33'); por eso la decisión se toma con la confianza de cada campo.
    """
    issues: list[Issue] = []
    threshold = settings.ocr_min_confidence
    if draft.ocr_confidence is not None and draft.ocr_confidence < threshold:
        issues.append(
            make_issue(
                IssueCode.LOW_OCR_CONFIDENCE,
                f"La confianza media del OCR es {draft.ocr_confidence:.0%}; revise los valores contra la imagen.",
            )
        )
    for name in KEY_OCR_FIELDS:
        trace = draft.traces.get(name)
        if trace is None or trace.source is not FieldSource.OCR or trace.confidence is None:
            continue
        if draft.data.get(name) is None or trace.confidence >= threshold:
            continue
        issues.append(
            make_issue(
                IssueCode.FIELD_LOW_CONFIDENCE,
                f"{FIELD_LABELS[name]}: el OCR leyó '{trace.value}' con una confianza de {trace.confidence:.0%}. "
                "Compárelo con la imagen antes de aprobar.",
                name,
            )
        )
    return issues


def _duplicate_issues(duplicates: DuplicateInfo, folio: int | None) -> list[Issue]:
    issues: list[Issue] = []
    if duplicates.original_file:
        issues.append(
            make_issue(
                IssueCode.DUPLICATE_FILE,
                f"El archivo es idéntico a '{duplicates.original_file}', que ya está registrado.",
            )
        )
    if duplicates.folio_peer_id is not None:
        detail = (
            "Tiene la misma fecha y el mismo monto: probablemente es un reenvío."
            if duplicates.folio_peer_same_content
            else "La fecha o el monto difieren: revise si el folio se leyó bien."
        )
        folio_text = f"con el folio {folio}" if folio is not None else "con el mismo folio"
        file_text = f" (archivo {duplicates.folio_peer_file})" if duplicates.folio_peer_file else ""
        issues.append(
            make_issue(
                IssueCode.DUPLICATE_FOLIO,
                f"Ya existe el registro ID {duplicates.folio_peer_id} del mismo emisor {folio_text}{file_text}. "
                f"{detail}",
            )
        )
    return issues


def validate_receipt(
    draft: ReceiptDraft, ctx: ValidationContext, duplicates: DuplicateInfo | None = None
) -> tuple[Issue, ...]:
    """Todas las incidencias de una boleta, en un orden estable."""
    if draft.read_status is not ReadStatus.OK and not draft.edited:
        return _read_issues(draft)
    issues: list[Issue] = []
    issues += _identity_issues(draft, ctx.settings)
    issues += _date_issues(draft, ctx.settings)
    issues += _amount_issues(draft, ctx)
    issues += _program_issues(draft, ctx)
    issues += _hourly_issues(draft, ctx)
    issues += _confidence_issues(draft, ctx.settings)
    issues += _duplicate_issues(duplicates or DuplicateInfo(), draft.data.folio)
    return tuple(issues)


def acceptance_blockers(issues: Iterable[Issue]) -> list[Issue]:
    """Incidencias bloqueantes que impiden aprobar aunque el usuario lo pida."""
    return [issue for issue in issues if issue.blocking and not issue.overridable]


def acceptable_codes(issues: Iterable[Issue]) -> frozenset[IssueCode]:
    """Códigos que el usuario da por revisados al aprobar: los de las bloqueantes aceptables."""
    return frozenset(issue.code for issue in issues if issue.blocking and issue.overridable)


def derive_status(
    issues: Sequence[Issue],
    *,
    read_status: ReadStatus,
    edited: bool,
    accepted: Collection[IssueCode],
    discarded: bool,
) -> ReceiptStatus:
    """Estado que corresponde a una boleta según sus incidencias y las acciones del usuario.

    `accepted` son los códigos que el usuario aceptó al aprobar. Solo esas incidencias se
    dan por revisadas: una bloqueante aceptable que aparece después deja la boleta pendiente.
    """
    if discarded:
        return ReceiptStatus.DISCARDED
    if read_status is not ReadStatus.OK and not edited:
        return ReceiptStatus.ERROR
    unresolved = [issue for issue in issues if issue.blocking and not (issue.overridable and issue.code in accepted)]
    if not unresolved:
        return ReceiptStatus.CORRECTED if edited else ReceiptStatus.APPROVED
    return ReceiptStatus.PENDING
