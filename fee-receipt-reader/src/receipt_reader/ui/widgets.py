"""Componentes reutilizables: tarjetas de totales, lienzo de gráficos y tablas."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from receipt_reader.ui.formatting import fold_search
from receipt_reader.ui.style import TONE_COLORS

ModelIndex = QModelIndex | QPersistentModelIndex
# Rol con una clave de orden primitiva (número o texto) para que el orden no dependa del texto formateado.
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
# Rol que entrega el registro completo de la fila (lo usan los delegados que dibujan varias líneas).
ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 2
EMPHASIS_BACKGROUND = "#EEF1F5"


class KpiCard(QFrame):
    """Tarjeta con un total: título, valor destacado y una línea de contexto."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        self._title = QLabel(title, objectName="kpiTitle")
        self._value = QLabel("-", objectName="kpiValue")
        self._caption = QLabel("", objectName="kpiCaption")
        for label in (self._title, self._value, self._caption):
            layout.addWidget(label)

    def set_value(self, value: str, caption: str = "", tone: str = "neutral", tooltip: str = "") -> None:
        self._value.setText(value)
        self._value.setStyleSheet(f"color: {TONE_COLORS.get(tone, TONE_COLORS['neutral'])};")
        self._caption.setText(caption)
        self.setToolTip(tooltip)

    def value_text(self) -> str:
        return self._value.text()


class KpiStrip(QWidget):
    """Fila de tarjetas de totales siempre visible sobre el contenido."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._cards: dict[str, KpiCard] = {}

    def add_card(self, key: str, title: str) -> KpiCard:
        card = KpiCard(title, self)
        self._cards[key] = card
        self._layout.addWidget(card)
        return card

    def set_value(self, key: str, value: str, caption: str = "", tone: str = "neutral", tooltip: str = "") -> None:
        self._cards[key].set_value(value, caption, tone, tooltip)

    def card(self, key: str) -> KpiCard:
        return self._cards[key]

    def card_keys(self) -> list[str]:
        return list(self._cards)


class ChartCanvas(QWidget):
    """Figura de matplotlib embebida que se redibuja a pedido."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.figure = Figure(layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumSize(200, 160)
        self.message: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.canvas)

    def clear(self) -> Figure:
        self.message = None
        self.figure.clear()
        self.figure.get_layout_engine().set(w_pad=0.08, h_pad=0.08)
        return self.figure

    def redraw(self) -> None:
        self.canvas.draw_idle()

    def show_message(self, message: str) -> None:
        """Muestra un aviso centrado cuando no hay datos que graficar."""
        figure = self.clear()
        self.message = message
        ax = figure.add_subplot()
        ax.axis("off")
        ax.text(0.5, 0.5, message, ha="center", va="center", color=TONE_COLORS["muted"], wrap=True)
        self.redraw()


@dataclass(frozen=True)
class Column:
    """Definición de una columna: clave del registro, encabezado y formato.

    `color` devuelve el color del texto de la celda (por ejemplo, el estado) y
    `tooltip` un texto de ayuda; ambos reciben el valor crudo.
    """

    key: str
    header: str
    formatter: Callable[[Any], str] = str
    numeric: bool = False
    color: Callable[[Any], str | None] | None = None
    tooltip: Callable[[Any], str] | None = None


def sort_key(value: Any, numeric: bool) -> Any:
    """Clave primitiva para ordenar: números como float y el resto como texto comparable."""
    if numeric:
        if value is None:
            return float("-inf")
        if isinstance(value, int | float | Decimal):
            return float(value)
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        label = getattr(value, "label", None)
        return str(label if label is not None else value.value).casefold()
    if hasattr(value, "iso"):
        return str(value.iso())
    if isinstance(value, int | float | Decimal):
        return float(value)
    return str(value).casefold()


class RecordTableModel(QAbstractTableModel):
    """Modelo de solo lectura para listas de registros (dict o dataclass)."""

    def __init__(
        self,
        columns: Sequence[Column],
        parent: QObject | None = None,
        *,
        emphasize: Callable[[Any], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[Any] = []
        # Filas destacadas (por ejemplo, la fila de totales): negrita y fondo gris.
        self._emphasize = emphasize
        self._bold = QFont()
        self._bold.setBold(True)

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, index: int) -> Any:
        return self._rows[index]

    def rows(self) -> list[Any]:
        return list(self._rows)

    def columns(self) -> list[Column]:
        return list(self._columns)

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns)

    @staticmethod
    def _raw(row: Any, key: str) -> Any:
        return row[key] if isinstance(row, dict) else getattr(row, key)

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        column = self._columns[index.column()]
        value = self._raw(self._rows[index.row()], column.key)
        if role == Qt.ItemDataRole.DisplayRole:
            return "-" if value is None else column.formatter(value)
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == SORT_ROLE:
            return sort_key(value, column.numeric)
        if role == ROW_ROLE:
            return self._rows[index.row()]
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role in (Qt.ItemDataRole.FontRole, Qt.ItemDataRole.BackgroundRole) and self._emphasize is not None:
            if not self._emphasize(self._rows[index.row()]):
                return None
            return self._bold if role == Qt.ItemDataRole.FontRole else QColor(EMPHASIS_BACKGROUND)
        if role == Qt.ItemDataRole.ForegroundRole and column.color is not None and value is not None:
            color = column.color(value)
            return QColor(color) if color else None
        if role == Qt.ItemDataRole.ToolTipRole:
            if column.tooltip is not None and value is not None:
                return column.tooltip(value) or None
            text = "" if value is None else column.formatter(value)
            return text if len(text) > 40 else None
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._columns[section].header
        if role == Qt.ItemDataRole.TextAlignmentRole and orientation == Qt.Orientation.Horizontal:
            column = self._columns[section]
            align = Qt.AlignmentFlag.AlignRight if column.numeric else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None


class SearchProxyModel(QSortFilterProxyModel):
    """Ordena por la clave primitiva y filtra por un texto libre sin distinguir tildes ni mayúsculas."""

    def __init__(self, search_text: Callable[[Any], str] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._search_text = search_text
        self._needle = ""
        self._cache: dict[int, str] = {}
        self.setSortRole(SORT_ROLE)

    def set_search(self, text: str) -> None:
        needle = fold_search(text.strip())
        if needle == self._needle:
            return
        if hasattr(self, "beginFilterChange"):
            # Qt 6.10 o posterior: invalidateFilter quedó obsoleto.
            self.beginFilterChange()
            self._needle = needle
            self.endFilterChange()
        else:
            self._needle = needle
            self.invalidateFilter()

    def clear_cache(self) -> None:
        """Se llama antes de que el modelo de origen cambie sus filas."""
        self._cache.clear()

    def filterAcceptsRow(self, source_row: int, _source_parent: ModelIndex) -> bool:
        if not self._needle or self._search_text is None:
            return True
        model = self.sourceModel()
        if not isinstance(model, RecordTableModel):
            return True
        haystack = self._cache.get(source_row)
        if haystack is None:
            haystack = fold_search(self._search_text(model.row(source_row)))
            self._cache[source_row] = haystack
        return all(part in haystack for part in self._needle.split())


def make_table_view(
    model: QAbstractTableModel,
    parent: QWidget | None = None,
    *,
    search_text: Callable[[Any], str] | None = None,
    fit_columns: bool = True,
) -> QTableView:
    """Tabla ordenable (por el valor crudo, no por el texto formateado) y opcionalmente filtrable."""
    proxy = SearchProxyModel(search_text, parent)
    proxy.setSourceModel(model)
    model.modelAboutToBeReset.connect(proxy.clear_cache)
    view = QTableView(parent)
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(26)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    if fit_columns:
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    else:
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    return view


def fit_columns_once(view: QTableView, max_width: int = 320) -> None:
    """Ajusta el ancho de las columnas al contenido una vez, sin fijarlo (el usuario puede cambiarlo)."""
    view.resizeColumnsToContents()
    header = view.horizontalHeader()
    for section in range(header.count() - 1):
        if header.sectionSize(section) > max_width:
            header.resizeSection(section, max_width)


def source_row_of(view: QTableView) -> int | None:
    """Fila del modelo de origen seleccionada en una vista con proxy (None si no hay selección)."""
    selection = view.selectionModel()
    if selection is None:
        return None
    indexes = selection.selectedRows()
    if not indexes:
        return None
    proxy = view.model()
    index = proxy.mapToSource(indexes[0]) if isinstance(proxy, QSortFilterProxyModel) else indexes[0]
    return index.row()


class ElidedLabel(QLabel):
    """Etiqueta de una línea que recorta el texto con puntos suspensivos y lo muestra completo al pasar el mouse."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, object_name: str = "") -> None:
        super().__init__(parent)
        if object_name:
            self.setObjectName(object_name)
        self._full = ""
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text if text else "")
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width() - 2, 20)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, width))


class StatusLabel(QLabel):
    """Mensaje de estado de hasta dos o tres líneas (ajusta el texto en vez de recortarlo)."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, object_name: str = "small") -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self.setText(text)
        self.setToolTip(text)

    def full_text(self) -> str:
        return self.text()


def section_title(text: str, parent: QWidget | None = None) -> QLabel:
    return QLabel(text, parent, objectName="sectionTitle")


def muted_label(text: str = "", parent: QWidget | None = None, *, small: bool = False) -> QLabel:
    label = QLabel(text, parent, objectName="small" if small else "muted")
    label.setWordWrap(True)
    return label


def chip(text: str, color: str, parent: QWidget | None = None) -> QLabel:
    """Etiqueta breve con fondo de color (por ejemplo, la severidad de una incidencia)."""
    label = QLabel(text, parent, objectName="chip")
    label.setStyleSheet(f"background: {color};")
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return label


def separator(parent: QWidget | None = None) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("separator")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


def panel(parent: QWidget | None = None) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("panel")
    return frame
