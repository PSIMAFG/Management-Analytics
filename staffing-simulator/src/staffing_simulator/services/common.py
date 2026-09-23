"""Infraestructura compartida por los servicios: conexión, reloj, avance y cancelación."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from staffing_simulator.data import db
from staffing_simulator.errors import DataError, OperationCancelledError

ProgressCallback = Callable[[int, str], None]
Clock = Callable[[], datetime]

log = logging.getLogger(__name__)


def _system_clock() -> datetime:
    return datetime.now().replace(microsecond=0)


class ServiceBase:
    """Cada llamada abre su propia conexión, por lo que es seguro usarla desde hilos de trabajo."""

    def __init__(self, db_path: Path, clock: Clock | None = None) -> None:
        self.db_path = Path(db_path)
        self._clock = clock or _system_clock

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Conexión propia de la llamada; los errores de SQLite se informan con un mensaje claro."""
        try:
            with db.open_db(self.db_path) as conn:
                yield conn
        except sqlite3.DatabaseError as error:
            log.exception("Error de base de datos")
            raise DataError(
                "No se pudo acceder a la base de datos. El detalle quedó registrado en el log de la aplicación."
            ) from error

    def _now(self) -> datetime:
        return self._clock().replace(microsecond=0)


class Progress:
    """Informa avance y detiene la tarea si el usuario pidió cancelar."""

    def __init__(self, callback: ProgressCallback | None = None, cancel: threading.Event | None = None) -> None:
        self._callback = callback
        self._cancel = cancel

    def check(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise OperationCancelledError

    def report(self, percent: int, message: str) -> None:
        """Revisa la cancelación e informa el avance; usar solo antes del punto de no retorno."""
        self.check()
        self._notify(percent, message)

    def finish(self, message: str) -> None:
        """Informa el 100 % sin revisar la cancelación.

        Se usa después del punto de no retorno (una transacción confirmada o un
        archivo ya escrito): cancelar en ese momento no puede deshacer nada y
        solo haría informar al usuario algo que no ocurrió.
        """
        self._notify(100, message)

    def _notify(self, percent: int, message: str) -> None:
        if self._callback is not None:
            self._callback(max(0, min(100, percent)), message)
