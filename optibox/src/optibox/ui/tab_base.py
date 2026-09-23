"""Base común de las pestañas de resultados."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from optibox.domain.run import RunDetail
from optibox.ui.charts import chart_has_data
from optibox.ui.widgets import ChartCanvas


class TabPage(QWidget):
    """Pestaña que muestra una parte de la corrida seleccionada.

    Cada pestaña sabe redibujarse con una corrida nueva (`set_detail`),
    entrega sus gráficos para que la autoprueba los dibuje y revisa por sí
    misma que sus vistas principales tengan datos (`self_check`).
    """

    title = ""

    def set_detail(self, detail: RunDetail | None) -> None:
        raise NotImplementedError

    def charts(self) -> tuple[ChartCanvas, ...]:
        return ()

    def self_check(self) -> list[str]:
        return []


def check_charts(charts: Sequence[tuple[str, ChartCanvas]]) -> list[str]:
    """Problemas de gráficos que quedaron vacíos o con un aviso en vez de datos."""
    problems: list[str] = []
    for name, chart in charts:
        if chart.has_message or not chart_has_data(chart.figure):
            problems.append(f"el gráfico '{name}' no tiene datos")
    return problems


def splitter(orientation: Qt.Orientation, widgets: Sequence[QWidget], stretches: Sequence[int]) -> QSplitter:
    """Divisor redimensionable que no permite colapsar sus partes.

    Las partes parten con tamaños proporcionales a `stretches` y conservan esa
    proporción cuando cambia el tamaño de la ventana.
    """
    split = QSplitter(orientation)
    split.setChildrenCollapsible(False)
    split.setHandleWidth(8)
    for widget, stretch in zip(widgets, stretches, strict=True):
        split.addWidget(widget)
        split.setStretchFactor(split.count() - 1, stretch)
    split.setSizes([stretch * 1000 for stretch in stretches])
    return split


def vbox(parent: QWidget | None = None, margins: int = 0, spacing: int = 6) -> tuple[QWidget, QVBoxLayout]:
    """Contenedor vertical sin márgenes."""
    container = QWidget(parent)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return container, layout
