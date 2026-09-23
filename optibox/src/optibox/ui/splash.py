"""Aviso de inicio mientras se preparan los datos de ejemplo."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

from optibox import APP_TITLE


class StartupNotice(QFrame):
    """Ventana pequeña y sin bordes que informa que la primera carga toma unos segundos."""

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.SplashScreen | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("panel")
        self.setFixedSize(460, 120)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel(APP_TITLE, objectName="sectionTitle")
        text = QLabel(message)
        text.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(text, 1)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
        self.show()
        for _ in range(3):
            QApplication.processEvents()
