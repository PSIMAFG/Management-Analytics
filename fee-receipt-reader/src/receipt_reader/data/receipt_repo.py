"""Repositorio de lotes, archivos, boletas, incidencias, trazas y correcciones.

Los agregados de los informes (totales por programa y período) se calculan
aquí con SQL sobre las boletas válidas, para que la interfaz y el Excel
muestren exactamente las mismas cifras.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from receipt_reader.domain.dates import Period
from receipt_reader.domain.location import LocationHints
from receipt_reader.domain.models import (
    REVIEW_STATUSES,
    VALID_STATUSES,
    Decree,
    FieldSource,
    FieldTrace,
    PeriodAxis,
    ReadStatus,
    ReceiptData,
    ReceiptStatus,
    Severity,
    TextKind,
    WorkdayType,
)
from receipt_reader.domain.money import round_half_up
from receipt_reader.domain.records import (
    BatchInfo,
    Correction,
    FolioPeer,
    ReceiptRecord,
    ReceiptRow,
    SourceFileInfo,
)
from receipt_reader.domain.reporting import NO_PROGRAM_LABEL, ReceiptFilter, StatusCount, SummaryRow, Totals
from receipt_reader.domain.retention import implied_hourly_rate
from receipt_reader.domain.validation import ISSUE_CATALOG, Issue, IssueCode
from receipt_reader.errors import DataError


def _status_list(statuses: Iterable[ReceiptStatus]) -> str:
    """Lista SQL literal de estados (valores fijos del enum, nunca texto del usuario)."""
    return "(" + ", ".join(f"'{status.value}'" for status in sorted(statuses)) + ")"


VALID_SQL = _status_list(VALID_STATUSES)
PERIOD_SQL = {
    PeriodAxis.SERVICE: "r.service_period",
    PeriodAxis.ISSUE: "substr(r.issue_date, 1, 7)",
    PeriodAxis.PAYMENT: "r.payment_period",
}


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_stamp(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _period(text: str | None) -> Period | None:
    return Period.parse(text) if text else None


def _hours_to_minutes(hours: Decimal | None) -> int | None:
    return None if hours is None else round_half_up(hours * 60)


def _minutes_to_hours(minutes: int | None) -> Decimal | None:
    if minutes is None:
        return None
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01")).normalize()


# Lotes


def start_batch(conn: sqlite3.Connection, root_path: str, started_at: datetime, files_found: int) -> int:
    cursor = conn.execute(
        "INSERT INTO batch (root_path, started_at, status, files_found) VALUES (?, ?, 'running', ?)",
        (root_path, stamp(started_at), files_found),
    )
    return int(cursor.lastrowid or 0)


def finish_batch(
    conn: sqlite3.Connection, batch_id: int, *, status: str, finished_at: datetime, processed: int, skipped: int
) -> None:
    conn.execute(
        "UPDATE batch SET status = ?, finished_at = ?, files_processed = ?, files_skipped = ? WHERE id = ?",
        (status, stamp(finished_at), processed, skipped, batch_id),
    )


def last_batch(conn: sqlite3.Connection) -> BatchInfo | None:
    row = conn.execute("SELECT * FROM batch ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return BatchInfo(
        id=row["id"],
        root_path=row["root_path"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_parse_stamp(row["finished_at"]),
        status=row["status"],
        files_found=row["files_found"],
        files_processed=row["files_processed"],
        files_skipped=row["files_skipped"],
    )


# Archivos de origen


def file_already_registered(conn: sqlite3.Connection, sha256: str, path: str) -> bool:
    row = conn.execute("SELECT 1 FROM source_file WHERE sha256 = ? AND path = ?", (sha256, path)).fetchone()
    return row is not None


def find_original_file(conn: sqlite3.Connection, sha256: str) -> SourceFileInfo | None:
    row = conn.execute("SELECT * FROM source_file WHERE sha256 = ? AND duplicate_of IS NULL", (sha256,)).fetchone()
    return None if row is None else _file_from_row(row)


def insert_source_file(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None,
    sha256: str,
    path: str,
    relative_path: str,
    page_count: int,
    read_error: str | None,
    hints: LocationHints,
    duplicate_of: int | None,
    processed_at: datetime,
) -> int:
    cursor = conn.execute(
        """INSERT INTO source_file (batch_id, sha256, path, relative_path, page_count, read_error, payment_period,
               folder_program_code, folio_hint, service_month_hint, duplicate_of, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            sha256,
            path,
            relative_path,
            page_count,
            read_error,
            hints.payment_period.iso() if hints.payment_period else None,
            hints.program_code,
            hints.folio_hint,
            hints.service_month_hint,
            duplicate_of,
            stamp(processed_at),
        ),
    )
    return int(cursor.lastrowid or 0)


def _file_from_row(row: sqlite3.Row, prefix: str = "") -> SourceFileInfo:
    return SourceFileInfo(
        id=row[f"{prefix}id"],
        sha256=row[f"{prefix}sha256"],
        path=row[f"{prefix}path"],
        relative_path=row[f"{prefix}relative_path"],
        page_count=row[f"{prefix}page_count"],
        read_error=row[f"{prefix}read_error"],
        payment_period=_period(row[f"{prefix}payment_period"]),
        folder_program_code=row[f"{prefix}folder_program_code"],
        folio_hint=row[f"{prefix}folio_hint"],
        service_month_hint=row[f"{prefix}service_month_hint"],
        duplicate_of=row[f"{prefix}duplicate_of"],
        processed_at=_parse_stamp(row[f"{prefix}processed_at"]),
    )


def get_source_file(conn: sqlite3.Connection, file_id: int) -> SourceFileInfo:
    row = conn.execute("SELECT * FROM source_file WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise DataError(f"No existe el archivo de origen {file_id}.")
    return _file_from_row(row)


# Boletas


def _data_columns(data: ReceiptData) -> dict[str, Any]:
    return {
        "issuer_rut": data.issuer_rut,
        "issuer_name": data.issuer_name,
        "receiver_rut": data.receiver_rut,
        "receiver_name": data.receiver_name,
        "folio": data.folio,
        "issue_date": data.issue_date.isoformat() if data.issue_date else None,
        "service_period": data.service_period.iso() if data.service_period else None,
        "payment_period": data.payment_period.iso() if data.payment_period else None,
        "gross": data.gross,
        "retention": data.retention,
        "net": data.net,
        "printed_rate_bp": data.printed_rate_bp,
        "program_id": data.program_id,
        "hours_minutes": _hours_to_minutes(data.hours),
        "workday_type": data.workday_type.value if data.workday_type else None,
        "decree_number": data.decree.number if data.decree else None,
        "decree_year": data.decree.year if data.decree else None,
        "gloss": data.gloss,
    }


def _data_from_row(row: sqlite3.Row) -> ReceiptData:
    decree = Decree(row["decree_number"], row["decree_year"]) if row["decree_number"] else None
    return ReceiptData(
        issuer_rut=row["issuer_rut"],
        issuer_name=row["issuer_name"],
        receiver_rut=row["receiver_rut"],
        receiver_name=row["receiver_name"],
        folio=row["folio"],
        issue_date=date.fromisoformat(row["issue_date"]) if row["issue_date"] else None,
        service_period=_period(row["service_period"]),
        payment_period=_period(row["payment_period"]),
        gross=row["gross"],
        retention=row["retention"],
        net=row["net"],
        printed_rate_bp=row["printed_rate_bp"],
        program_id=row["program_id"],
        hours=_minutes_to_hours(row["hours_minutes"]),
        workday_type=WorkdayType(row["workday_type"]) if row["workday_type"] else None,
        decree=decree,
        gloss=row["gloss"],
    )


def insert_receipt(
    conn: sqlite3.Connection,
    *,
    source_file_id: int,
    page_index: int,
    text_kind: TextKind,
    read_status: ReadStatus,
    data: ReceiptData,
    folder_program_id: int | None,
    text_program_id: int | None,
    text_program_ambiguous: bool,
    ocr_confidence: float | None,
    status: ReceiptStatus,
    created_at: datetime,
) -> int:
    columns = _data_columns(data) | {
        "source_file_id": source_file_id,
        "page_index": page_index,
        "text_kind": text_kind.value,
        "read_status": read_status.value,
        "folder_program_id": folder_program_id,
        "text_program_id": text_program_id,
        "text_program_ambiguous": int(text_program_ambiguous),
        "ocr_confidence": None if ocr_confidence is None else round(ocr_confidence, 4),
        "status": status.value,
        "created_at": stamp(created_at),
        "updated_at": stamp(created_at),
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    cursor = conn.execute(f"INSERT INTO receipt ({names}) VALUES ({marks})", tuple(columns.values()))
    return int(cursor.lastrowid or 0)


def update_receipt(
    conn: sqlite3.Connection,
    receipt_id: int,
    *,
    data: ReceiptData,
    status: ReceiptStatus,
    edited: bool,
    accepted_at: datetime | None,
    discard_reason: str | None,
    updated_at: datetime,
) -> None:
    columns = _data_columns(data) | {
        "status": status.value,
        "edited": int(edited),
        "accepted_at": stamp(accepted_at) if accepted_at else None,
        "discard_reason": discard_reason,
        "updated_at": stamp(updated_at),
    }
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE receipt SET {assignments} WHERE id = ?", (*columns.values(), receipt_id))


def set_status(conn: sqlite3.Connection, receipt_id: int, status: ReceiptStatus, updated_at: datetime) -> None:
    conn.execute(
        "UPDATE receipt SET status = ?, updated_at = ? WHERE id = ?", (status.value, stamp(updated_at), receipt_id)
    )


def replace_issues(conn: sqlite3.Connection, receipt_id: int, issues: Iterable[Issue]) -> None:
    conn.execute("DELETE FROM receipt_issue WHERE receipt_id = ?", (receipt_id,))
    conn.executemany(
        "INSERT INTO receipt_issue (receipt_id, code, field, severity, overridable, message) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (receipt_id, issue.code.value, issue.field, issue.severity.value, int(issue.overridable), issue.message)
            for issue in issues
        ],
    )


def save_traces(conn: sqlite3.Connection, receipt_id: int, traces: Mapping[str, FieldTrace]) -> None:
    conn.executemany(
        """INSERT INTO field_extraction (receipt_id, field, value, confidence, source) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(receipt_id, field) DO UPDATE SET value = excluded.value, confidence = excluded.confidence,
               source = excluded.source""",
        [
            (receipt_id, name, trace.value, trace.confidence, trace.source.value)
            for name, trace in sorted(traces.items())
        ],
    )


def replace_acceptance(conn: sqlite3.Connection, receipt_id: int, codes: Iterable[IssueCode]) -> None:
    """Códigos que el usuario aceptó al aprobar (una lista vacía retira la aceptación)."""
    conn.execute("DELETE FROM receipt_acceptance WHERE receipt_id = ?", (receipt_id,))
    conn.executemany(
        "INSERT INTO receipt_acceptance (receipt_id, code) VALUES (?, ?)",
        [(receipt_id, code.value) for code in sorted(set(codes))],
    )


def _accepted_codes_for(conn: sqlite3.Connection, receipt_id: int) -> frozenset[IssueCode]:
    rows = conn.execute("SELECT code FROM receipt_acceptance WHERE receipt_id = ?", (receipt_id,)).fetchall()
    return frozenset(IssueCode(row["code"]) for row in rows)


def _issues_for(conn: sqlite3.Connection, receipt_id: int) -> tuple[Issue, ...]:
    rows = conn.execute(
        "SELECT code, field, severity, overridable, message FROM receipt_issue WHERE receipt_id = ? ORDER BY id",
        (receipt_id,),
    ).fetchall()
    return tuple(
        Issue(IssueCode(row["code"]), row["message"], Severity(row["severity"]), bool(row["overridable"]), row["field"])
        for row in rows
    )


def _traces_for(conn: sqlite3.Connection, receipt_id: int) -> dict[str, FieldTrace]:
    rows = conn.execute(
        "SELECT field, value, confidence, source FROM field_extraction WHERE receipt_id = ?", (receipt_id,)
    ).fetchall()
    return {row["field"]: FieldTrace(row["value"], row["confidence"], FieldSource(row["source"])) for row in rows}


_RECORD_SQL = """
SELECT r.*, f.id AS f_id, f.sha256 AS f_sha256, f.path AS f_path, f.relative_path AS f_relative_path,
       f.page_count AS f_page_count, f.read_error AS f_read_error, f.payment_period AS f_payment_period,
       f.folder_program_code AS f_folder_program_code, f.folio_hint AS f_folio_hint,
       f.service_month_hint AS f_service_month_hint, f.duplicate_of AS f_duplicate_of,
       f.processed_at AS f_processed_at
FROM receipt r JOIN source_file f ON f.id = r.source_file_id
"""


def load_receipt(conn: sqlite3.Connection, receipt_id: int) -> ReceiptRecord:
    row = conn.execute(_RECORD_SQL + " WHERE r.id = ?", (receipt_id,)).fetchone()
    if row is None:
        raise DataError(f"No existe la boleta {receipt_id}.")
    return ReceiptRecord(
        id=row["id"],
        file=_file_from_row(row, "f_"),
        page_index=row["page_index"],
        read_status=ReadStatus(row["read_status"]),
        text_kind=TextKind(row["text_kind"]),
        data=_data_from_row(row),
        status=ReceiptStatus(row["status"]),
        traces=_traces_for(conn, receipt_id),
        issues=_issues_for(conn, receipt_id),
        folder_program_id=row["folder_program_id"],
        text_program_id=row["text_program_id"],
        text_program_ambiguous=bool(row["text_program_ambiguous"]),
        ocr_confidence=row["ocr_confidence"],
        discard_reason=row["discard_reason"],
        accepted_at=_parse_stamp(row["accepted_at"]),
        accepted_codes=_accepted_codes_for(conn, receipt_id),
        edited=bool(row["edited"]),
        created_at=_parse_stamp(row["created_at"]),
        updated_at=_parse_stamp(row["updated_at"]),
    )


def active_receipt_ids(conn: sqlite3.Connection) -> list[int]:
    """Boletas no descartadas (las que se revalidan al cambiar los parámetros)."""
    rows = conn.execute("SELECT id FROM receipt WHERE status <> ? ORDER BY id", (ReceiptStatus.DISCARDED.value,))
    return [row["id"] for row in rows]


def receipt_ids_with_key(conn: sqlite3.Connection, issuer_rut: str | None, folio: int | None) -> list[int]:
    if not issuer_rut or folio is None:
        return []
    rows = conn.execute("SELECT id FROM receipt WHERE issuer_rut = ? AND folio = ? ORDER BY id", (issuer_rut, folio))
    return [row["id"] for row in rows]


def folio_peer(
    conn: sqlite3.Connection,
    receipt_id: int | None,
    issuer_rut: str | None,
    folio: int | None,
    *,
    self_valid: bool = False,
) -> FolioPeer | None:
    """Otra boleta no descartada con el mismo emisor y folio que tiene precedencia sobre esta.

    Tiene precedencia una boleta válida (aprobada o corregida). Si no hay ninguna válida y
    esta tampoco lo es, la precedencia es de la registrada antes (id menor). Una boleta que
    aún no se registra (`receipt_id` None) cede ante cualquier otra. Así, corregir una boleta
    nunca deja pendiente a otra que ya estaba aprobada.
    """
    if not issuer_rut or folio is None:
        return None
    sql = (
        "SELECT r.id, r.issue_date, r.gross, f.relative_path "
        "FROM receipt r JOIN source_file f ON f.id = r.source_file_id "
        "WHERE r.issuer_rut = ? AND r.folio = ? AND r.status <> ?"
    )
    params: list[Any] = [issuer_rut, folio, ReceiptStatus.DISCARDED.value]
    if receipt_id is not None:
        sql += " AND r.id <> ?"
        params.append(receipt_id)
        if self_valid:
            sql += f" AND r.status IN {VALID_SQL}"
        else:
            sql += f" AND (r.status IN {VALID_SQL} OR r.id < ?)"
            params.append(receipt_id)
    row = conn.execute(sql + f" ORDER BY r.status IN {VALID_SQL} DESC, r.id LIMIT 1", params).fetchone()
    if row is None:
        return None
    issued = date.fromisoformat(row["issue_date"]) if row["issue_date"] else None
    return FolioPeer(row["id"], issued, row["gross"], row["relative_path"])


def valid_program_ids_for_rut(conn: sqlite3.Connection, issuer_rut: str, exclude_id: int | None = None) -> list[int]:
    rows = conn.execute(
        f"SELECT program_id FROM receipt WHERE issuer_rut = ? AND status IN {VALID_SQL} AND program_id IS NOT NULL "
        "AND id <> ? ORDER BY id",
        (issuer_rut, exclude_id or -1),
    )
    return [row["program_id"] for row in rows]


def add_corrections(conn: sqlite3.Connection, corrections: Iterable[Correction]) -> None:
    conn.executemany(
        "INSERT INTO correction (receipt_id, field, old_value, new_value, corrected_at) VALUES (?, ?, ?, ?, ?)",
        [(c.receipt_id, c.field, c.old_value, c.new_value, stamp(c.corrected_at)) for c in corrections],
    )


def list_corrections(conn: sqlite3.Connection, receipt_id: int | None = None) -> list[Correction]:
    sql = "SELECT receipt_id, field, old_value, new_value, corrected_at FROM correction"
    params: tuple[Any, ...] = ()
    if receipt_id is not None:
        sql += " WHERE receipt_id = ?"
        params = (receipt_id,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [
        Correction(
            row["receipt_id"],
            row["field"],
            row["old_value"],
            row["new_value"],
            datetime.fromisoformat(row["corrected_at"]),
        )
        for row in rows
    ]


# Consultas para vistas e informes


def _where(filt: ReceiptFilter, *, valid_only: bool) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if valid_only:
        clauses.append(f"r.status IN {VALID_SQL}")
    elif filt.statuses:
        marks = ", ".join("?" for _ in filt.statuses)
        clauses.append(f"r.status IN ({marks})")
        params += sorted(status.value for status in filt.statuses)
    if filt.program_id is not None:
        clauses.append("r.program_id = ?")
        params.append(filt.program_id)
    if filt.period is not None:
        clauses.append(f"{PERIOD_SQL[filt.axis]} = ?")
        params.append(filt.period.iso())
    if filt.issue_code:
        clauses.append("EXISTS (SELECT 1 FROM receipt_issue i WHERE i.receipt_id = r.id AND i.code = ?)")
        params.append(filt.issue_code)
    if filt.text:
        pattern = f"%{filt.text.strip()}%"
        clauses.append("(r.issuer_name LIKE ? OR r.issuer_rut LIKE ? OR f.relative_path LIKE ?)")
        params += [pattern, pattern.replace(".", ""), pattern]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


_ROWS_SQL = """
SELECT r.id, r.status, r.page_index, r.text_kind, r.issuer_rut, r.issuer_name, r.receiver_rut, r.folio,
       r.issue_date, r.service_period, r.payment_period, r.program_id, p.name AS program_name, r.gross,
       r.retention, r.net, r.printed_rate_bp, r.hours_minutes, r.workday_type, r.decree_number, r.decree_year,
       r.ocr_confidence, r.discard_reason, f.path, f.relative_path, f.sha256,
       (SELECT group_concat(code, ',') FROM (SELECT code FROM receipt_issue i WHERE i.receipt_id = r.id ORDER BY i.id))
           AS issue_codes,
       (SELECT count(*) FROM receipt_issue i WHERE i.receipt_id = r.id AND i.severity = 'blocking') AS blocking_count,
       (SELECT count(*) FROM receipt_issue i WHERE i.receipt_id = r.id AND i.severity = 'warning') AS warning_count
FROM receipt r
JOIN source_file f ON f.id = r.source_file_id
LEFT JOIN program p ON p.id = r.program_id
"""


def _row_from_sql(row: sqlite3.Row) -> ReceiptRow:
    codes = tuple(code for code in (row["issue_codes"] or "").split(",") if code)
    hours = _minutes_to_hours(row["hours_minutes"])
    workday = WorkdayType(row["workday_type"]) if row["workday_type"] else None
    decree = Decree(row["decree_number"], row["decree_year"]) if row["decree_number"] else None
    return ReceiptRow(
        id=row["id"],
        status=ReceiptStatus(row["status"]),
        file_name=row["path"].replace("\\", "/").rsplit("/", 1)[-1],
        relative_path=row["relative_path"],
        page_index=row["page_index"],
        text_kind=TextKind(row["text_kind"]),
        issuer_rut=row["issuer_rut"],
        issuer_name=row["issuer_name"],
        receiver_rut=row["receiver_rut"],
        folio=row["folio"],
        issue_date=date.fromisoformat(row["issue_date"]) if row["issue_date"] else None,
        service_period=_period(row["service_period"]),
        payment_period=_period(row["payment_period"]),
        program_id=row["program_id"],
        program_name=row["program_name"],
        gross=row["gross"],
        retention=row["retention"],
        net=row["net"],
        printed_rate_bp=row["printed_rate_bp"],
        hours=hours,
        workday_type=workday,
        decree_label=decree.label() if decree else None,
        ocr_confidence=row["ocr_confidence"],
        discard_reason=row["discard_reason"],
        issue_codes=codes,
        issue_titles=tuple(ISSUE_CATALOG[IssueCode(code)].title for code in codes),
        blocking_count=row["blocking_count"],
        warning_count=row["warning_count"],
        sha256=row["sha256"],
        hourly_rate=implied_hourly_rate(row["gross"], hours, workday.weeks_factor if workday else None),
    )


def list_rows(conn: sqlite3.Connection, filt: ReceiptFilter, *, valid_only: bool = False) -> list[ReceiptRow]:
    where, params = _where(filt, valid_only=valid_only)
    rows = conn.execute(_ROWS_SQL + where + " ORDER BY r.id", params).fetchall()
    return [_row_from_sql(row) for row in rows]


def summary_rows(conn: sqlite3.Connection, filt: ReceiptFilter) -> list[SummaryRow]:
    """Totales de boletas válidas por programa y período (según el eje del filtro)."""
    where, params = _where(filt, valid_only=True)
    period = PERIOD_SQL[filt.axis]
    sql = f"""
        SELECT r.program_id, coalesce(p.name, ?) AS program_name, {period} AS period, count(*) AS n,
               coalesce(sum(r.gross), 0) AS gross, coalesce(sum(r.retention), 0) AS retention,
               coalesce(sum(r.net), 0) AS net
        FROM receipt r JOIN source_file f ON f.id = r.source_file_id LEFT JOIN program p ON p.id = r.program_id
        {where}
        GROUP BY r.program_id, period
        ORDER BY program_name, period IS NULL, period
    """
    rows = conn.execute(sql, [NO_PROGRAM_LABEL, *params]).fetchall()
    return [
        SummaryRow(
            row["program_id"],
            row["program_name"],
            _period(row["period"]),
            row["n"],
            row["gross"],
            row["retention"],
            row["net"],
        )
        for row in rows
    ]


def program_totals(conn: sqlite3.Connection, filt: ReceiptFilter) -> list[SummaryRow]:
    where, params = _where(filt, valid_only=True)
    sql = f"""
        SELECT r.program_id, coalesce(p.name, ?) AS program_name, count(*) AS n,
               coalesce(sum(r.gross), 0) AS gross, coalesce(sum(r.retention), 0) AS retention,
               coalesce(sum(r.net), 0) AS net
        FROM receipt r JOIN source_file f ON f.id = r.source_file_id LEFT JOIN program p ON p.id = r.program_id
        {where}
        GROUP BY r.program_id ORDER BY program_name
    """
    rows = conn.execute(sql, [NO_PROGRAM_LABEL, *params]).fetchall()
    return [
        SummaryRow(row["program_id"], row["program_name"], None, row["n"], row["gross"], row["retention"], row["net"])
        for row in rows
    ]


def grand_totals(conn: sqlite3.Connection, filt: ReceiptFilter) -> Totals:
    where, params = _where(filt, valid_only=True)
    row = conn.execute(
        "SELECT count(*) AS n, coalesce(sum(r.gross), 0) AS gross, coalesce(sum(r.retention), 0) AS retention, "
        "coalesce(sum(r.net), 0) AS net FROM receipt r JOIN source_file f ON f.id = r.source_file_id" + where,
        params,
    ).fetchone()
    return Totals(row["n"], row["gross"], row["retention"], row["net"])


def status_counts(conn: sqlite3.Connection, filt: ReceiptFilter) -> list[StatusCount]:
    base = ReceiptFilter(
        program_id=filt.program_id, period=filt.period, axis=filt.axis, issue_code=filt.issue_code, text=filt.text
    )
    where, params = _where(base, valid_only=False)
    rows = conn.execute(
        "SELECT r.status, count(*) AS n FROM receipt r JOIN source_file f ON f.id = r.source_file_id"
        + where
        + " GROUP BY r.status",
        params,
    ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    return [StatusCount(status, counts.get(status.value, 0)) for status in ReceiptStatus]


def distinct_periods(conn: sqlite3.Connection, axis: PeriodAxis) -> list[Period]:
    rows = conn.execute(
        f"SELECT DISTINCT {PERIOD_SQL[axis]} AS period FROM receipt r WHERE {PERIOD_SQL[axis]} IS NOT NULL "
        "ORDER BY period"
    ).fetchall()
    return [Period.parse(row["period"]) for row in rows]


def issue_code_counts(conn: sqlite3.Connection, filt: ReceiptFilter) -> dict[str, int]:
    """Boletas por revisar con cada incidencia, con los filtros de programa, período y eje de la cola."""
    base = ReceiptFilter(
        program_id=filt.program_id, statuses=REVIEW_STATUSES, period=filt.period, axis=filt.axis, text=filt.text
    )
    where, params = _where(base, valid_only=False)
    rows = conn.execute(
        "SELECT i.code, count(DISTINCT i.receipt_id) AS n FROM receipt_issue i JOIN receipt r ON r.id = i.receipt_id "
        "JOIN source_file f ON f.id = r.source_file_id" + where + " GROUP BY i.code",
        params,
    ).fetchall()
    return {row["code"]: row["n"] for row in rows}


def all_traces(conn: sqlite3.Connection) -> dict[int, dict[str, FieldTrace]]:
    result: dict[int, dict[str, FieldTrace]] = {}
    for row in conn.execute("SELECT receipt_id, field, value, confidence, source FROM field_extraction"):
        result.setdefault(row["receipt_id"], {})[row["field"]] = FieldTrace(
            row["value"], row["confidence"], FieldSource(row["source"])
        )
    return result


def all_issue_messages(conn: sqlite3.Connection) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for row in conn.execute("SELECT receipt_id, message FROM receipt_issue ORDER BY id"):
        result.setdefault(row["receipt_id"], []).append(row["message"])
    return result
