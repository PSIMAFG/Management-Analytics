"""Constructores de datos de prueba compartidos por los tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from receipt_reader.data.synthetic import (
    ORGANIZATION_NAME,
    ORGANIZATION_RUT,
    DocFormat,
    PageSpec,
    SyntheticDocument,
    SyntheticProvider,
    SyntheticReceipt,
    printed_rut,
)
from receipt_reader.data.synthetic_render import SERVICE_LABEL
from receipt_reader.domain.dates import Period
from receipt_reader.domain.layout import TextLine, lines_from_text
from receipt_reader.domain.models import WorkdayType
from receipt_reader.domain.retention import expected_retention
from receipt_reader.domain.rut import make_rut
from receipt_reader.services.demo import DemoResult

FIXED_NOW = datetime(2026, 7, 20, 10, 0)
ISSUER_RUT = make_rut(41_234_567)
OTHER_ISSUER_RUT = make_rut(42_345_678)
ABSOLUTE_PATH_COLUMNS = frozenset({"path", "root_path"})


@dataclass(frozen=True)
class DemoEnvironment:
    """Base de ejemplo creada una vez por sesión y las carpetas de sus archivos."""

    root: Path
    db_path: Path
    lot_dir: Path
    samples_dir: Path
    result: DemoResult


def fixed_clock() -> datetime:
    return FIXED_NOW


def receipt_text(
    *,
    name: str = "CAMILA ANDREA ROJAS SOTO",
    rut_line: str | None = None,
    folio_line: str = "N ° 245",
    phone_line: str = "TELEFONO: 987654321",
    date_line: str = "Fecha: 03 de Noviembre de 2025",
    receiver_line: str | None = None,
    gloss: Sequence[str] = (
        "SERVICIOS PROFESIONALES ATENCIÓN COMUNITARIA, MES DE OCTUBRE 2025,",
        "22 HRS. SEMANALES, DECRETO N° 3619/2025",
    ),
    gross_line: str = "Total Honorarios: $: 800.000",
    retention_line: str = "14.50 % Impto. Retenido: 116.000",
    net_line: str = "Total: 684.000",
    emission_line: str = "Fecha / Hora Emisión: 03/11/2025 12:04",
) -> str:
    """Texto de una boleta con el orden de etiquetas de una boleta electrónica (datos ficticios)."""
    rut = rut_line if rut_line is not None else f"RUT: {printed_rut(ISSUER_RUT)}"
    receiver = (
        receiver_line
        if receiver_line is not None
        else f"Señor(es): {ORGANIZATION_NAME.upper()} Rut: {printed_rut(ORGANIZATION_RUT)}"
    )
    lines = [
        "BOLETA DE HONORARIOS",
        f"{name} ELECTRONICA",
        folio_line,
        rut,
        "SERVICIOS PROFESIONALES INDEPENDIENTES",
        "CALLE UNO 1234, CIUDAD EJEMPLO",
        phone_line,
        date_line,
        receiver,
        "Domicilio: AVENIDA CENTRAL 100, CIUDAD EJEMPLO",
        SERVICE_LABEL,
        *gloss,
        gross_line,
        retention_line,
        net_line,
        emission_line,
        "EJEMPLO SINTÉTICO - SIN VALIDEZ TRIBUTARIA",
    ]
    return "\n".join(line for line in lines if line)


def receipt_lines(**parts: Any) -> list[TextLine]:
    return lines_from_text(receipt_text(**parts))


def provider(
    *,
    rut: str = ISSUER_RUT,
    name: str = "Camila Andrea Rojas Soto",
    program_code: str = "110",
    hours: Decimal = Decimal(22),
    workday: WorkdayType = WorkdayType.WEEKLY,
) -> SyntheticProvider:
    return SyntheticProvider(
        rut=rut,
        name=name,
        feminine=True,
        profession="PSICÓLOGA",
        activity="SERVICIOS PROFESIONALES INDEPENDIENTES",
        address="Calle Uno 1234, Ciudad Ejemplo",
        phone="987654321",
        program_code=program_code,
        workday=workday,
        hours=hours,
        rate_factor=Decimal(1),
        first_period_index=0,
        last_period_index=7,
        first_folio=100,
    )


def synthetic_receipt(
    *,
    who: SyntheticProvider | None = None,
    folio: int = 245,
    issue_date: date = date(2026, 3, 4),
    gross: int = 720_000,
    rate_bp: int = 1525,
    program_code: str = "110",
    gloss: tuple[str, ...] = (
        "SERVICIOS PROFESIONALES ATENCIÓN COMUNITARIA, MES DE FEBRERO 2026,",
        "22 HRS. SEMANALES, DECRETO N° 1234/2026",
    ),
) -> SyntheticReceipt:
    retention = expected_retention(gross, rate_bp)
    return SyntheticReceipt(
        provider=who or provider(),
        folio=folio,
        issue_date=issue_date,
        emission_time="10:15",
        service_period=Period(issue_date.year, issue_date.month).shift(-1),
        payment_period=Period(issue_date.year, issue_date.month),
        program_code=program_code,
        gloss_lines=gloss,
        gross=gross,
        retention=retention,
        net=gross - retention,
        rate_bp=rate_bp,
    )


def document(
    relative_path: str, receipts: Sequence[SyntheticReceipt | None], doc_format: DocFormat = DocFormat.NATIVE_PDF
) -> SyntheticDocument:
    """Documento con una página por elemento; None agrega una página anexa sin boleta."""
    pages = tuple(
        PageSpec(receipt)
        if receipt is not None
        else PageSpec(None, "INFORME MENSUAL DE ACTIVIDADES", ("Actividades del período.", "Firma de la jefatura."))
        for receipt in receipts
    )
    return SyntheticDocument(relative_path, doc_format, pages, noise=0.2)


def table_dump(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Contenido de todas las tablas (sin las rutas absolutas) para comparar bases."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        dump: dict[str, list[tuple[object, ...]]] = {}
        for table in tables:
            columns = [
                row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[1] not in ABSOLUTE_PATH_COLUMNS
            ]
            names = ", ".join(columns)
            dump[table] = list(conn.execute(f"SELECT {names} FROM {table} ORDER BY 1, 2"))
        return dump
    finally:
        conn.close()


def receipt_id_by_tag(db_path: Path, tag: str) -> int:
    """Id de la boleta del lote de ejemplo que lleva la marca indicada."""
    from receipt_reader.data.synthetic import build_demo_lot
    from receipt_reader.services.reports import ReportService

    document = next(doc for doc in build_demo_lot().documents if tag in doc.tags)
    rows = ReportService(db_path).rows()
    return next(row.id for row in rows if row.relative_path == document.relative_path)
