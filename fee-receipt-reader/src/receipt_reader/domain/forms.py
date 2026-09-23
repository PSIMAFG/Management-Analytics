"""Validadores del formulario de revisión y de los parámetros.

Convierte el texto que escribe el usuario en valores del dominio con las
mismas reglas que se aplican al leer las boletas (RUT con DV, montos en
formato chileno, fechas día/mes/año, horas con decimales, período aaaa-mm).
`validate_settings` concentra las reglas de los parámetros de validación:
la usan el servicio al guardar y el diálogo mientras se escribe.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from receipt_reader.domain.dates import Period, parse_date
from receipt_reader.domain.models import Decree, Settings, WorkdayType
from receipt_reader.domain.money import parse_clp, parse_decimal, parse_percent_bp
from receipt_reader.domain.rut import format_rut, is_valid_rut, parse_rut
from receipt_reader.domain.text import collapse_spaces, fold, format_thousands, normalize_text
from receipt_reader.errors import FieldFormatError, FormError

EDITABLE_FIELDS: tuple[str, ...] = (
    "issuer_rut",
    "issuer_name",
    "receiver_rut",
    "receiver_name",
    "folio",
    "issue_date",
    "service_period",
    "payment_period",
    "gross",
    "retention",
    "net",
    "printed_rate_bp",
    "program_id",
    "hours",
    "workday_type",
    "decree",
    "gloss",
)
_DECREE_TEXT = re.compile(r"(\d{1,5})(?:\s*[/-]\s*(\d{4}))?")
FOLDER_CODE_RE = re.compile(r"\d{3}")
# Folio: solo dígitos, o miles con grupos de 3 ('1.234'); '12.5' es ambiguo y se rechaza.
_FOLIO_TEXT = re.compile(r"\d+|\d{1,3}(?:\.\d{3})+")
MINUTES_PER_HOUR = 60
MAX_WINDOW_MONTHS = 24
MIN_OCR_DPI = 100
MAX_OCR_DPI = 400


def _parse_name(text: str) -> str:
    name = collapse_spaces(text)
    if sum(char.isalpha() for char in name) < 3:
        raise FieldFormatError("El nombre debe tener al menos 3 letras.")
    return name


def _parse_folio(text: str) -> int:
    cleaned = normalize_text(text).strip()
    if not _FOLIO_TEXT.fullmatch(cleaned) or int(cleaned.replace(".", "")) <= 0:
        raise FieldFormatError(f"'{cleaned}' no es un folio válido (número entero positivo, por ejemplo 1234).")
    return int(cleaned.replace(".", ""))


def _parse_rate(text: str) -> int:
    value = parse_percent_bp(text)
    if not 0 < value <= 5000:
        raise FieldFormatError("La tasa debe estar entre 0 y 50 %.")
    return value


def _parse_program(text: str) -> int:
    if not text.strip().isdigit():
        raise FieldFormatError("Seleccione un programa del catálogo.")
    return int(text)


def _parse_hours(text: str) -> Decimal:
    """Horas con hasta 2 decimales que correspondan a minutos enteros (se guardan en minutos).

    '22,5' (22 h 30 min) y '7,25' (7 h 15 min) se aceptan; '7,99' no, porque se guardaría
    redondeado y lo guardado no coincidiría con lo escrito.
    """
    value = parse_decimal(text)
    if not Decimal(0) < value <= Decimal(200):
        raise FieldFormatError("Las horas deben ser mayores que 0 y no superar 200.")
    if (value * MINUTES_PER_HOUR) % 1:
        raise FieldFormatError(
            f"'{normalize_text(text).strip()}' no corresponde a minutos enteros: use por ejemplo 22, 22,5 o 7,25."
        )
    return value


def _parse_workday(text: str) -> WorkdayType:
    folded = fold(text.strip())
    if folded.startswith("SEMANAL"):
        return WorkdayType.WEEKLY
    if folded.startswith("MENSUAL"):
        return WorkdayType.MONTHLY
    raise FieldFormatError("El tipo de jornada debe ser semanal o mensual.")


def _parse_decree(text: str) -> Decree:
    match = _DECREE_TEXT.fullmatch(normalize_text(text).strip())
    if match is None:
        raise FieldFormatError("El decreto se escribe como número/año, por ejemplo 1234/2025.")
    number = int(match.group(1))
    year = int(match.group(2)) if match.group(2) else None
    if number <= 0 or (year is not None and not 2000 <= year <= 2100):
        raise FieldFormatError("El decreto debe tener un número positivo y un año entre 2000 y 2100.")
    return Decree(number, year)


def _parse_gloss(text: str) -> str:
    lines = [collapse_spaces(line) for line in normalize_text(text).splitlines()]
    return "\n".join(line for line in lines if line)


_PARSERS: dict[str, Callable[[str], Any]] = {
    "issuer_rut": parse_rut,
    "issuer_name": _parse_name,
    "receiver_rut": parse_rut,
    "receiver_name": _parse_name,
    "folio": _parse_folio,
    "issue_date": parse_date,
    "service_period": Period.parse,
    "payment_period": Period.parse,
    "gross": parse_clp,
    "retention": parse_clp,
    "net": parse_clp,
    "printed_rate_bp": _parse_rate,
    "program_id": _parse_program,
    "hours": _parse_hours,
    "workday_type": _parse_workday,
    "decree": _parse_decree,
    "gloss": _parse_gloss,
}


def parse_field(name: str, text: str | None) -> Any:
    """Valor del dominio para un campo del formulario; texto vacío significa sin valor."""
    if name not in _PARSERS:
        raise FieldFormatError(f"El campo '{name}' no se puede editar.")
    if text is None or not text.strip():
        return None
    return _PARSERS[name](text)


def parse_receipt_form(values: Mapping[str, str | None]) -> dict[str, Any]:
    """Interpreta todos los campos recibidos; si alguno falla, informa todos los errores juntos."""
    parsed: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, text in values.items():
        try:
            parsed[name] = parse_field(name, text)
        except FieldFormatError as error:
            errors[name] = error.user_message
    if errors:
        raise FormError(errors)
    return parsed


def validate_settings(settings: Settings) -> None:
    """Revisa los parámetros de validación; si algo no cuadra, informa todos los campos juntos."""
    errors: dict[str, str] = {}
    if sum(char.isalpha() for char in settings.organization_name) < 3:
        errors["organization_name"] = "Escriba el nombre de la organización."
    if settings.organization_rut and not is_valid_rut(settings.organization_rut):
        errors["organization_rut"] = "El RUT de la organización no es válido."
    if settings.date_window_start > settings.date_window_end:
        errors["date_window_end"] = "La ventana fija de fechas termina antes de empezar."
    for name in ("window_months_before", "window_months_after"):
        if not 0 <= getattr(settings, name) <= MAX_WINDOW_MONTHS:
            errors[name] = f"Los meses de la ventana deben estar entre 0 y {MAX_WINDOW_MONTHS}."
    if settings.amount_min < 0:
        errors["amount_min"] = "El monto mínimo no puede ser negativo."
    elif settings.amount_min > settings.amount_max:
        errors["amount_max"] = "El monto mínimo no puede superar al máximo."
    if settings.retention_tolerance < 0:
        errors["retention_tolerance"] = "La tolerancia de la retención no puede ser negativa."
    if not 0 <= settings.folder_month_tolerance <= MAX_WINDOW_MONTHS:
        errors["folder_month_tolerance"] = f"La tolerancia del mes debe estar entre 0 y {MAX_WINDOW_MONTHS} meses."
    if not 0 <= settings.ocr_min_confidence <= 1:
        errors["ocr_min_confidence"] = "La confianza mínima del OCR debe estar entre 0 y 100 %."
    if not MIN_OCR_DPI <= settings.ocr_dpi <= MAX_OCR_DPI:
        errors["ocr_dpi"] = f"La resolución del OCR debe estar entre {MIN_OCR_DPI} y {MAX_OCR_DPI} ppp."
    if errors:
        raise FormError(errors)


def validate_program_fields(folder_code: str, name: str, short_name: str) -> None:
    """Reglas de un programa del catálogo: código de carpeta de 3 dígitos y nombres con sentido."""
    errors: dict[str, str] = {}
    if not FOLDER_CODE_RE.fullmatch(folder_code.strip()):
        errors["folder_code"] = "El código de carpeta debe tener exactamente 3 dígitos, por ejemplo 110."
    if sum(char.isalpha() for char in name) < 3:
        errors["name"] = "Escriba el nombre del programa."
    if sum(char.isalpha() for char in short_name) < 2:
        errors["short_name"] = "Escriba el nombre corto del programa."
    if errors:
        raise FormError(errors)


def validate_alias_fields(alias: str, priority: int) -> None:
    """Reglas de un alias: texto no vacío y prioridad no negativa (menor número gana)."""
    errors: dict[str, str] = {}
    if not collapse_spaces(alias):
        errors["alias"] = "Escriba el texto del alias tal como aparece en la glosa."
    if priority < 0:
        errors["priority"] = "La prioridad no puede ser negativa."
    if errors:
        raise FormError(errors)


def form_text(name: str, value: Any) -> str:
    """Texto con que se muestra un valor en el formulario (inverso de `parse_field`)."""
    if value is None:
        return ""
    if name in ("issuer_rut", "receiver_rut"):
        return format_rut(value)
    if name in ("gross", "retention", "net"):
        return format_thousands(value)
    if name == "printed_rate_bp":
        return f"{value // 100},{value % 100:02d}"
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, Period):
        return value.iso()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f").replace(".", ",")
    if isinstance(value, WorkdayType):
        return value.value
    if isinstance(value, Decree):
        return value.label()
    return str(value)
