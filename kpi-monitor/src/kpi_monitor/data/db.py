"""Conexión a SQLite y creación del esquema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from kpi_monitor.errors import DataError

SCHEMA_VERSION = 1
READ_ERROR = (
    "No se pudo leer la base de datos. Cierre otras ventanas de la aplicación y vuelva a intentarlo; si el problema "
    "sigue, la base puede estar dañada (use la opción --reiniciar-datos para regenerarla)."
)

log = logging.getLogger(__name__)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Abre la base con claves foráneas activas y filas accesibles por nombre."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_db(db_path: Path | str, error_message: str = READ_ERROR) -> Iterator[sqlite3.Connection]:
    """Abre una conexión y la cierra siempre al salir del bloque.

    Un error de SQLite dentro del bloque (base bloqueada por otra ventana, archivo
    dañado) se convierte en DataError con `error_message`, un texto en español apto
    para mostrarse; el detalle técnico queda encadenado para el log. Los errores
    propios de la aplicación pasan sin cambios.
    """
    try:
        conn = connect(db_path)
    except sqlite3.Error as error:
        raise DataError(f"No se pudo abrir la base de datos ({db_path}).") from error
    try:
        yield conn
    except sqlite3.Error as error:
        raise DataError(error_message) from error
    finally:
        conn.close()


def initialize(conn: sqlite3.Connection) -> bool:
    """Crea el esquema si la base está vacía.

    Devuelve True cuando la base se creó en esta llamada (y por lo tanto
    corresponde poblarla con los datos de ejemplo).
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
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


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Confirma los cambios al salir sin errores y los revierte si hay una excepción."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()
