"""Persistencia: esquema, restricciones, repositorios y transacciones."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from receipt_reader.data import catalog_repo, db, receipt_repo
from receipt_reader.domain.dates import Period
from receipt_reader.domain.location import LocationHints
from receipt_reader.domain.models import (
    Decree,
    FieldSource,
    FieldTrace,
    ReadStatus,
    ReceiptData,
    ReceiptStatus,
    Settings,
    TextKind,
    WorkdayType,
)
from receipt_reader.domain.records import Correction
from receipt_reader.domain.validation import IssueCode, make_issue
from receipt_reader.errors import DataError

NOW = datetime(2026, 7, 20, 10, 0)
DATA = ReceiptData(
    issuer_rut="41234567-3",
    issuer_name="Camila Andrea Rojas Soto",
    receiver_rut="45210804-6",
    receiver_name="ORGANIZACIÓN EJEMPLO",
    folio=245,
    issue_date=date(2026, 3, 4),
    service_period=Period(2026, 2),
    payment_period=Period(2026, 3),
    gross=720_000,
    retention=109_800,
    net=610_200,
    printed_rate_bp=1525,
    program_id=1,
    hours=Decimal("12.5"),
    workday_type=WorkdayType.WEEKLY,
    decree=Decree(1234, 2026),
    gloss="LÍNEA UNO\nLÍNEA DOS",
)


def _file(conn: sqlite3.Connection, sha: str = "a" * 64, path: str = "C:/lote/a.pdf") -> int:
    return receipt_repo.insert_source_file(
        conn,
        batch_id=None,
        sha256=sha,
        path=path,
        relative_path="2026-03/110 Programa/a.pdf",
        page_count=2,
        read_error=None,
        hints=LocationHints(payment_period=Period(2026, 3), program_code="110", folio_hint=245),
        duplicate_of=None,
        processed_at=NOW,
    )


def _receipt(
    conn: sqlite3.Connection, file_id: int, *, page: int = 0, status: ReceiptStatus = ReceiptStatus.APPROVED
) -> int:
    return receipt_repo.insert_receipt(
        conn,
        source_file_id=file_id,
        page_index=page,
        text_kind=TextKind.NATIVE,
        read_status=ReadStatus.OK,
        data=DATA,
        folder_program_id=1,
        text_program_id=1,
        text_program_ambiguous=False,
        ocr_confidence=None,
        status=status,
        created_at=NOW,
    )


def test_schema_version_and_foreign_keys(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.initialize(conn) is False
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    expected = {
        "setting",
        "program",
        "program_alias",
        "retention_rate",
        "reference_rate",
        "provider",
        "batch",
        "source_file",
        "receipt",
        "receipt_issue",
        "field_extraction",
        "correction",
    }
    assert expected <= tables


def test_other_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "vieja.db"
    with db.session(path) as conn:
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(DataError, match="versión de esquema"):
            db.initialize(conn)


def test_catalog_seed(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn:
        programs = catalog_repo.list_programs(conn)
        assert [program.folder_code for program in programs] == ["110", "220", "330", "440"]
        assert catalog_repo.load_retention_table(conn).rate_for(2025) == 1450
        assert len(catalog_repo.list_reference_rates(conn)) == 8
        assert catalog_repo.list_aliases(conn)
        settings = catalog_repo.load_settings(conn)
    assert settings.organization_name == "Organización Ejemplo"
    assert settings.date_window_start < settings.date_window_end


def test_settings_round_trip(catalog_db: Path) -> None:
    changed = Settings(organization_rut="45210804-6", date_window_start=date(2024, 1, 1), ocr_min_confidence=0.7)
    with db.session(catalog_db) as conn, db.transaction(conn):
        catalog_repo.save_settings(conn, changed)
    with db.session(catalog_db) as conn:
        assert catalog_repo.load_settings(conn) == changed


def test_receipt_round_trip_keeps_types(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn, db.transaction(conn):
        receipt_id = _receipt(conn, _file(conn))
        traces = {"gross": FieldTrace("720.000", 0.98, FieldSource.OCR)}
        receipt_repo.save_traces(conn, receipt_id, traces)
        issue = make_issue(IssueCode.RATE_MISSING, "Sin tasa.")
        receipt_repo.replace_issues(conn, receipt_id, [issue])
    with db.session(catalog_db) as conn:
        record = receipt_repo.load_receipt(conn, receipt_id)
        hours_minutes = conn.execute("SELECT hours_minutes FROM receipt").fetchone()[0]
    assert record.data == DATA
    assert hours_minutes == 750  # las horas se guardan como minutos enteros
    assert record.traces == traces
    assert record.issues == (issue,)
    assert record.file.payment_period == Period(2026, 3)
    assert record.file.folio_hint == 245


def test_replacing_issues_never_leaves_a_stale_one_behind(catalog_db: Path) -> None:
    # B24: el original perdía el motivo de revisión en una clave aparte y podía quedar obsoleto
    # tras una reevaluación incremental. Aquí una nueva lista de incidencias reemplaza a la
    # anterior por completo: no queda ninguna incidencia resuelta colgando.
    with db.session(catalog_db) as conn, db.transaction(conn):
        receipt_id = _receipt(conn, _file(conn))
        first_pass = [make_issue(IssueCode.GROSS_MISSING, "Falta el bruto."), make_issue(IssueCode.RATE_MISSING, "x")]
        receipt_repo.replace_issues(conn, receipt_id, first_pass)
    with db.session(catalog_db) as conn:
        assert {issue.code for issue in receipt_repo.load_receipt(conn, receipt_id).issues} == {
            IssueCode.GROSS_MISSING,
            IssueCode.RATE_MISSING,
        }
    with db.session(catalog_db) as conn, db.transaction(conn):
        receipt_repo.replace_issues(conn, receipt_id, [make_issue(IssueCode.RATE_MISSING, "x")])
    with db.session(catalog_db) as conn:
        record = receipt_repo.load_receipt(conn, receipt_id)
    assert [issue.code for issue in record.issues] == [IssueCode.RATE_MISSING]


def test_valid_receipts_cannot_share_issuer_and_folio(catalog_db: Path) -> None:
    # B40: restricción de unicidad (RUT emisor, folio) entre boletas válidas.
    with db.session(catalog_db) as conn:
        first = _file(conn, "a" * 64, "C:/lote/a.pdf")
        second = _file(conn, "b" * 64, "C:/lote/b.pdf")
        _receipt(conn, first)
        _receipt(conn, second, status=ReceiptStatus.PENDING)
        with pytest.raises(sqlite3.IntegrityError):
            _receipt(conn, second, page=1)
        conn.rollback()


def test_same_hash_only_once_as_original(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn:
        _file(conn, "c" * 64, "C:/lote/c.pdf")
        with pytest.raises(sqlite3.IntegrityError):
            _file(conn, "c" * 64, "C:/otra/c.pdf")
        conn.rollback()


def test_discarded_requires_a_reason(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn:
        receipt_id = _receipt(conn, _file(conn))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE receipt SET status = 'discarded' WHERE id = ?", (receipt_id,))
        conn.rollback()


def test_check_constraints_reject_bad_values(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn:
        receipt_id = _receipt(conn, _file(conn))
        for sql in (
            "UPDATE receipt SET issue_date = '04/03/2026' WHERE id = ?",
            "UPDATE receipt SET gross = -1 WHERE id = ?",
            "UPDATE receipt SET status = 'otro' WHERE id = ?",
            "UPDATE receipt SET program_id = 999 WHERE id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (receipt_id,))
        conn.rollback()


def test_transaction_rolls_back_on_error(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn:
        with pytest.raises(RuntimeError), db.transaction(conn):
            _file(conn)
            raise RuntimeError("falla simulada")
        assert conn.execute("SELECT count(*) FROM source_file").fetchone()[0] == 0


def test_corrections_are_listed_in_order(catalog_db: Path) -> None:
    with db.session(catalog_db) as conn, db.transaction(conn):
        receipt_id = _receipt(conn, _file(conn))
        receipt_repo.add_corrections(
            conn,
            [
                Correction(receipt_id, "gross", None, "720000", NOW),
                Correction(receipt_id, "status", "pending", "corrected", NOW),
            ],
        )
    with db.session(catalog_db) as conn:
        found = receipt_repo.list_corrections(conn, receipt_id)
    assert [(c.field, c.old_value, c.new_value) for c in found] == [
        ("gross", None, "720000"),
        ("status", "pending", "corrected"),
    ]


def test_summary_is_computed_in_sql_over_valid_receipts(catalog_db: Path) -> None:
    from receipt_reader.domain.reporting import ReceiptFilter

    with db.session(catalog_db) as conn, db.transaction(conn):
        file_id = _file(conn)
        _receipt(conn, file_id)
        pending = receipt_repo.insert_receipt(
            conn,
            source_file_id=file_id,
            page_index=1,
            text_kind=TextKind.OCR,
            read_status=ReadStatus.OK,
            data=DATA.with_changes(folio=246, gross=999_000),
            folder_program_id=1,
            text_program_id=None,
            text_program_ambiguous=False,
            ocr_confidence=0.9,
            status=ReceiptStatus.PENDING,
            created_at=NOW,
        )
        assert pending > 0
    with db.session(catalog_db) as conn:
        totals = receipt_repo.grand_totals(conn, ReceiptFilter())
        rows = receipt_repo.summary_rows(conn, ReceiptFilter())
        counts = {item.status: item.count for item in receipt_repo.status_counts(conn, ReceiptFilter())}
    assert (totals.count, totals.gross, totals.retention, totals.net) == (1, 720_000, 109_800, 610_200)
    assert [(row.program_name, row.period, row.gross) for row in rows] == [
        ("Programa de Atención Comunitaria", Period(2026, 2), 720_000)
    ]
    assert counts[ReceiptStatus.PENDING] == 1
    assert counts[ReceiptStatus.APPROVED] == 1
