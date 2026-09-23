"""Generador reproducible de boletas de honorarios ficticias.

Produce la especificación de un lote de ejemplo (prestadores, boletas,
formatos de archivo y casos para la revisión) a partir de una semilla fija.
Los datos son sintéticos pero conservan la estructura de un caso real:

- 14 prestadores con RUT entre 40.000.000 y 44.999.999 (DV válido), nombres
  armados con listas de nombres y apellidos comunes.
- 4 programas genéricos; uno concentra cerca de la mitad de las boletas.
- 8 meses de pago; el servicio corresponde casi siempre al mes anterior.
- Bruto = valor hora × horas semanales × 4 (o × horas mensuales), con valores
  hora del orden de 8.000 a 10.500 pesos, retención con la tasa del año de
  emisión y líquido = bruto - retención.
- Formatos: PDF nativo, PDF escaneado, paquetes de varias páginas e imágenes.

La escritura de los archivos está en `synthetic_render`.
"""

from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum

from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import Settings, WorkdayType
from receipt_reader.domain.money import round_half_up
from receipt_reader.domain.retention import LEGAL_RATES_BP, RetentionTable, expected_retention
from receipt_reader.domain.rut import make_rut
from receipt_reader.domain.text import MONTH_NAMES

SEED = 2026

ORGANIZATION_NAME = "Organización Ejemplo"
ORGANIZATION_RUT = make_rut(45_210_804)
ORGANIZATION_ADDRESS = "Avenida Central 100, Ciudad Ejemplo"
OTHER_RECEIVER_NAME = "Entidad Ejemplo Dos"
OTHER_RECEIVER_RUT = make_rut(45_873_112)

DEMO_PAYMENT_PERIODS = tuple(Period(2025, 11).shift(offset) for offset in range(8))
SAMPLE_PAYMENT_PERIOD = Period(2026, 7)
DEMO_SETTINGS = Settings(
    organization_rut=ORGANIZATION_RUT,
    organization_name=ORGANIZATION_NAME,
    date_window_start=date(2025, 10, 1),
    date_window_end=date(2026, 7, 31),
    amount_min=30_000,
    amount_max=5_000_000,
    retention_tolerance=1,
    folder_month_tolerance=1,
    ocr_min_confidence=0.85,
    ocr_dpi=200,
)


@dataclass(frozen=True)
class ProgramSpec:
    folder_code: str
    name: str
    short_name: str
    aliases: tuple[tuple[str, int], ...]
    base_hourly_2025: int


PROGRAMS: tuple[ProgramSpec, ...] = (
    ProgramSpec(
        "110",
        "Programa de Atención Comunitaria",
        "Atención Comunitaria",
        (("Programa de Atención Comunitaria", 10), ("Atención Comunitaria", 20), ("PAC", 40)),
        8_150,
    ),
    ProgramSpec(
        "220",
        "Programa de Apoyo Familiar",
        "Apoyo Familiar",
        (("Programa de Apoyo Familiar", 10), ("Apoyo Familiar", 20), ("PAF", 40)),
        9_950,
    ),
    ProgramSpec(
        "330",
        "Programa de Intervención Temprana",
        "Intervención Temprana",
        (("Programa de Intervención Temprana", 10), ("Intervención Temprana", 20), ("PIT", 40)),
        8_450,
    ),
    ProgramSpec(
        "440",
        "Programa de Rehabilitación Integral",
        "Rehabilitación Integral",
        (("Programa de Rehabilitación Integral", 10), ("Rehabilitación Integral", 20), ("PRI", 40)),
        8_800,
    ),
)
YEARLY_RATE_INCREASE = Decimal("1.035")
REFERENCE_BAND = Decimal("0.08")

FIRST_NAMES_F = ("Camila", "Valentina", "Francisca", "Javiera", "Constanza", "Daniela", "Catalina", "Fernanda")
FIRST_NAMES_M = ("Matías", "Sebastián", "Nicolás", "Felipe", "Diego", "Tomás", "Joaquín", "Cristóbal")
SURNAMES = (
    "González",
    "Muñoz",
    "Rojas",
    "Díaz",
    "Pérez",
    "Soto",
    "Contreras",
    "Silva",
    "Martínez",
    "Sepúlveda",
    "Morales",
    "Fuentes",
    "Araya",
    "Espinoza",
    "Valenzuela",
    "Castillo",
    "Tapia",
    "Reyes",
    "Pizarro",
    "Núñez",
)
PROFESSIONS = (
    ("PSICÓLOGA", "PSICÓLOGO"),
    ("TRABAJADORA SOCIAL", "TRABAJADOR SOCIAL"),
    ("TERAPEUTA OCUPACIONAL", "TERAPEUTA OCUPACIONAL"),
    ("FONOAUDIÓLOGA", "FONOAUDIÓLOGO"),
    ("EDUCADORA", "EDUCADOR"),
)
ACTIVITIES = (
    "ASESORÍAS Y SERVICIOS PROFESIONALES",
    "SERVICIOS PROFESIONALES INDEPENDIENTES",
    "ACTIVIDADES DE CONSULTORÍA PROFESIONAL",
)
STREETS = ("Calle Uno", "Calle Dos", "Pasaje Tres", "Avenida Cuatro", "Calle Cinco", "Pasaje Seis")
WEEKLY_HOURS = (11, 15, 22, 22, 22, 33, 44)
MONTHLY_HOURS = (40, 64, 88)
GLOSS_WIDTH = 58


class DocFormat(StrEnum):
    NATIVE_PDF = "native_pdf"
    SCANNED_PDF = "scanned_pdf"
    PACKAGE_PDF = "package_pdf"
    PNG = "png"
    JPG = "jpg"
    CORRUPT_PDF = "corrupt_pdf"
    ANNEX_PDF = "annex_pdf"

    @property
    def extension(self) -> str:
        return {"png": ".png", "jpg": ".jpg"}.get(self.value, ".pdf")

    @property
    def is_raster(self) -> bool:
        return self in (DocFormat.SCANNED_PDF, DocFormat.PACKAGE_PDF, DocFormat.PNG, DocFormat.JPG, DocFormat.ANNEX_PDF)


@dataclass(frozen=True)
class SyntheticProvider:
    rut: str
    name: str
    feminine: bool
    profession: str
    activity: str
    address: str
    phone: str
    program_code: str
    workday: WorkdayType
    hours: Decimal
    rate_factor: Decimal
    first_period_index: int
    last_period_index: int
    first_folio: int

    @property
    def short_name(self) -> str:
        parts = self.name.split()
        return f"{parts[0]} {parts[-2]}"


@dataclass(frozen=True)
class SyntheticReceipt:
    """Contenido impreso de una boleta y, aparte, lo que "leería" el OCR simulado."""

    provider: SyntheticProvider
    folio: int
    issue_date: date
    emission_time: str
    service_period: Period | None
    payment_period: Period
    program_code: str
    gloss_lines: tuple[str, ...]
    gross: int
    retention: int
    net: int
    rate_bp: int
    receiver_name: str = ORGANIZATION_NAME
    receiver_rut: str = ORGANIZATION_RUT
    print_folio: bool = True
    rate_text_style: int = 0
    ocr_overrides: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageSpec:
    receipt: SyntheticReceipt | None = None
    annex_title: str = ""
    annex_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyntheticDocument:
    relative_path: str
    doc_format: DocFormat
    pages: tuple[PageSpec, ...]
    noise: float = 0.3
    ocr_confidence: tuple[float, float] = (0.93, 0.995)
    copy_of: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def receipt(self) -> SyntheticReceipt | None:
        return next((page.receipt for page in self.pages if page.receipt is not None), None)


@dataclass(frozen=True)
class DemoLot:
    documents: tuple[SyntheticDocument, ...]
    providers: tuple[SyntheticProvider, ...]


def format_money(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def printed_rut(rut: str, dash: str = "\u2212") -> str:
    body, dv = rut.split("-")
    return f"{int(body):,}".replace(",", ".") + dash + dv


def printed_rate(rate_bp: int, style: int) -> str:
    """'14.50' o '14.5', como se imprime la tasa en la boleta (punto decimal)."""
    whole, cents = divmod(rate_bp, 100)
    text = f"{whole}.{cents:02d}"
    if style == 1 and text.endswith("0"):
        text = text[:-1]
    return text


def hourly_rate(program: ProgramSpec, year: int) -> Decimal:
    rate = Decimal(program.base_hourly_2025)
    for _ in range(max(year - 2025, 0)):
        rate *= YEARLY_RATE_INCREASE
    return rate


def reference_range(program: ProgramSpec, year: int) -> tuple[int, int]:
    """Rango de referencia del valor hora, redondeado a 50 pesos."""
    base = hourly_rate(program, year)
    low = round_half_up(base * (1 - REFERENCE_BAND) / 50) * 50
    high = round_half_up(base * (1 + REFERENCE_BAND) / 50) * 50
    return low, high


def _program(code: str) -> ProgramSpec:
    return next(program for program in PROGRAMS if program.folder_code == code)


def _unique_rut_body(rng: random.Random, used: set[int], low: int, high: int) -> int:
    while True:
        body = rng.randint(low, high)
        if body not in used:
            used.add(body)
            return body


def build_providers(rng: random.Random) -> tuple[SyntheticProvider, ...]:
    """14 prestadores: 8 en el programa principal y 2 en cada uno de los otros."""
    program_codes = ["110"] * 8 + ["220"] * 2 + ["330"] * 2 + ["440"] * 2
    used_bodies: set[int] = set()
    used_names: set[str] = set()
    providers: list[SyntheticProvider] = []
    for index, code in enumerate(program_codes):
        feminine = rng.random() < 0.65
        while True:
            first = rng.choice(FIRST_NAMES_F if feminine else FIRST_NAMES_M)
            second = rng.choice(FIRST_NAMES_F if feminine else FIRST_NAMES_M)
            last1, last2 = rng.sample(SURNAMES, 2)
            name = f"{first} {second} {last1} {last2}" if first != second else f"{first} {last1} {last2}"
            if name not in used_names:
                used_names.add(name)
                break
        profession = rng.choice(PROFESSIONS)[0 if feminine else 1]
        monthly = index in (5, 12)
        if index == 9:
            hours = Decimal("12.5")
        elif monthly:
            hours = Decimal(rng.choice(MONTHLY_HOURS))
        else:
            hours = Decimal(rng.choice(WEEKLY_HOURS))
        first_period = 0 if index not in (3, 11) else rng.randint(2, 3)
        last_period = len(DEMO_PAYMENT_PERIODS) - 1 if index != 7 else 4
        providers.append(
            SyntheticProvider(
                rut=make_rut(_unique_rut_body(rng, used_bodies, 40_000_000, 44_999_999)),
                name=name,
                feminine=feminine,
                profession=profession,
                activity=rng.choice(ACTIVITIES),
                address=f"{rng.choice(STREETS)} {rng.randint(100, 2999)}, Ciudad Ejemplo",
                phone=f"9{rng.randint(10_000_000, 99_999_999)}",
                program_code=code,
                workday=WorkdayType.MONTHLY if monthly else WorkdayType.WEEKLY,
                hours=hours,
                rate_factor=Decimal(rng.randint(970, 1030)) / 1000,
                first_period_index=first_period,
                last_period_index=last_period,
                first_folio=rng.randint(20, 380),
            )
        )
    return tuple(providers)


def _hours_text(hours: Decimal) -> str:
    return format(hours.normalize(), "f").replace(".", ",")


def build_gloss(
    rng: random.Random,
    provider: SyntheticProvider,
    program: ProgramSpec,
    service: Period | None,
    decree: tuple[int, date],
    *,
    style: int | None = None,
) -> tuple[str, ...]:
    """Glosa con programa, decreto, mes de servicio y jornada, en líneas de ancho acotado."""
    number, decree_date = decree
    month = MONTH_NAMES[service.month - 1].upper() if service else ""
    hours = _hours_text(provider.hours)
    kind = "SEMANALES" if provider.workday is WorkdayType.WEEKLY else "MENSUALES"
    variant = rng.randrange(3) if style is None else style
    program_name = program.name.upper()
    if variant == 0:
        month_part = f" MES DE {month} \u2212 {hours} HORAS {kind}" if service else f" {hours} HORAS {kind}"
        text = (
            f"PRESTACIÓN DE SERVICIOS PROFESIONALES COMO {provider.profession} EN {program_name}, "
            f"SEGÚN D.A. N° {number} DEL {decree_date:%d.%m.%y}.{month_part}"
        )
    elif variant == 1:
        month_part = f" MES DE {month} {service.year}," if service else ""
        text = (
            f"SERVICIOS PROFESIONALES {program.short_name.upper()},{month_part} {hours} HRS. {kind}, "
            f"DECRETO N° {number}/{decree_date.year}"
        )
    else:
        month_part = f" CORRESPONDIENTE AL MES DE {month}," if service else ""
        text = (
            f"ATENCIÓN PROFESIONAL {program_name}{month_part} JORNADA DE {hours} HORAS {kind}. "
            f"D.A. {number}/{decree_date.year}"
        )
    return tuple(textwrap.wrap(text, GLOSS_WIDTH))


def _gross_for(provider: SyntheticProvider, program: ProgramSpec, year: int) -> int:
    rate = round_half_up(hourly_rate(program, year) * provider.rate_factor / 10) * 10
    return round_half_up(Decimal(rate) * provider.hours * provider.workday.weeks_factor)


def _make_receipt(
    rng: random.Random,
    provider: SyntheticProvider,
    folio: int,
    payment: Period,
    table: RetentionTable,
    decrees: dict[tuple[str, int], tuple[int, date]],
    *,
    service_offset: int = 1,
    gross_factor: Decimal = Decimal(1),
    omit_service_month: bool = False,
    program_code: str | None = None,
    gloss_program_code: str | None = None,
) -> SyntheticReceipt:
    day = rng.randint(1, 9)
    issue = date(payment.year, payment.month, day)
    service = payment.shift(-service_offset)
    program = _program(provider.program_code)
    decree_key = (provider.rut, service.year)
    if decree_key not in decrees:
        decrees[decree_key] = (rng.randint(1000, 4999), date(service.year, 1, rng.randint(5, 28)))
    gloss_program = _program(gloss_program_code or provider.program_code)
    gloss = build_gloss(rng, provider, gloss_program, None if omit_service_month else service, decrees[decree_key])
    gross = _gross_for(provider, program, service.year)
    if gross_factor != 1:
        gross = round_half_up(Decimal(gross) * gross_factor / 100) * 100
    rate_bp = table.rate_for(issue.year) or 0
    retention = expected_retention(gross, rate_bp)
    return SyntheticReceipt(
        provider=provider,
        folio=folio,
        issue_date=issue,
        emission_time=f"{rng.randint(8, 18):02d}:{rng.randint(0, 59):02d}",
        service_period=service,
        payment_period=payment,
        program_code=program_code or provider.program_code,
        gloss_lines=gloss,
        gross=gross,
        retention=retention,
        net=gross - retention,
        rate_bp=rate_bp,
        rate_text_style=rng.randrange(2),
    )


def _file_stem(receipt: SyntheticReceipt, rng: random.Random, with_month: bool | None = None) -> str:
    stem = f"{receipt.provider.short_name} B {receipt.folio}"
    add_month = rng.random() < 0.25 if with_month is None else with_month
    if add_month and receipt.service_period is not None:
        stem += f" MES {MONTH_NAMES[receipt.service_period.month - 1].upper()}"
    return stem


def _folder(payment: Period, code: str) -> str:
    program = next((p for p in PROGRAMS if p.folder_code == code), None)
    label = program.short_name if program else "Programa sin catálogo"
    return f"{payment.iso()}/{code} {label}"


def _pick_format(rng: random.Random) -> DocFormat:
    roll = rng.random()
    if roll < 0.45:
        return DocFormat.NATIVE_PDF
    if roll < 0.70:
        return DocFormat.SCANNED_PDF
    if roll < 0.85:
        return DocFormat.PACKAGE_PDF
    return DocFormat.PNG if rng.random() < 0.5 else DocFormat.JPG


ANNEX_TITLES = ("INFORME MENSUAL DE ACTIVIDADES", "CERTIFICADO DE CUMPLIMIENTO", "REGISTRO DE ASISTENCIA")


def _annex_page(rng: random.Random, receipt: SyntheticReceipt) -> PageSpec:
    title = rng.choice(ANNEX_TITLES)
    period = receipt.service_period.label() if receipt.service_period else receipt.payment_period.label()
    lines = (
        f"Prestador: {receipt.provider.name}",
        f"Período informado: {period}",
        "Actividades de coordinación con el equipo del programa.",
        "Elaboración de informes y registro de prestaciones.",
        "Se adjunta la boleta de honorarios del período.",
        "Firma y timbre de la jefatura del programa.",
    )
    return PageSpec(None, title, lines)


def _document_for(
    rng: random.Random, receipt: SyntheticReceipt, doc_format: DocFormat, *, stem: str | None = None
) -> SyntheticDocument:
    folder = _folder(receipt.payment_period, receipt.program_code)
    name = (stem or _file_stem(receipt, rng)) + doc_format.extension
    pages: tuple[PageSpec, ...] = (PageSpec(receipt),)
    if doc_format is DocFormat.PACKAGE_PDF:
        annexes = [_annex_page(rng, receipt) for _ in range(rng.randint(1, 2))]
        position = rng.randint(0, len(annexes))
        pages = (*annexes[:position], PageSpec(receipt), *annexes[position:])
    noise = 0.0 if doc_format is DocFormat.NATIVE_PDF else round(rng.uniform(0.15, 0.45), 2)
    return SyntheticDocument(f"{folder}/{name}", doc_format, pages, noise=noise, tags=receipt.tags)


def _with_tags(receipt: SyntheticReceipt, *tags: str, **changes: object) -> SyntheticReceipt:
    return replace(receipt, tags=(*receipt.tags, *tags), **changes)


def build_demo_lot(seed: int = SEED) -> DemoLot:
    """Lote de ejemplo completo: boletas regulares más los casos para la demo de revisión."""
    rng = random.Random(seed)
    table = RetentionTable(LEGAL_RATES_BP)
    providers = build_providers(rng)
    decrees: dict[tuple[str, int], tuple[int, date]] = {}
    folios = {p.rut: p.first_folio for p in providers}
    documents: list[SyntheticDocument] = []
    regular: list[tuple[SyntheticReceipt, DocFormat]] = []

    for period_index, payment in enumerate(DEMO_PAYMENT_PERIODS):
        for provider in providers:
            if not provider.first_period_index <= period_index <= provider.last_period_index:
                continue
            if rng.random() < 0.05:
                continue  # boleta que no llegó este mes
            folios[provider.rut] += rng.randint(1, 4)
            offset = 2 if rng.random() < 0.05 else 1
            factor = Decimal(rng.choice(("0.5", "0.75"))) if rng.random() < 0.06 else Decimal(1)
            receipt = _make_receipt(
                rng,
                provider,
                folios[provider.rut],
                payment,
                table,
                decrees,
                service_offset=offset,
                gross_factor=factor,
                omit_service_month=rng.random() < 0.04,
            )
            if factor != 1:
                receipt = _with_tags(receipt, "mes_parcial")
            regular.append((receipt, _pick_format(rng)))

    # Casos de revisión aplicados sobre boletas regulares (índices fijos para que sean estables).
    special: dict[int, tuple[str, DocFormat]] = {}
    scanned_indexes = [i for i, (_, fmt) in enumerate(regular) if fmt.is_raster]
    native_indexes = [i for i, (_, fmt) in enumerate(regular) if fmt is DocFormat.NATIVE_PDF]
    special[scanned_indexes[3]] = ("folio_ilegible", DocFormat.SCANNED_PDF)
    special[scanned_indexes[7]] = ("liquido_mal_leido", DocFormat.SCANNED_PDF)
    special[scanned_indexes[11]] = ("bruto_ilegible", DocFormat.JPG)
    special[scanned_indexes[15]] = ("baja_confianza", DocFormat.SCANNED_PDF)
    special[native_indexes[4]] = ("tasa_otro_anio", DocFormat.NATIVE_PDF)
    special[native_indexes[9]] = ("receptor_distinto", DocFormat.NATIVE_PDF)
    special[native_indexes[14]] = ("programa_en_conflicto", DocFormat.NATIVE_PDF)
    special[native_indexes[18]] = ("carpeta_desconocida", DocFormat.NATIVE_PDF)

    for index, (receipt, doc_format) in enumerate(regular):
        case = special.get(index)
        if case is None:
            documents.append(_document_for(rng, receipt, doc_format))
            continue
        tag, doc_format = case
        documents.append(_special_document(rng, receipt, tag, doc_format, table, decrees))

    documents += _extra_cases(rng, documents, providers, regular, table, decrees)
    return DemoLot(tuple(documents), providers)


def _special_document(
    rng: random.Random,
    receipt: SyntheticReceipt,
    tag: str,
    doc_format: DocFormat,
    table: RetentionTable,
    decrees: dict[tuple[str, int], tuple[int, date]],
) -> SyntheticDocument:
    if tag == "folio_ilegible":
        receipt = _with_tags(receipt, tag, ocr_overrides={"folio": "N °"})
        return _document_for(rng, receipt, doc_format, stem=_file_stem(receipt, rng, with_month=False))
    if tag == "liquido_mal_leido":
        wrong = receipt.net + 50_000 if str(receipt.net)[-5] != "9" else receipt.net - 50_000
        receipt = _with_tags(receipt, tag, ocr_overrides={"net_amount": format_money(wrong)})
    elif tag == "bruto_ilegible":
        text = format_money(receipt.gross)
        receipt = _with_tags(receipt, tag, ocr_overrides={"gross_amount": text[:-1] + "O"})
    elif tag == "baja_confianza":
        receipt = _with_tags(receipt, tag)
        return replace(_document_for(rng, receipt, doc_format), noise=0.8, ocr_confidence=(0.62, 0.8))
    elif tag == "tasa_otro_anio":
        previous = table.rate_for(receipt.issue_date.year - 1) or receipt.rate_bp
        retention = expected_retention(receipt.gross, previous)
        receipt = _with_tags(receipt, tag, rate_bp=previous, retention=retention, net=receipt.gross - retention)
    elif tag == "receptor_distinto":
        receipt = _with_tags(receipt, tag, receiver_name=OTHER_RECEIVER_NAME, receiver_rut=OTHER_RECEIVER_RUT)
    elif tag == "programa_en_conflicto":
        other = next(p.folder_code for p in PROGRAMS if p.folder_code != receipt.program_code)
        program = _program(receipt.provider.program_code)
        decree = decrees[(receipt.provider.rut, receipt.service_period.year if receipt.service_period else 2025)]
        gloss = build_gloss(rng, receipt.provider, _program(other), receipt.service_period, decree, style=0)
        receipt = _with_tags(receipt, tag, gloss_lines=gloss, program_code=program.folder_code)
    elif tag == "carpeta_desconocida":
        receipt = _with_tags(receipt, tag, program_code="550")
    return _document_for(rng, receipt, doc_format)


def _extra_cases(
    rng: random.Random,
    documents: list[SyntheticDocument],
    providers: tuple[SyntheticProvider, ...],
    regular: list[tuple[SyntheticReceipt, DocFormat]],
    table: RetentionTable,
    decrees: dict[tuple[str, int], tuple[int, date]],
) -> list[SyntheticDocument]:
    """Duplicados, boleta antigua, archivo dañado y paquete sin boleta."""
    extra: list[SyntheticDocument] = []
    # Copia exacta de un archivo ya entregado, guardada otra vez en otra carpeta (mismo contenido).
    original = documents[10]
    period_folder = original.relative_path.split("/", 1)[0]
    copy_path = f"{period_folder}/Reenvios/" + original.relative_path.rsplit("/", 1)[1]
    extra.append(replace(original, relative_path=copy_path, copy_of=original.relative_path, tags=("copia_exacta",)))
    # La misma boleta escaneada y enviada de nuevo al mes siguiente: mismo emisor y folio.
    resent_receipt, _ = regular[20]
    next_payment = resent_receipt.payment_period.shift(1)
    resent = _with_tags(resent_receipt, "folio_repetido", payment_period=next_payment)
    extra.append(_document_for(rng, resent, DocFormat.SCANNED_PDF, stem=_file_stem(resent, rng, with_month=True)))
    # Boleta antigua entregada tarde: emitida fuera de la ventana del lote.
    provider = providers[2]
    old = _make_receipt(rng, provider, provider.first_folio - 5, Period(2024, 10), table, decrees)
    old = _with_tags(old, "fecha_fuera_de_ventana", payment_period=DEMO_PAYMENT_PERIODS[1])
    extra.append(_document_for(rng, old, DocFormat.NATIVE_PDF))
    # Archivo dañado y paquete que solo trae anexos.
    damaged_receipt, _ = regular[30]
    damaged = _document_for(rng, damaged_receipt, DocFormat.NATIVE_PDF)
    damaged_path = damaged.relative_path.rsplit(".", 1)[0] + " escaneo incompleto.pdf"
    extra.append(
        replace(damaged, relative_path=damaged_path, doc_format=DocFormat.CORRUPT_PDF, tags=("archivo_danado",))
    )
    annex_receipt, _ = regular[40]
    annex_only = _document_for(rng, annex_receipt, DocFormat.PACKAGE_PDF)
    pages = tuple(page for page in annex_only.pages if page.receipt is None) or (_annex_page(rng, annex_receipt),)
    stem = annex_only.relative_path.rsplit(".", 1)[0] + " anexos.pdf"
    extra.append(SyntheticDocument(stem, DocFormat.ANNEX_PDF, pages, noise=0.3, tags=("sin_boleta",)))
    return extra


def build_sample_set(seed: int = SEED + 1) -> tuple[SyntheticDocument, ...]:
    """Archivos de muestra para procesar con OCR real desde la interfaz (mes siguiente al lote)."""
    rng = random.Random(seed)
    lot_rng = random.Random(SEED)
    providers = build_providers(lot_rng)
    table = RetentionTable(LEGAL_RATES_BP)
    decrees: dict[tuple[str, int], tuple[int, date]] = {}
    payment = SAMPLE_PAYMENT_PERIOD
    formats = (
        DocFormat.NATIVE_PDF,
        DocFormat.SCANNED_PDF,
        DocFormat.PNG,
        DocFormat.NATIVE_PDF,
        DocFormat.PACKAGE_PDF,
        DocFormat.JPG,
        DocFormat.SCANNED_PDF,
        DocFormat.NATIVE_PDF,
        DocFormat.SCANNED_PDF,
        DocFormat.NATIVE_PDF,
        DocFormat.PNG,
        DocFormat.NATIVE_PDF,
    )
    documents: list[SyntheticDocument] = []
    for index, doc_format in enumerate(formats):
        provider = providers[index]
        folio = provider.first_folio + 60 + index
        receipt = _make_receipt(rng, provider, folio, payment, table, decrees)
        if index == 3:
            receipt = _with_tags(receipt, "folio_ilegible", print_folio=False)
        elif index == 6:
            receipt = _with_tags(receipt, "liquido_incoherente", net=receipt.net - 10_000)
        elif index == 7:
            previous = table.rate_for(receipt.issue_date.year - 1) or receipt.rate_bp
            retention = expected_retention(receipt.gross, previous)
            receipt = _with_tags(
                receipt, "tasa_otro_anio", rate_bp=previous, retention=retention, net=receipt.gross - retention
            )
        elif index == 9:
            receipt = _with_tags(
                receipt, "receptor_distinto", receiver_name=OTHER_RECEIVER_NAME, receiver_rut=OTHER_RECEIVER_RUT
            )
        elif index == 11:
            other = next(p.folder_code for p in PROGRAMS if p.folder_code != receipt.program_code)
            key = (provider.rut, receipt.service_period.year if receipt.service_period else payment.year)
            gloss = build_gloss(rng, provider, _program(other), receipt.service_period, decrees[key], style=0)
            receipt = _with_tags(receipt, "programa_en_conflicto", gloss_lines=gloss)
        documents.append(
            _document_for(rng, receipt, doc_format, stem=_file_stem(receipt, rng, with_month=index % 4 == 1))
        )
    return tuple(documents)
