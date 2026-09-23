"""Conexión a SQLite y creación del esquema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from receipt_reader.errors import DataError

SCHEMA_VERSION = 3
BUSY_TIMEOUT_SECONDS = 15

# Cambios para llevar una base existente de la versión indicada a la siguiente.
MIGRATIONS: dict[int, str] = {
    # 1 -> 2: se guardan los códigos aceptados al aprobar. Las boletas ya aprobadas
    # conservan como aceptadas las incidencias aceptables que tenían en ese momento.
    1: """
        CREATE TABLE receipt_acceptance (
            receipt_id INTEGER NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            PRIMARY KEY (receipt_id, code)
        );
        INSERT INTO receipt_acceptance (receipt_id, code)
            SELECT DISTINCT i.receipt_id, i.code FROM receipt_issue i JOIN receipt r ON r.id = i.receipt_id
            WHERE r.accepted_at IS NOT NULL AND i.severity = 'blocking' AND i.overridable = 1;
    """,
    # 2 -> 3: los programas se pueden desactivar en vez de borrarlos (las boletas ya
    # registradas conservan el programa que tenían aunque se desactive).
    2: """
        ALTER TABLE program ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1));
    """,
}

log = logging.getLogger(__name__)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Abre la base con claves foráneas activas y filas accesibles por nombre.

    Se usa el diario WAL para que la interfaz pueda leer mientras un proceso
    en segundo plano registra boletas. Con WAL, `synchronous = NORMAL` es la
    configuración recomendada: no arriesga la integridad de la base y evita
    una escritura forzada a disco en cada confirmación (se confirma una vez
    por archivo procesado).
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as error:
        raise DataError(f"No se pudo abrir la base de datos en {db_path}.") from error
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Conexión de corta duración que se cierra siempre al salir."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def initialize(conn: sqlite3.Connection) -> bool:
    """Crea el esquema si la base está vacía o lo actualiza si es de una versión anterior.

    Devuelve True cuando la base se creó en esta llamada (y por lo tanto
    corresponde poblarla con los datos de ejemplo).
    """
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        return False
    if version == 0:
        schema = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        log.info("Esquema creado (versión %d)", SCHEMA_VERSION)
        return True
    if version > SCHEMA_VERSION or any(step not in MIGRATIONS for step in range(version, SCHEMA_VERSION)):
        raise DataError(
            f"La base de datos tiene la versión de esquema {version} y la aplicación espera la {SCHEMA_VERSION}. "
            "Use la opción --reiniciar-datos para regenerarla."
        )
    for step in range(version, SCHEMA_VERSION):
        # Cada paso es atómico: si falla, la base queda en la versión anterior.
        try:
            conn.executescript(f"BEGIN; {MIGRATIONS[step]} PRAGMA user_version = {step + 1}; COMMIT;")
        except sqlite3.Error as error:
            conn.rollback()
            raise DataError(f"No se pudo actualizar la base de datos a la versión {step + 1}.") from error
        log.info("Esquema actualizado de la versión %d a la %d", step, step + 1)
    return False


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Confirma los cambios al salir sin errores y los revierte si hay una excepción."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()
