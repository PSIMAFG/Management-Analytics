"""Repositorio de catálogos y parámetros: programas, alias, tasas, prestadores y configuración."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import fields
from datetime import date, datetime

from receipt_reader.domain.models import Program, ProgramAlias, Provider, ReferenceRate, Settings
from receipt_reader.domain.retention import RetentionTable
from receipt_reader.errors import DataError

_SETTING_TYPES = {f.name: f.type for f in fields(Settings)}


def _setting_to_text(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _setting_from_text(name: str, text: str) -> object:
    kind = str(_SETTING_TYPES[name])
    if kind == "date":
        return date.fromisoformat(text)
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


def load_settings(conn: sqlite3.Connection) -> Settings:
    rows = conn.execute("SELECT key, value FROM setting").fetchall()
    values = {row["key"]: _setting_from_text(row["key"], row["value"]) for row in rows if row["key"] in _SETTING_TYPES}
    return Settings(**values)


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    for item in fields(Settings):
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (item.name, _setting_to_text(getattr(settings, item.name))),
        )


def _program_row(row: sqlite3.Row) -> Program:
    return Program(row["id"], row["folder_code"], row["name"], row["short_name"], bool(row["active"]))


def list_programs(conn: sqlite3.Connection, *, active_only: bool = False) -> list[Program]:
    query = "SELECT id, folder_code, name, short_name, active FROM program"
    if active_only:
        query += " WHERE active = 1"
    rows = conn.execute(query + " ORDER BY folder_code").fetchall()
    return [_program_row(row) for row in rows]


def get_program(conn: sqlite3.Connection, program_id: int) -> Program | None:
    row = conn.execute(
        "SELECT id, folder_code, name, short_name, active FROM program WHERE id = ?", (program_id,)
    ).fetchone()
    return None if row is None else _program_row(row)


def insert_program(conn: sqlite3.Connection, folder_code: str, name: str, short_name: str) -> int:
    try:
        cursor = conn.execute(
            "INSERT INTO program (folder_code, name, short_name) VALUES (?, ?, ?)", (folder_code, name, short_name)
        )
    except sqlite3.IntegrityError as error:
        raise DataError(f"Ya existe un programa con el código {folder_code} o el nombre '{name}'.") from error
    return int(cursor.lastrowid or 0)


def update_program(
    conn: sqlite3.Connection, program_id: int, folder_code: str, name: str, short_name: str, *, active: bool
) -> None:
    try:
        cursor = conn.execute(
            "UPDATE program SET folder_code = ?, name = ?, short_name = ?, active = ? WHERE id = ?",
            (folder_code, name, short_name, int(active), program_id),
        )
    except sqlite3.IntegrityError as error:
        raise DataError(f"Ya existe un programa con el código {folder_code} o el nombre '{name}'.") from error
    if cursor.rowcount == 0:
        raise DataError(f"No existe el programa {program_id}.")


def set_program_active(conn: sqlite3.Connection, program_id: int, active: bool) -> None:
    cursor = conn.execute("UPDATE program SET active = ? WHERE id = ?", (int(active), program_id))
    if cursor.rowcount == 0:
        raise DataError(f"No existe el programa {program_id}.")


def list_aliases(conn: sqlite3.Connection, *, active_only: bool = False) -> list[ProgramAlias]:
    query = "SELECT program_alias.program_id, alias, priority FROM program_alias"
    if active_only:
        query += " JOIN program ON program.id = program_alias.program_id AND program.active = 1"
    rows = conn.execute(query + " ORDER BY priority, alias").fetchall()
    return [ProgramAlias(row["program_id"], row["alias"], row["priority"]) for row in rows]


def insert_alias(conn: sqlite3.Connection, alias: ProgramAlias) -> None:
    try:
        conn.execute(
            "INSERT INTO program_alias (program_id, alias, priority) VALUES (?, ?, ?)",
            (alias.program_id, alias.alias, alias.priority),
        )
    except sqlite3.IntegrityError as error:
        raise DataError(f"Ya existe el alias '{alias.alias}'.") from error


def delete_alias(conn: sqlite3.Connection, program_id: int, alias: str) -> None:
    cursor = conn.execute("DELETE FROM program_alias WHERE program_id = ? AND alias = ?", (program_id, alias))
    if cursor.rowcount == 0:
        raise DataError(f"No existe el alias '{alias}' del programa {program_id}.")


def upsert_program(conn: sqlite3.Connection, folder_code: str, name: str, short_name: str, *, active: bool) -> int:
    """Crea el programa del código de carpeta o actualiza el existente (usado al importar)."""
    row = conn.execute("SELECT id FROM program WHERE folder_code = ?", (folder_code,)).fetchone()
    if row is None:
        program_id = insert_program(conn, folder_code, name, short_name)
        if not active:
            set_program_active(conn, program_id, False)
        return program_id
    update_program(conn, row["id"], folder_code, name, short_name, active=active)
    return int(row["id"])


def upsert_alias(conn: sqlite3.Connection, alias: ProgramAlias) -> None:
    """Crea el alias o, si el texto ya existe (es único), lo reasigna al programa importado."""
    conn.execute(
        "INSERT INTO program_alias (program_id, alias, priority) VALUES (?, ?, ?) "
        "ON CONFLICT(alias) DO UPDATE SET program_id = excluded.program_id, priority = excluded.priority",
        (alias.program_id, alias.alias, alias.priority),
    )


def load_retention_table(conn: sqlite3.Connection) -> RetentionTable:
    rows = conn.execute("SELECT year, rate_bp FROM retention_rate").fetchall()
    return RetentionTable({row["year"]: row["rate_bp"] for row in rows})


def upsert_retention_rate(conn: sqlite3.Connection, year: int, rate_bp: int) -> None:
    conn.execute(
        "INSERT INTO retention_rate (year, rate_bp) VALUES (?, ?) "
        "ON CONFLICT(year) DO UPDATE SET rate_bp = excluded.rate_bp",
        (year, rate_bp),
    )


def list_reference_rates(conn: sqlite3.Connection) -> list[ReferenceRate]:
    rows = conn.execute(
        "SELECT program_id, year, min_hourly, max_hourly FROM reference_rate ORDER BY program_id, year"
    ).fetchall()
    return [ReferenceRate(row["program_id"], row["year"], row["min_hourly"], row["max_hourly"]) for row in rows]


def upsert_reference_rates(conn: sqlite3.Connection, rates: Iterable[ReferenceRate]) -> int:
    count = 0
    for rate in rates:
        conn.execute(
            "INSERT INTO reference_rate (program_id, year, min_hourly, max_hourly) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(program_id, year) DO UPDATE SET min_hourly = excluded.min_hourly, "
            "max_hourly = excluded.max_hourly",
            (rate.program_id, rate.year, rate.min_hourly, rate.max_hourly),
        )
        count += 1
    return count


def list_providers(conn: sqlite3.Connection) -> list[Provider]:
    rows = conn.execute("SELECT rut, canonical_name, confirmed_at FROM provider ORDER BY canonical_name").fetchall()
    return [Provider(row["rut"], row["canonical_name"], datetime.fromisoformat(row["confirmed_at"])) for row in rows]


def get_provider(conn: sqlite3.Connection, rut: str) -> Provider | None:
    row = conn.execute("SELECT rut, canonical_name, confirmed_at FROM provider WHERE rut = ?", (rut,)).fetchone()
    if row is None:
        return None
    return Provider(row["rut"], row["canonical_name"], datetime.fromisoformat(row["confirmed_at"]))


def upsert_providers(conn: sqlite3.Connection, providers: Iterable[Provider]) -> int:
    count = 0
    for provider in providers:
        conn.execute(
            "INSERT INTO provider (rut, canonical_name, confirmed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(rut) DO UPDATE SET canonical_name = excluded.canonical_name, "
            "confirmed_at = excluded.confirmed_at",
            (provider.rut, provider.canonical_name, provider.confirmed_at.isoformat(timespec="seconds")),
        )
        count += 1
    return count
