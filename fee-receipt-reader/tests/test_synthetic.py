"""Generador sintético: calibración, reproducibilidad y base de ejemplo."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from receipt_reader.data.synthetic import (
    DEMO_PAYMENT_PERIODS,
    ORGANIZATION_RUT,
    DocFormat,
    build_demo_lot,
    build_sample_set,
)
from receipt_reader.data.synthetic_render import WATERMARK, render_document
from receipt_reader.domain.retention import LEGAL_RATES_BP, RetentionTable, expected_retention
from receipt_reader.domain.rut import is_valid_rut
from receipt_reader.paths import AppPaths
from receipt_reader.services.demo import create_demo_database, ensure_database, remove_database
from support import DemoEnvironment, table_dump


def _hashes(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_lot_is_calibrated() -> None:
    lot = build_demo_lot()
    assert 12 <= len(lot.providers) <= 15
    for provider in lot.providers:
        body = int(provider.rut.split("-")[0])
        assert 40_000_000 <= body <= 45_999_999
        assert is_valid_rut(provider.rut)
    assert len({provider.rut for provider in lot.providers}) == len(lot.providers)
    assert is_valid_rut(ORGANIZATION_RUT)
    assert 6 <= len(DEMO_PAYMENT_PERIODS) <= 8
    formats = Counter(document.doc_format for document in lot.documents)
    for doc_format in (
        DocFormat.NATIVE_PDF,
        DocFormat.SCANNED_PDF,
        DocFormat.PACKAGE_PDF,
        DocFormat.PNG,
        DocFormat.JPG,
    ):
        assert formats[doc_format] > 0
    table = RetentionTable(LEGAL_RATES_BP)
    for document in lot.documents:
        receipt = document.receipt
        if receipt is None or "tasa_otro_anio" in receipt.tags:
            continue
        assert receipt.rate_bp == table.rate_for(receipt.issue_date.year)
        assert receipt.retention == expected_retention(receipt.gross, receipt.rate_bp)
        assert receipt.net == receipt.gross - receipt.retention
        assert 100_000 <= receipt.gross <= 2_000_000


def test_lot_and_samples_are_reproducible() -> None:
    assert build_demo_lot() == build_demo_lot()
    assert build_sample_set() == build_sample_set()
    assert build_demo_lot(seed=7) != build_demo_lot()
    assert 10 <= len(build_sample_set()) <= 15


def test_rendering_is_byte_identical() -> None:
    lot = build_demo_lot()
    by_format = {document.doc_format: document for document in reversed(lot.documents)}
    for doc_format, document in by_format.items():
        first = render_document(document, dpi=60, seed=11)
        second = render_document(document, dpi=60, seed=11)
        assert first[0] == second[0], doc_format
        assert first[1] == second[1], doc_format


def test_synthetic_documents_carry_the_watermark() -> None:
    lot = build_demo_lot()
    native = next(document for document in lot.documents if document.doc_format is DocFormat.NATIVE_PDF)
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(render_document(native, dpi=60, seed=1)[0])
    try:
        page = pdf[0]
        text = page.get_textpage().get_text_range()
    finally:
        pdf.close()
    assert WATERMARK in text


def test_demo_database_is_deterministic(demo_env: DemoEnvironment, tmp_path: Path) -> None:
    # Misma semilla y mismo reloj: mismos archivos (mismo hash) y el mismo contenido en la base.
    second = create_demo_database(tmp_path / "demo.db", tmp_path / "lote_demo", tmp_path / "muestras")
    assert second.batch.status_counts == demo_env.result.batch.status_counts
    assert _hashes(tmp_path / "lote_demo") == _hashes(demo_env.lot_dir)
    assert _hashes(tmp_path / "muestras") == _hashes(demo_env.samples_dir)
    assert table_dump(tmp_path / "demo.db") == table_dump(demo_env.db_path)


def test_existing_database_is_not_regenerated(tmp_path: Path, demo_env: DemoEnvironment) -> None:
    paths = AppPaths(tmp_path / "datos").ensure()
    paths.db_path.write_bytes(demo_env.db_path.read_bytes())
    before = table_dump(paths.db_path)
    assert ensure_database(paths) is None
    assert table_dump(paths.db_path) == before
    assert not paths.demo_lot_dir.exists()


def test_remove_database_deletes_sqlite_side_files(tmp_path: Path) -> None:
    db_path = tmp_path / "base.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        (tmp_path / f"base.db{suffix}").write_bytes(b"x")
    remove_database(db_path)
    assert list(tmp_path.iterdir()) == []
