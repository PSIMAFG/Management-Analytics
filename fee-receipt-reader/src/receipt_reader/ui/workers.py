"""Ejecución de tareas largas fuera del hilo de la interfaz."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from receipt_reader.errors import UNEXPECTED_ERROR, AppError

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)


class Worker(QRunnable):
    """Ejecuta una función en el pool de hilos y comunica el resultado por señales.

    Si la función acepta los argumentos `progress` y `cancel`, recibe una
    función para informar avance (porcentaje, mensaje) y un evento para
    detectar la cancelación pedida por el usuario.
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.cancel_event = threading.Event()
        if with_progress:
            self.kwargs["progress"] = self.signals.progress.emit
            self.kwargs["cancel"] = self.cancel_event

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except AppError as error:
            log.warning("Tarea interrumpida: %s", error.user_message)
            self.signals.failed.emit(error.user_message)
        except Exception:
            log.exception("Error inesperado en una tarea en segundo plano")
            self.signals.failed.emit(UNEXPECTED_ERROR)
        else:
            self.signals.finished.emit(result)


class TaskRunner(QObject):
    """Mantiene vivas las tareas en curso hasta que terminan.

    Con `max_threads` usa un pool propio: con 1 hilo las tareas se ejecutan de a
    una y en orden (sirve para bibliotecas que no admiten uso concurrente).
    """

    def __init__(self, parent: QObject | None = None, *, max_threads: int | None = None) -> None:
        super().__init__(parent)
        if max_threads is None:
            self._pool = QThreadPool.globalInstance()
        else:
            self._pool = QThreadPool(self)
            self._pool.setMaxThreadCount(max_threads)
        self._active: set[Worker] = set()

    def is_busy(self) -> bool:
        return bool(self._active)

    def start(
        self,
        worker: Worker,
        on_finished: Callable[[Any], None],
        on_failed: Callable[[str], None],
        on_progress: Callable[[int, str], None] | None = None,
    ) -> Worker:
        self._active.add(worker)
        worker.signals.finished.connect(on_finished)
        worker.signals.failed.connect(on_failed)
        if on_progress is not None:
            worker.signals.progress.connect(on_progress)
        # Se conectan al final: la tarea sigue viva (y cuenta como activa) mientras se entrega su resultado.
        worker.signals.finished.connect(lambda _result: self._active.discard(worker))
        worker.signals.failed.connect(lambda _message: self._active.discard(worker))
        self._pool.start(worker)
        return worker

    def wait_all(self, timeout_ms: int = 60_000) -> bool:
        return self._pool.waitForDone(timeout_ms)
