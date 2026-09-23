"""Procesamiento de carpetas de extremo a extremo sobre archivos sintéticos."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from receipt_reader.data import db
from receipt_reader.data.ocr import SyntheticOcrEngine
from receipt_reader.data.synthetic import DocFormat
from receipt_reader.data.synthetic_render import write_documents
from receipt_reader.domain.dates import Period
from receipt_reader.domain.layout import TextBox
from receipt_reader.domain.models import RECEIPT_FIELDS, FieldSource, ReadStatus, ReceiptStatus, TextKind
from receipt_reader.domain.reporting import ReceiptFilter
from receipt_reader.services.processing import ProcessingService
from receipt_reader.services.reports import ReportService
from receipt_reader.services.review import ReviewService
from support import OTHER_ISSUER_RUT, document, fixed_clock, provider, synthetic_receipt

FOLDER = "2026-03/110 Atención Comunitaria"


def _service(db_path: Path, pages: Mapping[tuple[str, int], list[TextBox]]) -> ProcessingService:
    return ProcessingService(db_path, ocr=SyntheticOcrEngine(pages), clock=fixed_clock)


def test_native_pdf_end_to_end(catalog_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "entrada"
    pages = write_documents(
        [document(f"{FOLDER}/Camila Rojas B 245.pdf", [synthetic_receipt()])], root, dpi=100, seed=1
    )
    result = _service(catalog_db, pages).process_folder(root)
    assert (result.files_found, result.files_processed, result.receipts_created) == (1, 1, 1)
    assert result.status_counts == {ReceiptStatus.APPROVED: 1}
    rows = ReportService(catalog_db).rows()
    row = rows[0]
    assert row.text_kind is TextKind.NATIVE
    assert (row.issuer_rut, row.folio, row.gross, row.retention, row.net) == (
        "41234567-3",
        245,
        720_000,
        109_800,
        610_200,
    )
    assert row.issue_date == date(2026, 3, 4)
    assert row.service_period == Period(2026, 2)
    assert row.payment_period == Period(2026, 3)
    assert row.program_name == "Programa de Atención Comunitaria"
    assert row.blocking_count == 0


def test_every_receipt_page_of_a_package_is_registered(catalog_db: Path, tmp_path: Path) -> None:
    # B12 y A12: la boleta puede no estar en la página 1 y un PDF puede traer varias.
    root = tmp_path / "entrada"
    second = synthetic_receipt(folio=246, gross=360_000)
    docs = [
        document(f"{FOLDER}/paquete escaneado.pdf", [None, synthetic_receipt(), None], DocFormat.PACKAGE_PDF),
        document(f"{FOLDER}/dos boletas.pdf", [synthetic_receipt(folio=300), None, second]),
    ]
    pages = write_documents(docs, root, dpi=100, seed=2)
    _service(catalog_db, pages).process_folder(root)
    rows = sorted(ReportService(catalog_db).rows(), key=lambda row: (row.file_name, row.page_index))
    assert [(row.file_name, row.page_index, row.folio) for row in rows] == [
        ("dos boletas.pdf", 0, 300),
        ("dos boletas.pdf", 2, 246),
        ("paquete escaneado.pdf", 1, 245),
    ]
    scanned = next(row for row in rows if row.file_name == "paquete escaneado.pdf")
    assert scanned.text_kind is TextKind.OCR
    assert scanned.ocr_confidence is not None
    assert all(row.status is ReceiptStatus.APPROVED for row in rows)


def test_field_traces_are_keyed_by_the_receipt_fields(catalog_db: Path, tmp_path: Path) -> None:
    # B39: la confianza se guarda por campo, tipada y con el mismo nombre del campo de la boleta,
    # así que ninguna métrica puede leer una clave de confianza que no existe.
    root = tmp_path / "entrada"
    doc = document(f"{FOLDER}/escaneada.pdf", [synthetic_receipt()], DocFormat.SCANNED_PDF)
    pages = write_documents([doc], root, dpi=100, seed=12)
    _service(catalog_db, pages).process_folder(root)
    row = ReportService(catalog_db).rows()[0]
    assert row.text_kind is TextKind.OCR
    traces = ReviewService(catalog_db).detail(row.id).record.traces
    assert set(traces) <= set(RECEIPT_FIELDS)
    for name in ("issuer_rut", "folio", "issue_date", "gross", "retention", "net"):
        trace = traces[name]
        assert trace.source is FieldSource.OCR, name
        assert trace.confidence is not None, name
        assert 0 < trace.confidence <= 1, name
    assert traces["payment_period"].source is FieldSource.FOLDER


def test_duplicate_file_and_duplicate_folio(catalog_db: Path, tmp_path: Path) -> None:
    # B06 y B40: el lote se consolida sobre la base y no en una memoria aparte, así que los
    # duplicados (por hash y por RUT y folio) se detectan dentro de la misma corrida.
    root = tmp_path / "entrada"
    pages = write_documents([document(f"{FOLDER}/original.pdf", [synthetic_receipt()])], root, dpi=100, seed=3)
    copy_dir = root / "2026-04" / "110 Atención Comunitaria"
    copy_dir.mkdir(parents=True)
    shutil.copyfile(root / FOLDER / "original.pdf", copy_dir / "reenvio.pdf")
    other = document("2026-04/110 Atención Comunitaria/otro contenido.pdf", [synthetic_receipt(gross=700_000)])
    pages |= write_documents([other], root, dpi=100, seed=4)
    _service(catalog_db, pages).process_folder(root)
    rows = {row.file_name: row for row in ReportService(catalog_db).rows()}
    assert rows["original.pdf"].status is ReceiptStatus.APPROVED
    copy = rows["reenvio.pdf"]
    assert copy.status is ReceiptStatus.PENDING
    assert {"DUPLICATE_FILE", "DUPLICATE_FOLIO"} <= set(copy.issue_codes)
    assert rows["otro contenido.pdf"].issue_codes == ("DUPLICATE_FOLIO",)
    assert rows["original.pdf"].sha256 == copy.sha256


def test_unreadable_and_annex_only_files_are_kept(catalog_db: Path, tmp_path: Path) -> None:
    # B07: todo archivo leído queda registrado con su estado; nada desaparece.
    root = tmp_path / "entrada"
    pages = write_documents([document(f"{FOLDER}/solo anexos.pdf", [None])], root, dpi=100, seed=5)
    (root / FOLDER / "dañado.pdf").write_bytes(b"%PDF-1.4\nno es un pdf")
    result = _service(catalog_db, pages).process_folder(root)
    assert result.unreadable_files == (f"{FOLDER}/dañado.pdf",)
    with db.session(catalog_db) as conn:
        statuses = dict(conn.execute("SELECT read_status, status FROM receipt").fetchall())
    assert statuses == {ReadStatus.UNREADABLE.value: "error", ReadStatus.NO_RECEIPT.value: "error"}
    rows = ReportService(catalog_db).rows()
    assert {row.issue_codes for row in rows} == {("UNREADABLE",), ("NOT_A_RECEIPT",)}
    # Lo que dice la carpeta se conserva aunque el archivo no se pueda leer.
    assert all(row.payment_period == Period(2026, 3) and row.program_id == 1 for row in rows)


def test_reprocessing_skips_registered_files(catalog_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "entrada"
    pages = write_documents([document(f"{FOLDER}/a.pdf", [synthetic_receipt()])], root, dpi=100, seed=6)
    service = _service(catalog_db, pages)
    service.process_folder(root)
    again = service.process_folder(root)
    assert (again.files_processed, again.files_skipped, again.receipts_created) == (0, 1, 0)
    assert len(ReportService(catalog_db).rows()) == 1


def test_cancel_keeps_processed_files_and_marks_the_batch(catalog_db: Path, tmp_path: Path) -> None:
    # B31: cancelar conserva lo ya procesado y no deja nada a medias.
    root = tmp_path / "entrada"
    docs = [
        document(f"{FOLDER}/{index}.pdf", [synthetic_receipt(folio=500 + index, who=provider())]) for index in range(3)
    ]
    pages = write_documents(docs, root, dpi=100, seed=7)
    cancel = threading.Event()
    messages: list[str] = []

    def progress(_percent: int, message: str) -> None:
        messages.append(message)
        if message.startswith("Archivo 2 de 3"):
            cancel.set()

    result = _service(catalog_db, pages).process_folder(root, progress=progress, cancel=cancel)
    assert result.cancelled
    assert result.files_processed == 1
    assert "cancelado" in result.message()
    assert len(ReportService(catalog_db).rows()) == 1
    batch = _service(catalog_db, pages).last_batch()
    assert batch is not None
    assert batch.status == "cancelled"


def test_unicode_paths_and_folder_program_conflict(catalog_db: Path, tmp_path: Path) -> None:
    # B35 y B22: rutas con tildes y ñ; la carpeta manda y el texto distinto queda como conflicto.
    root = tmp_path / "Revisión año 2026"
    gloss = ("SERVICIOS PROFESIONALES APOYO FAMILIAR, MES DE FEBRERO 2026,", "22 HRS. SEMANALES")
    doc = document(f"{FOLDER}/Muñoz Peña B 245.pdf", [synthetic_receipt(gloss=gloss)])
    pages = write_documents([doc], root, dpi=100, seed=8)
    _service(catalog_db, pages).process_folder(root)
    row = ReportService(catalog_db).rows()[0]
    assert row.program_name == "Programa de Atención Comunitaria"
    assert row.issue_codes == ("PROGRAM_CONFLICT",)
    assert row.status is ReceiptStatus.PENDING


def test_program_from_text_when_folder_has_no_code(catalog_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "entrada"
    doc = document("sin convención/boleta.pdf", [synthetic_receipt()])
    pages = write_documents([doc], root, dpi=100, seed=9)
    _service(catalog_db, pages).process_folder(root)
    row = ReportService(catalog_db).rows()[0]
    record = ReviewService(catalog_db).detail(row.id).record
    assert record.data.program_id == 1
    assert record.traces["program_id"].source is FieldSource.NATIVE
    assert record.data.payment_period is None


def test_receipts_from_another_issuer_with_same_folio_are_not_duplicates(catalog_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "entrada"
    other = provider(rut=OTHER_ISSUER_RUT, name="Tomás Ignacio Reyes Muñoz")
    docs = [
        document(f"{FOLDER}/a.pdf", [synthetic_receipt()]),
        document(f"{FOLDER}/b.pdf", [synthetic_receipt(who=other)]),
    ]
    pages = write_documents(docs, root, dpi=100, seed=10)
    result = _service(catalog_db, pages).process_folder(root)
    assert result.status_counts == {ReceiptStatus.APPROVED: 2}
    assert ReportService(catalog_db).overview(ReceiptFilter()).valid.count == 2


def test_analyze_file_does_not_register(catalog_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "entrada"
    pages = write_documents([document(f"{FOLDER}/a.pdf", [None, synthetic_receipt()])], root, dpi=100, seed=11)
    analysis = _service(catalog_db, pages).analyze_file(root / FOLDER / "a.pdf", root)
    assert analysis.page_count == 2
    assert [page.index for page in analysis.receipt_pages] == [1]
    assert analysis.receipt_pages[0].issues == ()
    assert not ReportService(catalog_db).rows()
