"""Conexión a SQLite y creación del esquema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from optibox.errors import DataError

SCHEMA_VERSION = 1

log = logging.getLogger(__name__)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Abre la base con claves foráneas activas y filas accesibles por nombre."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def as_data_error(error: sqlite3.Error, db_path: Path | str) -> DataError:
    """Traduce un error de SQLite a un mensaje en español que indica el archivo afectado."""
    path = Path(db_path)
    log.warning("Error de SQLite en %s: %s", path, error)
    if isinstance(error, sqlite3.IntegrityError):
        return DataError(
            "Los datos no se guardaron porque contradicen otros registros de la base (por ejemplo, un código "
            "repetido o una referencia a un registro que no existe)."
        )
    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower():
        return DataError(f"La base de datos {path} está en uso por otro programa. Ciérrelo e intente de nuevo.")
    return DataError(
        f"No se pudo abrir la base de datos {path}: está dañada o en uso. Ciérrela si está abierta en otro "
        "programa, o elimínela (opción --reiniciar-datos) para que la aplicación la regenere con los datos de ejemplo."
    )


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Abre una conexión y la cierra al salir (con o sin error).

    Los errores de SQLite se relanzan como DataError con un mensaje para el
    usuario; los errores de programación (consultas mal escritas) no se
    ocultan.
    """
    try:
        conn = connect(db_path)
    except sqlite3.Error as error:
        raise as_data_error(error, db_path) from error
    try:
        yield conn
    except sqlite3.ProgrammingError:
        raise
    except sqlite3.Error as error:
        raise as_data_error(error, db_path) from error
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
