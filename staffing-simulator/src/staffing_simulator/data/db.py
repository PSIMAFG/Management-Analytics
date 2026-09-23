"""Conexión a SQLite, creación del esquema y actualización de bases de versiones anteriores."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from staffing_simulator.errors import DataError

SCHEMA_VERSION = 3

# Cambios para llevar una base existente de una versión a la siguiente (versión de origen -> sentencias).
# La versión 3 rediseña la estructura financiera y no migra bases anteriores: use --reiniciar-datos.
MIGRATIONS: dict[int, tuple[str, ...]] = {}

log = logging.getLogger(__name__)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Abre la base con claves foráneas activas y filas accesibles por nombre."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_db(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Conexión que se cierra siempre al salir del bloque.

    Cada llamada de un servicio abre su propia conexión, lo que permite usar
    los servicios desde hilos de trabajo sin compartir conexiones.
    """
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def initialize(conn: sqlite3.Connection) -> bool:
    """Crea el esquema si la base está vacía o actualiza una base de una versión anterior.

    Devuelve True cuando la base se creó en esta llamada (y por lo tanto
    corresponde poblarla con los datos de ejemplo).
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return False
    if 0 < version < SCHEMA_VERSION and all(step in MIGRATIONS for step in range(version, SCHEMA_VERSION)):
        _migrate(conn, version)
        return False
    if version != 0:
        raise DataError(
            f"La base de datos tiene la versión de esquema {version} y la aplicación espera la {SCHEMA_VERSION}. "
            "Use la opción --reiniciar-datos para regenerarla."
        )
    schema = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    log.info("Esquema creado (versión %d)", SCHEMA_VERSION)
    return True


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """Aplica en una sola transacción los cambios desde `version` hasta la versión actual."""
    with transaction(conn):
        # BEGIN explícito: sqlite3 no abre una transacción antes de ALTER TABLE.
        conn.execute("BEGIN")
        for step in range(version, SCHEMA_VERSION):
            for statement in MIGRATIONS[step]:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    log.info("Esquema actualizado de la versión %d a la %d", version, SCHEMA_VERSION)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Confirma los cambios al salir sin errores y los revierte si hay una excepción."""
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
