"""Ejecución de tareas largas fuera del hilo de la interfaz."""

from __future__ import annotations

import gc
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from staffing_simulator.errors import AppError, OperationCancelledError

log = logging.getLogger(__name__)

UNEXPECTED_ERROR = "Ocurrió un error inesperado. El detalle quedó registrado en el log de la aplicación."


class MainThreadGarbageCollector(QObject):
    """Ejecuta la recolección de ciclos de Python solo en el hilo de la interfaz.

    Los Worker no forman ciclos de referencias (ver `Worker`), pero otros
    objetos de Qt sí pueden quedar en ciclos, por ejemplo un diálogo que guarda
    una función que lo referencia. El recolector automático de Python puede
    activarse dentro de un hilo de trabajo y destruir allí esos objetos, que
    deben destruirse en el hilo que los creó. Por eso la aplicación desactiva la
    recolección automática y la hace desde un temporizador del hilo principal,
    con los mismos umbrales por generación. Se instala de forma explícita al
    arrancar (`install_main_thread_gc` en app.py), nunca como efecto lateral.
    """

    INTERVAL_MS = 400

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._thresholds = gc.get_threshold()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.collect_if_needed)
        self._timer.start(self.INTERVAL_MS)

    def collect_if_needed(self) -> None:
        counts = gc.get_count()
        if counts[0] <= self._thresholds[0]:
            return
        generation = 0
        if counts[1] > self._thresholds[1]:
            generation = 2 if counts[2] > self._thresholds[2] else 1
        gc.collect(generation)


_collector: MainThreadGarbageCollector | None = None


def install_main_thread_gc(app: QCoreApplication) -> None:
    """Instala una sola vez el recolector en el hilo principal de la aplicación."""
    global _collector
    if _collector is not None and shiboken6.isValid(_collector):
        return
    _collector = MainThreadGarbageCollector(app)


class WorkerSignals(QObject):
    """Señales de un Worker; todas llevan el identificador de la tarea."""

    finished = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int, str)
    progress = Signal(int, int, str)


class Worker(QRunnable):
    """Ejecuta una función en el pool de hilos y comunica el resultado por señales.

    Si la función acepta los argumentos `progress` y `cancel`, recibe una
    función para informar avance (porcentaje, mensaje) y un evento para
    detectar la cancelación pedida por el usuario.

    El pool no destruye el Worker al terminar (`autoDelete` desactivado): lo
    conserva el TaskRunner hasta que `done` indica que `run` ya retornó, para
    que Python y Qt nunca liberen dos veces el mismo objeto.

    El Worker no se referencia a sí mismo: la función de avance solo captura
    las señales y el identificador, y al terminar se sueltan la función y sus
    argumentos. Así se libera por conteo de referencias en el hilo principal,
    sin depender del recolector de ciclos.
    """

    _ids = itertools.count(1)

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.task_id = next(Worker._ids)
        self.done = threading.Event()
        self.fn: Callable[..., Any] | None = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.cancel_event = threading.Event()
        if with_progress:
            self.kwargs["progress"] = _progress_emitter(self.signals, self.task_id)
            self.kwargs["cancel"] = self.cancel_event

    def cancel(self) -> None:
        """Pide detener la tarea; la función lo detecta en su próximo punto de control."""
        self.cancel_event.set()

    def run(self) -> None:
        fn, args, kwargs = self.fn, self.args, self.kwargs
        try:
            if fn is None:
                raise RuntimeError("El Worker ya se ejecutó")
            result = fn(*args, **kwargs)
        except OperationCancelledError as error:
            log.info("Tarea cancelada por el usuario")
            self.signals.cancelled.emit(self.task_id, error.user_message)
        except AppError as error:
            log.warning("Tarea interrumpida: %s", error.user_message)
            self.signals.failed.emit(self.task_id, error.user_message)
        except Exception:
            log.exception("Error inesperado en una tarea en segundo plano")
            self.signals.failed.emit(self.task_id, UNEXPECTED_ERROR)
        else:
            self.signals.finished.emit(self.task_id, result)
        finally:
            # Soltar la función y sus argumentos antes de avisar que terminó.
            self.fn, self.args, self.kwargs = None, (), {}
            self.done.set()


def _progress_emitter(signals: WorkerSignals, task_id: int) -> Callable[[int, str], None]:
    """Función de avance que no referencia al Worker (evita un ciclo Worker -> kwargs -> Worker)."""

    def report(percent: int, message: str) -> None:
        signals.progress.emit(task_id, percent, message)

    return report


@dataclass(frozen=True)
class _Callbacks:
    on_finished: Callable[[Any], None]
    on_failed: Callable[[str], None]
    on_progress: Callable[[int, str], None] | None
    on_cancelled: Callable[[str], None] | None


class TaskRunner(QObject):
    """Lanza tareas en segundo plano y entrega sus resultados en el hilo de la interfaz.

    Las señales de cada Worker llegan a las ranuras de este objeto, que vive en
    el hilo principal; desde ahí se llaman las funciones indicadas al iniciar.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._tasks: dict[int, tuple[Worker, _Callbacks]] = {}
        self._finished: list[Worker] = []

    def start(
        self,
        worker: Worker,
        on_finished: Callable[[Any], None],
        on_failed: Callable[[str], None],
        on_progress: Callable[[int, str], None] | None = None,
        on_cancelled: Callable[[str], None] | None = None,
    ) -> Worker:
        self._purge()
        self._tasks[worker.task_id] = (worker, _Callbacks(on_finished, on_failed, on_progress, on_cancelled))
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        worker.signals.progress.connect(self._on_progress)
        self._pool.start(worker)
        return worker

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    def cancel_all(self) -> None:
        for worker, _callbacks in list(self._tasks.values()):
            worker.cancel()

    def wait_all(self, timeout_ms: int = 60_000) -> bool:
        finished = self._pool.waitForDone(timeout_ms)
        if finished:
            self._purge()
        return finished

    def _take(self, task_id: int) -> _Callbacks | None:
        entry = self._tasks.pop(task_id, None)
        if entry is None:
            return None
        worker, callbacks = entry
        self._finished.append(worker)
        return callbacks

    def _purge(self) -> None:
        """Libera los Worker cuyo hilo ya terminó de ejecutar `run`."""
        self._finished = [worker for worker in self._finished if not worker.done.is_set()]

    @Slot(int, object)
    def _on_finished(self, task_id: int, result: object) -> None:
        callbacks = self._take(task_id)
        if callbacks is not None:
            callbacks.on_finished(result)

    @Slot(int, str)
    def _on_failed(self, task_id: int, message: str) -> None:
        callbacks = self._take(task_id)
        if callbacks is not None:
            callbacks.on_failed(message)

    @Slot(int, str)
    def _on_cancelled(self, task_id: int, message: str) -> None:
        callbacks = self._take(task_id)
        if callbacks is not None:
            (callbacks.on_cancelled or callbacks.on_failed)(message)

    @Slot(int, int, str)
    def _on_progress(self, task_id: int, percent: int, message: str) -> None:
        entry = self._tasks.get(task_id)
        if entry is not None and entry[1].on_progress is not None:
            entry[1].on_progress(percent, message)
