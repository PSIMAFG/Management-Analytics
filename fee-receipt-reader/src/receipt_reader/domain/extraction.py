"""Extracción determinista de los campos de una boleta a partir de sus líneas.

Cada campo se busca en su bloque del documento:

- Encabezado del emisor (antes de 'Señor(es)'): nombre, RUT del emisor y folio
  anclado a 'BOLETA DE HONORARIOS ELECTRONICA N°' (si el OCR deforma el
  encabezado, a la línea 'ELECTRONICA').
- Bloque del receptor ('Señor(es): ... Rut: ...'): RUT y nombre del receptor.
- 'Fecha:' con la fecha de emisión en palabras; como respaldo, 'Fecha / Hora Emisión'.
- Glosa: desde la etiqueta de la prestación (`SERVICE_PATTERN`) hasta 'Total Honorarios',
  conservando las líneas. De ella salen el período de servicio, las horas, la jornada y el decreto.
- Totales: 'Total Honorarios' (bruto), '<tasa> % Impto. Retenido' (retención y tasa
  impresa) y 'Total:' (líquido).

Ningún valor se inventa: si una etiqueta aparece pero su valor no se puede leer
sin ambigüedad, el campo queda vacío y se conserva el texto crudo para la revisión.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from receipt_reader.domain.classify import FEES_PATTERN, SERVICE_PATTERN
from receipt_reader.domain.dates import Period, find_long_date, find_numeric_date, infer_year
from receipt_reader.domain.layout import TextLine
from receipt_reader.domain.models import Decree, WorkdayType
from receipt_reader.domain.money import parse_decimal, parse_percent_bp, try_parse_clp
from receipt_reader.domain.rut import RutMatch, find_ruts
from receipt_reader.domain.text import MONTH_PATTERN, collapse_spaces, fold, month_from_name
from receipt_reader.errors import FieldFormatError

HEADER_RE = re.compile(r"BOLETA\s+DE\s+" + FEES_PATTERN)
ELECTRONIC_RE = re.compile(r"ELECTRONICA")
RECEIVER_RE = re.compile(r"SENOR\s*\(?\s*ES\s*\)?\s*:?")
SERVICE_RE = re.compile(SERVICE_PATTERN + r"\s*:?")
GROSS_LABEL_RE = re.compile(r"TOTAL\s+" + FEES_PATTERN)
RETENTION_LABEL_RE = re.compile(r"RETENID[OA]")
NET_LABEL_RE = re.compile(r"(?<![A-Z])TOTAL\s*:")

# El monto es el token completo que sigue a la etiqueta (sin la puntuación final, como en
# '684.000.-') y se interpreta entero: '80O.000' (una O leída en vez de un cero) no se toma
# como 80, y '800 000' (un número partido por el OCR) tampoco se toma como 800. En esos casos
# el campo queda vacío y se conserva el texto crudo para la revisión.
MONEY = r"(\d\S*?)[.,;:-]*(?!\S)(?!\s+\d)"
GROSS_RE = re.compile(r"TOTAL\s+" + FEES_PATTERN + r"\s*:?\s*\$?\s*:?\s*" + MONEY)
RETENTION_RE = re.compile(r"RETENID[OA]\s*:?\s*\$?\s*:?\s*" + MONEY)
RATE_RE = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
NET_RE = re.compile(r"(?<![A-Z])TOTAL\s*:\s*\$?\s*:?\s*" + MONEY)
LONE_MONEY_RE = re.compile(r"\$?\s*:?\s*" + MONEY)
TRAILING_MONEY_RE = re.compile(r"\s+\$?\s*\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?$")

RUT_ANCHOR_RE = re.compile(r"\bRUT\s*:?\s*$")
FOLIO_RE = re.compile(r"(?<![A-Z0-9])N\s*(?:[°º]|O(?=\s*\d)|\.)?\s*\.?\s*[:#]?\s*(\d{1,10})(?!\d)")
FOLIO_EXCLUDED_RE = re.compile(
    r"\bRES\b|\bEX\b|\bCALLE\b|\bAVENIDA\b|\bAV\.|\bPASAJE\b|\bPJE\b|\bDOMICILIO\b|\bDEPTO\b"
)
DATE_LABEL_RE = re.compile(r"\bFECHA\s*:")
EMISSION_LABEL_RE = re.compile(r"FECHA\s*/\s*HORA\s+EMISION\s*:?")

SERVICE_MONTH_RE = re.compile(
    r"\bMES(?:ES)?\s+(?:DE\s+)?(" + MONTH_PATTERN + r")\b(?:[\s,/-]+(?:DE\s+|DEL\s+)?(\d{4})(?!\d))?"
)
MONTH_YEAR_RE = re.compile(r"\b(" + MONTH_PATTERN + r")\s+(?:DE\s+|DEL\s+)?(\d{4})(?!\d)")
FULL_DATE_PREFIX_RE = re.compile(r"\d{1,2}\s+DE\s+$")
HOURS_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:HORAS?|HRS?)(?![A-Z])\.?")
WORKDAY_AFTER_RE = re.compile(r"\s*(?:(SEMANAL(?:ES)?|A\s+LA\s+SEMANA)|(MENSUAL(?:ES)?|AL\s+MES))\b")
WEEKLY_RE = re.compile(r"\bSEMANAL(?:ES)?\b|\bA\s+LA\s+SEMANA\b")
MONTHLY_RE = re.compile(r"\bMENSUAL(?:ES)?\b|\bAL\s+MES\b")
DECREE_RE = re.compile(
    r"(?<![A-Z])(?:D\s*\.?\s*A\s*\.?|DECRETO(?:\s+ALCALDICIO)?|DCTO\s*\.?)\s*(?:EXENTO\s+)?"
    r"(?:N\s*[°º]?\s*\.?\s*|NRO\s*\.?\s*|NUMERO\s+)?[:#]?\s*(\d{1,5})(?!\d)"
    # Año del decreto: '1234/2025', 'DE 12.01.25', 'DE 12 DE ENERO DE 2026' o 'DE 2025'. Un número
    # suelto de dos dígitos después de 'DE' es ambiguo (puede ser el día) y no se toma como año.
    r"(?:\s*[/-]\s*(\d{4}|\d{2})(?![\d/.-])"
    r"|\s+(?:DE|DEL)\s+(?:\d{1,2}[./-]\d{1,2}[./-](\d{4}|\d{2})(?!\d)"
    r"|\d{1,2}\s+DE\s+(?:" + MONTH_PATTERN + r")\s+(?:DE|DEL)\s+(\d{4})(?!\d)"
    r"|(\d{4})(?!\d)))?"
)
HEADER_NOISE_RE = re.compile(
    r"BOLETA\s+DE\s+"
    + FEES_PATTERN
    + r"|ELECTRONICA|(?<![A-Z0-9])N\s*[°º]\s*\.?\s*\d+|\bRUT\b.*$|\bTELEFONO\b.*$|\bGIRO\b.*$"
)
NAME_WORD_RE = re.compile(r"[A-ZÁÉÍÓÚÜÑa-záéíóúüñ][A-ZÁÉÍÓÚÜÑa-záéíóúüñ'\u00b4.-]*")
NAME_STOPWORDS = frozenset(
    [
        "BOLETA",
        "HONORARIOS",
        "ELECTRONICA",
        "RUT",
        "FECHA",
        "TELEFONO",
        "FONO",
        "DOMICILIO",
        "DIRECCION",
        "GIRO",
        "SENOR",
        "SENORES",
        "TOTAL",
        "IMPTO",
        "RETENIDO",
        "PROFESIONAL",
        "PROFESIONALES",
        "SERVICIO",
        "SERVICIOS",
        "ASESORIA",
        "ASESORIAS",
        "CONSULTORIA",
        "CONSULTORIAS",
        "PSICOLOGO",
        "PSICOLOGA",
        "TRABAJADOR",
        "TRABAJADORA",
        "SOCIAL",
        "TERAPEUTA",
        "OCUPACIONAL",
        "FONOAUDIOLOGO",
        "FONOAUDIOLOGA",
        "EDUCADOR",
        "EDUCADORA",
        "KINESIOLOGO",
        "KINESIOLOGA",
        "ENFERMERO",
        "ENFERMERA",
        "MEDICO",
        "MEDICA",
        "NUTRICIONISTA",
        "ACTIVIDADES",
        "CALLE",
        "AVENIDA",
        "PASAJE",
        "COMUNA",
        "CIUDAD",
        "REGION",
        "SII",
        "DOCUMENTO",
        "ORGANIZACION",
        "EMPRESA",
        "LIMITADA",
        "LTDA",
        "SPA",
        "OTRAS",
        "OTROS",
        "PROGRAMA",
        "ATENCION",
    ]
)
MAX_GLOSS_LINES = 8
MIN_HOURS = Decimal(0)
MAX_HOURS = Decimal(200)


@dataclass(frozen=True)
class Extracted:
    """Valor extraído con el texto crudo del que proviene y la confianza de su línea.

    `value` es None cuando la etiqueta existe pero el valor no se pudo interpretar.
    """

    value: Any
    raw: str | None
    confidence: float


@dataclass(frozen=True)
class PageExtraction:
    """Resultado de extraer una página de boleta."""

    fields: dict[str, Extracted] = field(default_factory=dict)
    program_text: str = ""
    notes: tuple[str, ...] = ()

    def value(self, name: str) -> Any:
        found = self.fields.get(name)
        return None if found is None else found.value


def _find_index(folded: Sequence[str], pattern: re.Pattern[str], start: int = 0, stop: int | None = None) -> int | None:
    end = len(folded) if stop is None else min(stop, len(folded))
    for index in range(max(start, 0), end):
        if pattern.search(folded[index]):
            return index
    return None


def _money_after(
    lines: Sequence[TextLine],
    folded: Sequence[str],
    index: int,
    pattern: re.Pattern[str],
    label: re.Pattern[str],
) -> Extracted | None:
    """Monto que sigue a una etiqueta en la misma línea o, si no está, en la línea siguiente.

    Si después de la etiqueta hay texto que no es un monto legible, se devuelve ese texto
    crudo sin valor: el campo queda vacío y la revisión muestra lo que se leyó.
    """
    line = lines[index]
    match = pattern.search(folded[index])
    if match is not None:
        raw = line.text[match.start(1) : match.end(1)]
        return Extracted(try_parse_clp(raw), raw, line.confidence)
    label_match = label.search(folded[index])
    tail = collapse_spaces(line.text[label_match.end() :].strip(" :$")) if label_match else ""
    if tail:
        return Extracted(None, tail, line.confidence)
    if index + 1 < len(lines):
        following = lines[index + 1]
        lone = LONE_MONEY_RE.fullmatch(folded[index + 1].strip())
        if lone is not None:
            raw = lone.group(1)
            return Extracted(try_parse_clp(raw), raw, following.confidence)
    return None


def _pick_rut(candidates: list[tuple[RutMatch, bool, float]], exclude: set[str]) -> tuple[RutMatch, float] | None:
    usable = [c for c in candidates if c[0].rut not in exclude]
    if not usable:
        return None
    # Primero los anclados a 'RUT:', luego los de DV válido; a igualdad, el primero en aparecer.
    ordered = sorted(enumerate(usable), key=lambda item: (not item[1][1], not item[1][0].valid, item[0]))
    match, _anchored, confidence = ordered[0][1]
    return match, confidence


def _rut_candidates(
    lines: Sequence[TextLine], folded: Sequence[str], start: int, stop: int
) -> list[tuple[RutMatch, bool, float]]:
    found: list[tuple[RutMatch, bool, float]] = []
    for index in range(max(start, 0), min(stop, len(lines))):
        for match in find_ruts(lines[index].text):
            anchored = RUT_ANCHOR_RE.search(folded[index][: match.start]) is not None
            found.append((match, anchored, lines[index].confidence))
    return found


def _rut_line_index(lines: Sequence[TextLine], rut: str, stop: int) -> int | None:
    for index in range(min(stop, len(lines))):
        if any(match.rut == rut for match in find_ruts(lines[index].text)):
            return index
    return None


def looks_like_person_name(text: str) -> bool:
    """Nombre de persona plausible: 2 a 6 palabras alfabéticas y ninguna palabra de etiqueta.

    Las palabras de rechazo se comparan como palabras completas, nunca como
    subcadenas (un apellido que contiene 'por' o 'rut' sigue siendo válido).
    """
    words = text.split()
    if not 2 <= len(words) <= 6:
        return False
    if not all(NAME_WORD_RE.fullmatch(word) for word in words):
        return False
    if sum(char.isalpha() for char in text) < 6:
        return False
    return not any(fold(word).strip(".'\u00b4-") in NAME_STOPWORDS for word in words)


def _strip_header_noise(line: TextLine, folded_line: str) -> str:
    keep = [True] * len(line.text)
    for match in HEADER_NOISE_RE.finditer(folded_line):
        for position in range(match.start(), match.end()):
            keep[position] = False
    kept = "".join(char for char, flag in zip(line.text, keep, strict=True) if flag)
    return collapse_spaces(kept.strip(" :-,."))


def _extract_name(lines: Sequence[TextLine], folded: Sequence[str], stop: int) -> Extracted | None:
    for index in range(min(stop, len(lines))):
        candidate = _strip_header_noise(lines[index], folded[index])
        if looks_like_person_name(candidate):
            return Extracted(candidate, candidate, lines[index].confidence)
    return None


def _extract_folio(
    lines: Sequence[TextLine], folded: Sequence[str], header_index: int | None, stop: int
) -> Extracted | None:
    """Folio en las líneas que siguen al encabezado.

    Si el OCR deformó el encabezado, el ancla es la línea 'ELECTRONICA' del mismo
    recuadro; sin ninguna de las dos no se busca un 'N°' suelto (podría ser un teléfono).
    """
    anchor = header_index if header_index is not None else _find_index(folded, ELECTRONIC_RE, 0, stop)
    if anchor is None:
        return None
    window = range(anchor, min(anchor + 4, max(stop, anchor + 1), len(lines)))

    def found(index: int, match: re.Match[str]) -> Extracted:
        raw = match.group(1)
        return Extracted(int(raw) if int(raw) > 0 else None, raw, lines[index].confidence)

    # 1) En la línea del encabezado o de 'ELECTRONICA'.
    for index in window:
        if HEADER_RE.search(folded[index]) or ELECTRONIC_RE.search(folded[index]):
            match = FOLIO_RE.search(folded[index])
            if match is not None:
                return found(index, match)
    # 2) Una línea que comienza con 'N°'.
    for index in window:
        offset = len(folded[index]) - len(folded[index].lstrip())
        match = FOLIO_RE.match(folded[index], offset)
        if match is not None:
            return found(index, match)
    # 3) Cualquier 'N°' del bloque que no esté en una dirección ni en la resolución del SII.
    for index in window:
        if FOLIO_EXCLUDED_RE.search(folded[index]):
            continue
        match = FOLIO_RE.search(folded[index])
        if match is not None:
            return found(index, match)
    return None


def _extract_date(lines: Sequence[TextLine], folded: Sequence[str]) -> Extracted | None:
    for index, text in enumerate(folded):
        match = DATE_LABEL_RE.search(text)
        if match is None or "HORA" in text:
            continue
        tail = lines[index].text[match.end() :]
        value = find_long_date(tail) or find_numeric_date(tail)
        if value is not None:
            return Extracted(value, collapse_spaces(tail), lines[index].confidence)
    for index, text in enumerate(folded):
        match = EMISSION_LABEL_RE.search(text)
        if match is None:
            continue
        tail = lines[index].text[match.end() :]
        value = find_numeric_date(tail)
        if value is not None:
            return Extracted(value, collapse_spaces(tail), lines[index].confidence * 0.95)
    return None


def _extract_gloss(
    lines: Sequence[TextLine], folded: Sequence[str], service_index: int | None, gross_index: int | None
) -> tuple[str | None, float]:
    if service_index is None:
        return None, 0.0
    if gross_index is not None and gross_index > service_index:
        end = gross_index
    else:
        end = min(len(lines), service_index + 1 + MAX_GLOSS_LINES)
    label = SERVICE_RE.search(folded[service_index])
    first = lines[service_index].text[label.end() :] if label is not None else ""
    parts: list[str] = []
    confidences: list[float] = []
    candidates = [(first, lines[service_index].confidence)] + [
        (line.text, line.confidence) for line in lines[service_index + 1 : end]
    ]
    for text, confidence in candidates:
        cleaned = TRAILING_MONEY_RE.sub("", text).strip()
        if not cleaned or LONE_MONEY_RE.fullmatch(cleaned):
            continue
        parts.append(cleaned)
        confidences.append(confidence)
    if not parts:
        return None, 0.0
    return "\n".join(parts), sum(confidences) / len(confidences)


def _extract_service_period(gloss_folded: str, issue_date: date | None) -> tuple[Period | None, str | None]:
    match = SERVICE_MONTH_RE.search(gloss_folded)
    if match is not None:
        month = month_from_name(match.group(1))
        year = int(match.group(2)) if match.group(2) else None
        raw = collapse_spaces(match.group(0))
    else:
        month = year = None
        raw = None
        for candidate in MONTH_YEAR_RE.finditer(gloss_folded):
            if FULL_DATE_PREFIX_RE.search(gloss_folded[: candidate.start()]):
                continue  # es parte de una fecha completa (por ejemplo, la del decreto)
            month = month_from_name(candidate.group(1))
            year = int(candidate.group(2))
            raw = collapse_spaces(candidate.group(0))
            break
    if month is None:
        return None, None
    if year is None:
        if issue_date is None:
            return None, raw
        year = infer_year(month, issue_date)
    try:
        return Period(year, month), raw
    except FieldFormatError:
        return None, raw


def _extract_hours(gloss_folded: str) -> tuple[Decimal | None, WorkdayType | None, str | None]:
    chosen: tuple[Decimal, WorkdayType | None, str] | None = None
    for match in HOURS_RE.finditer(gloss_folded):
        try:
            hours = parse_decimal(match.group(1))
        except FieldFormatError:
            continue
        if not MIN_HOURS < hours <= MAX_HOURS:
            continue
        workday: WorkdayType | None = None
        after = WORKDAY_AFTER_RE.match(gloss_folded, match.end())
        if after is not None:
            workday = WorkdayType.WEEKLY if after.group(1) else WorkdayType.MONTHLY
        candidate = (hours, workday, collapse_spaces(match.group(0) + (after.group(0) if after else "")))
        if workday is not None:
            chosen = candidate
            break
        if chosen is None:
            chosen = candidate
    if chosen is None:
        return None, _workday_anywhere(gloss_folded), None
    hours, workday, raw = chosen
    return hours, workday or _workday_anywhere(gloss_folded), raw


def _workday_anywhere(gloss_folded: str) -> WorkdayType | None:
    weekly = WEEKLY_RE.search(gloss_folded) is not None
    monthly = MONTHLY_RE.search(gloss_folded) is not None
    if weekly == monthly:
        return None
    return WorkdayType.WEEKLY if weekly else WorkdayType.MONTHLY


def find_decree(folded_text: str) -> tuple[Decree | None, str | None]:
    """Decreto como (número, año).

    Un número entre 2000 y 2100 sin año a continuación se descarta porque es el año
    ('DECRETO 2025'); con año explícito es un número de decreto ('DECRETO N° 2050/2025').
    """
    for match in DECREE_RE.finditer(folded_text):
        number = int(match.group(1))
        year_text = next((group for group in match.groups()[1:] if group), None)
        if number == 0 or (2000 <= number <= 2100 and not year_text):
            continue
        year: int | None = None
        if year_text:
            year = int(year_text) + (2000 if len(year_text) == 2 else 0)
            if not 2000 <= year <= 2100:
                year = None
        return Decree(number, year), collapse_spaces(match.group(0))
    return None, None


def _extract_amounts(lines: Sequence[TextLine], folded: Sequence[str], fields: dict[str, Extracted]) -> int | None:
    gross_index = _find_index(folded, GROSS_LABEL_RE)
    if gross_index is not None:
        gross = _money_after(lines, folded, gross_index, GROSS_RE, GROSS_LABEL_RE)
        if gross is not None:
            fields["gross"] = gross
    start = gross_index or 0
    retention_index = _find_index(folded, RETENTION_LABEL_RE, start)
    if retention_index is not None:
        retention = _money_after(lines, folded, retention_index, RETENTION_RE, RETENTION_LABEL_RE)
        if retention is not None:
            fields["retention"] = retention
        rate = RATE_RE.search(folded[retention_index])
        if rate is not None:
            raw = rate.group(1)
            try:
                bp: int | None = parse_percent_bp(raw)
            except FieldFormatError:
                bp = None
            if bp is not None and not 0 < bp <= 5000:
                bp = None
            fields["printed_rate_bp"] = Extracted(bp, raw + " %", lines[retention_index].confidence)
    net_start = (retention_index + 1) if retention_index is not None else start
    for index in range(net_start, len(lines)):
        if GROSS_LABEL_RE.search(folded[index]) or not NET_LABEL_RE.search(folded[index]):
            continue
        net = _money_after(lines, folded, index, NET_RE, NET_LABEL_RE)
        if net is not None:
            fields["net"] = net
            break
    return gross_index


def extract_receipt(lines: Sequence[TextLine], *, organization_rut: str | None = None) -> PageExtraction:
    """Extrae los campos de una página ya clasificada como boleta."""
    folded = [fold(line.text) for line in lines]
    fields: dict[str, Extracted] = {}
    notes: list[str] = []

    header_index = _find_index(folded, HEADER_RE)
    receiver_index = _find_index(folded, RECEIVER_RE)
    service_index = _find_index(folded, SERVICE_RE)
    candidates_end = [i for i in (receiver_index, service_index) if i is not None]
    header_end = min(candidates_end) if candidates_end else min(len(lines), 12)

    exclude = {organization_rut} if organization_rut else set()
    issuer = _pick_rut(_rut_candidates(lines, folded, 0, header_end), exclude)
    if issuer is not None:
        fields["issuer_rut"] = Extracted(issuer[0].rut, issuer[0].rut, issuer[1])
        exclude.add(issuer[0].rut)

    if receiver_index is not None:
        stop = service_index if service_index is not None and service_index > receiver_index else receiver_index + 3
        receiver = _pick_rut(
            _rut_candidates(lines, folded, receiver_index, stop),
            {fields["issuer_rut"].value} if "issuer_rut" in fields else set(),
        )
        if receiver is not None:
            fields["receiver_rut"] = Extracted(receiver[0].rut, receiver[0].rut, receiver[1])
        label = RECEIVER_RE.search(folded[receiver_index])
        if label is not None:
            tail_folded = folded[receiver_index][label.end() :]
            cut = re.search(r"\bRUT\b", tail_folded)
            tail = lines[receiver_index].text[label.end() : label.end() + (cut.start() if cut else len(tail_folded))]
            name = collapse_spaces(tail.strip(" :-,"))
            if name:
                fields["receiver_name"] = Extracted(name, name, lines[receiver_index].confidence)

    name_stop = header_end
    if issuer is not None:
        rut_line = _rut_line_index(lines, issuer[0].rut, header_end)
        if rut_line is not None:
            name_stop = rut_line + 1
    name = _extract_name(lines, folded, name_stop)
    if name is not None:
        fields["issuer_name"] = name

    folio = _extract_folio(lines, folded, header_index, header_end)
    if folio is not None:
        fields["folio"] = folio

    issue_date = _extract_date(lines, folded)
    if issue_date is not None:
        fields["issue_date"] = issue_date

    gross_index = _extract_amounts(lines, folded, fields)

    gloss, gloss_confidence = _extract_gloss(lines, folded, service_index, gross_index)
    if gloss is not None:
        fields["gloss"] = Extracted(gloss, gloss, gloss_confidence)
        gloss_folded = fold(gloss)
        period, raw_period = _extract_service_period(gloss_folded, issue_date.value if issue_date else None)
        if period is not None:
            fields["service_period"] = Extracted(period, raw_period, gloss_confidence)
        elif raw_period is not None:
            notes.append(f"La glosa menciona '{raw_period}' pero no se pudo determinar el año del servicio.")
        hours, workday, raw_hours = _extract_hours(gloss_folded)
        if hours is not None:
            fields["hours"] = Extracted(hours, raw_hours, gloss_confidence)
        if workday is not None:
            fields["workday_type"] = Extracted(workday, workday.value, gloss_confidence)
        decree, raw_decree = find_decree(gloss_folded)
        if decree is not None:
            fields["decree"] = Extracted(decree, raw_decree, gloss_confidence)

    program_text = gloss or "\n".join(line.text for index, line in enumerate(lines) if index != receiver_index)
    return PageExtraction(fields=fields, program_text=program_text, notes=tuple(notes))
