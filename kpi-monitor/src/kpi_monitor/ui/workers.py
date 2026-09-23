"""Ejecución de tareas largas fuera del hilo de la interfaz.

Las funciones se ejecutan en el pool de hilos de Qt y comunican el resultado
por señales, que Qt entrega en el hilo de la interfaz: los widgets solo se
tocan desde las funciones conectadas a esas señales, nunca desde la tarea.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QObject, QRunnable, QThreadPool, Signal

from kpi_monitor.errors import AppError, OperationCancelledError

log = logging.getLogger(__name__)

UNEXPECTED_ERROR = "Ocurrió un error inesperado. El detalle quedó registrado en el log de la aplicación."


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    progress = Signal(int, str)


class Worker(QRunnable):
    """Ejecuta una función en el pool de hilos y comunica el resultado por señales.

    Si la función acepta los argumentos `progress` y `cancel`, recibe una
    función para informar avance (porcentaje, mensaje) y un evento para
    detectar la cancelación pedida por el usuario.
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.cancel_event = threading.Event()
        if with_progress:
            self.kwargs["progress"] = self.signals.progress.emit
            self.kwargs["cancel"] = self.cancel_event

    def cancel(self) -> None:
        """Pide a la tarea que se detenga en el próximo punto de control."""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except OperationCancelledError as error:
            log.info("Tarea cancelada: %s", error.user_message)
            self.signals.cancelled.emit(error.user_message)
        except AppError as error:
            log.warning("Tarea interrumpida: %s", error.user_message)
            self.signals.failed.emit(error.user_message)
        except Exception:
            log.exception("Error inesperado en una tarea en segundo plano")
            self.signals.failed.emit(UNEXPECTED_ERROR)
        else:
            self.signals.finished.emit(result)


class TaskRunner(QObject):
    """Mantiene vivas las tareas en curso hasta que terminan."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._active: set[Worker] = set()

    @property
    def busy(self) -> bool:
        return bool(self._active)

    def start(
        self,
        worker: Worker,
        on_finished: Callable[[Any], None],
        on_failed: Callable[[str], None],
        on_progress: Callable[[int, str], None] | None = None,
        on_cancelled: Callable[[str], None] | None = None,
    ) -> Worker:
        """Lanza la tarea. Sin `on_cancelled`, una cancelación se informa como falla."""
        self._active.add(worker)
        worker.signals.finished.connect(lambda _result: self._active.discard(worker))
        worker.signals.failed.connect(lambda _message: self._active.discard(worker))
        worker.signals.cancelled.connect(lambda _message: self._active.discard(worker))
        worker.signals.finished.connect(on_finished)
        worker.signals.failed.connect(on_failed)
        worker.signals.cancelled.connect(on_cancelled if on_cancelled is not None else on_failed)
        if on_progress is not None:
            worker.signals.progress.connect(on_progress)
        self._pool.start(worker)
        return worker

    def wait_all(self, timeout_ms: int = 60_000) -> bool:
        return self._pool.waitForDone(timeout_ms)

    def wait_idle(self, timeout_ms: int = 60_000) -> bool:
        """Espera a que terminen las tareas y se entreguen sus señales (autoprueba y tests)."""
        timer = QElapsedTimer()
        timer.start()
        while self._active:
            if timer.elapsed() > timeout_ms:
                return False
            self._pool.waitForDone(20)
            QCoreApplication.processEvents()
        QCoreApplication.processEvents()
        return True
