"""Vista previa de la página exacta de una boleta, con zoom.

La página se rasteriza una vez por boleta en segundo plano (a RENDER_DPI) y
el zoom solo escala esa imagen. Las lecturas de PDF se hacen en un pool de un
solo hilo porque la biblioteca de PDF no admite uso concurrente.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget

from receipt_reader.ui.dialogs import open_path
from receipt_reader.ui.widgets import ElidedLabel
from receipt_reader.ui.workers import TaskRunner, Worker

log = logging.getLogger(__name__)

RENDER_DPI = 200
SCREEN_DPI = 96
ZOOM_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
FIT_MARGIN = 18
UNREADABLE_FILE = "El archivo no se pudo abrir, así que no hay página que mostrar. Use Abrir original para revisarlo."


def next_zoom(current: float, direction: int) -> float:
    """Siguiente nivel de zoom hacia arriba (1) o hacia abajo (-1) desde un valor cualquiera."""
    if direction > 0:
        return next((step for step in ZOOM_STEPS if step > current + 1e-6), ZOOM_STEPS[-1])
    return next((step for step in reversed(ZOOM_STEPS) if step < current - 1e-6), ZOOM_STEPS[0])


def zoom_label(zoom: float | None) -> str:
    return "Ajustado" if zoom is None else f"{round(zoom * 100)} %"


@dataclass(frozen=True)
class PreviewTarget:
    """Qué página mostrar y cómo describirla."""

    receipt_id: int
    path: Path
    page_index: int
    page_count: int
    caption: str


class PagePreview(QWidget):
    """Imagen de la página con controles de zoom y un botón para abrir el archivo original."""

    def __init__(
        self,
        render: Callable[[int, int], bytes],
        tasks: TaskRunner,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._render = render
        self._tasks = tasks
        self._target: PreviewTarget | None = None
        self._image: QImage | None = None
        self._zoom: float | None = None
        self._request = 0
        self._blocked_message: str | None = None
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(60)
        self._fit_timer.timeout.connect(self._apply_zoom)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self.zoom_out_button = self._tool("-", "Alejar (Ctrl + rueda del mouse)", lambda: self.step_zoom(-1))
        self.zoom_label = QLabel(zoom_label(None), objectName="muted")
        self.zoom_label.setMinimumWidth(62)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in_button = self._tool("+", "Acercar (Ctrl + rueda del mouse)", lambda: self.step_zoom(1))
        self.fit_button = self._tool("Ajustar", "Ajustar la página al ancho disponible", self.fit_width)
        self.open_button = self._tool("Abrir original", "Abrir el archivo con la aplicación del sistema", self.open)
        for widget in (self.zoom_out_button, self.zoom_label, self.zoom_in_button, self.fit_button):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        toolbar.addWidget(self.open_button)
        layout.addLayout(toolbar)
        self.caption_label = ElidedLabel("", object_name="small")
        layout.addWidget(self.caption_label)

        self.scroll = QScrollArea(objectName="preview")
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scroll.viewport().setObjectName("previewViewport")
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.image_label.setWordWrap(True)
        self.image_label.setObjectName("muted")
        self.image_label.setContentsMargins(8, 8, 8, 8)
        self.scroll.setWidget(self.image_label)
        self.scroll.viewport().installEventFilter(self)
        layout.addWidget(self.scroll, 1)
        self.show_placeholder("Seleccione una boleta de la cola para ver su página.")

    def _tool(self, text: str, tip: str, slot: Callable[[], None]) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    # Estado

    @property
    def target(self) -> PreviewTarget | None:
        return self._target

    def has_image(self) -> bool:
        pixmap = self.image_label.pixmap()
        return self._image is not None and pixmap is not None and not pixmap.isNull()

    def zoom(self) -> float | None:
        return self._zoom

    def _update_controls(self) -> None:
        has_image = self._image is not None
        self.zoom_out_button.setEnabled(has_image and (self._zoom is None or self._zoom > ZOOM_STEPS[0]))
        self.zoom_in_button.setEnabled(has_image and (self._zoom is None or self._zoom < ZOOM_STEPS[-1]))
        self.fit_button.setEnabled(has_image and self._zoom is not None)
        self.open_button.setEnabled(self._target is not None)
        self.zoom_label.setText(zoom_label(self._zoom) if has_image else "")

    def show_placeholder(self, message: str) -> None:
        self._image = None
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(message)
        self._update_controls()

    # Carga

    def show_target(self, target: PreviewTarget | None) -> None:
        """Muestra la página indicada (o limpia la vista si es None)."""
        self._request += 1
        self._target = target
        if target is None:
            self.caption_label.set_full_text("")
            self.show_placeholder("Seleccione una boleta de la cola para ver su página.")
            return
        self.caption_label.set_full_text(target.caption)
        if not target.page_count:
            # Archivo dañado o sin páginas: no hay nada que dibujar, pero se puede abrir el original.
            self.show_placeholder(UNREADABLE_FILE)
            return
        if self._blocked_message is not None:
            self.show_placeholder(self._blocked_message)
            return
        self.show_placeholder("Cargando la vista previa...")
        request = self._request
        worker = Worker(self._render, target.receipt_id, RENDER_DPI)
        self._tasks.start(
            worker,
            lambda png, request=request: self._on_rendered(request, png),
            lambda message, request=request: self._on_failed(request, message),
        )

    def set_blocked(self, message: str | None) -> None:
        """Suspende las vistas previas (por ejemplo, mientras se procesa una carpeta) y las retoma después."""
        self._blocked_message = message
        if self._target is None:
            return
        if message is not None:
            self.show_placeholder(message)
        else:
            self.show_target(self._target)

    def _on_rendered(self, request: int, png: bytes) -> None:
        if request != self._request:
            return
        image = QImage.fromData(png, "PNG")
        if image.isNull():
            self.show_placeholder("No se pudo mostrar la página.")
            return
        self._image = image
        self.image_label.setText("")
        self._apply_zoom()

    def _on_failed(self, request: int, message: str) -> None:
        if request != self._request:
            return
        log.warning("Vista previa no disponible: %s", message)
        self.show_placeholder(message)

    # Zoom

    def fit_width(self) -> None:
        self._zoom = None
        self._apply_zoom()

    def step_zoom(self, direction: int) -> None:
        if self._image is None:
            return
        self._zoom = next_zoom(self._effective_zoom(), direction)
        self._apply_zoom()

    def _effective_zoom(self) -> float:
        if self._zoom is not None:
            return self._zoom
        if self._image is None or self._image.width() == 0:
            return 1.0
        return self._fit_pixels() / (self._image.width() * SCREEN_DPI / RENDER_DPI)

    def _fit_pixels(self) -> int:
        return max(self.scroll.viewport().width() - FIT_MARGIN, 80)

    def _apply_zoom(self) -> None:
        if self._image is None:
            self._update_controls()
            return
        if self._zoom is None:
            width = self._fit_pixels()
        else:
            width = round(self._image.width() * SCREEN_DPI / RENDER_DPI * self._zoom)
        scaled = self._image.scaledToWidth(max(width, 40), Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(QPixmap.fromImage(scaled))
        self._update_controls()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._zoom is None and self._image is not None:
            self._fit_timer.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.scroll.viewport()
            and event.type() == QEvent.Type.Wheel
            and isinstance(event, QWheelEvent)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.step_zoom(1 if event.angleDelta().y() > 0 else -1)
            return True
        return super().eventFilter(watched, event)

    def open(self) -> None:
        if self._target is not None:
            open_path(self._target.path)
