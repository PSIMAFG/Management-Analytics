"""Componentes reutilizables: tarjetas de totales, lienzo de gráficos y tablas."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from matplotlib.backend_bases import Event, MouseEvent
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QCursor, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from optibox.ui.charts import Region
from optibox.ui.style import TONE_COLORS

ModelIndex = QModelIndex | QPersistentModelIndex


class KpiCard(QFrame):
    """Tarjeta con un total: título, valor destacado y una línea de contexto."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(120)
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

    def caption_text(self) -> str:
        return self._caption.text()


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

    def card_keys(self) -> tuple[str, ...]:
        return tuple(self._cards)


Painter = Callable[[Figure], Sequence[Region]]


class ChartCanvas(QWidget):
    """Figura de matplotlib embebida que se redibuja a pedido.

    `render` recibe una función que dibuja sobre la figura vacía y devuelve
    sus regiones sensibles. La función se vuelve a ejecutar cuando cambia el
    tamaño del lienzo, para que los rótulos que dependen del espacio en
    pantalla (textos dentro de la agenda, por ejemplo) se ajusten.

    Al pasar el mouse sobre una región se muestra su texto de ayuda (el valor
    exacto, además de lo que el gráfico ya rotula) y al hacer clic se avisa a
    `on_click` con la región elegida.
    """

    def __init__(self, parent: QWidget | None = None, min_height: int = 220) -> None:
        super().__init__(parent)
        self.figure = Figure(layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(min_height)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.canvas)
        self._regions: list[Region] = []
        self._hovered: Region | None = None
        self._painter: Painter | None = None
        self._painted_size: tuple[int, int] | None = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self.ensure_current)
        self.on_click: Callable[[Region], None] | None = None
        self.has_message = False
        self._stale = True
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("figure_leave_event", self._on_leave)

    def clear(self) -> Figure:
        self.figure.clear()
        self._regions = []
        self._hovered = None
        self.has_message = False
        return self.figure

    def redraw(self) -> None:
        """Pide un redibujo en la próxima vuelta del ciclo de eventos."""
        self._stale = True
        self.canvas.draw_idle()

    def _draw_now(self) -> None:
        """Renderiza de inmediato; si el lienzo aún no tiene tamaño, lo deja para cuando se muestre."""
        if self.canvas.width() <= 0 or self.canvas.height() <= 0:
            self.redraw()
            return
        self.canvas.draw()
        self._stale = False

    def force_draw(self) -> None:
        """Deja el gráfico dibujado al tamaño actual (autoprueba y capturas), sin renderizar dos veces."""
        self.ensure_current()
        if self._stale:
            self._draw_now()

    def render(self, painter: Painter) -> None:
        """Dibuja con `painter` y lo recuerda para redibujar si cambia el tamaño.

        Si el gráfico no está a la vista (otra pestaña), el dibujo se posterga
        hasta que se muestre: así cambiar de corrida no dibuja lo que no se ve.
        """
        self._painter = painter
        self.has_message = False
        self._painted_size = None
        if self.isVisible():
            self._paint()

    def _paint(self) -> None:
        if self._painter is None:
            return
        figure = self.clear()
        self._regions = list(self._painter(figure))
        self._painted_size = (self.canvas.width(), self.canvas.height())
        self._draw_now()

    def ensure_current(self) -> None:
        """Dibuja de inmediato si hay un dibujo pendiente o si el tamaño cambió desde el último."""
        self._resize_timer.stop()
        if self._painter is not None and self._painted_size != (self.canvas.width(), self.canvas.height()):
            self._paint()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.ensure_current()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._painter is not None and self.isVisible():
            self._resize_timer.start()

    def set_regions(self, regions: Sequence[Region]) -> None:
        self._regions = list(regions)

    @property
    def regions(self) -> tuple[Region, ...]:
        return tuple(self._regions)

    def show_message(self, message: str) -> None:
        """Muestra un aviso centrado cuando no hay datos que graficar."""
        self._painter = None
        figure = self.clear()
        ax = figure.add_subplot()
        ax.axis("off")
        ax.text(0.5, 0.5, message, ha="center", va="center", color=TONE_COLORS["muted"], wrap=True)
        self.has_message = True
        self.redraw()

    def region_at(self, event: MouseEvent) -> Region | None:
        for region in self._regions:
            if region.contains(event.inaxes, event.xdata, event.ydata):
                return region
        return None

    def _on_motion(self, event: Event) -> None:
        if not isinstance(event, MouseEvent):
            return
        region = self.region_at(event)
        if region is self._hovered:
            return
        self._hovered = region
        if region is None:
            QToolTip.hideText()
            self.canvas.unsetCursor()
            return
        QToolTip.showText(QCursor.pos(), region.text, self.canvas)
        if self.on_click is not None:
            self.canvas.setCursor(Qt.CursorShape.PointingHandCursor)

    def _on_press(self, event: Event) -> None:
        if not isinstance(event, MouseEvent) or self.on_click is None:
            return
        region = self.region_at(event)
        if region is not None:
            self.on_click(region)

    def _on_leave(self, _event: Event) -> None:
        self._hovered = None
        QToolTip.hideText()


@dataclass(frozen=True)
class Column:
    """Definición de una columna: clave del registro, encabezado y formato."""

    key: str
    header: str
    formatter: Callable[[Any], str] = str
    numeric: bool = False
    tooltip: str = ""


class RecordTableModel(QAbstractTableModel):
    """Modelo de solo lectura para listas de registros (dict o dataclass)."""

    def __init__(self, columns: Sequence[Column], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[Any] = []

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, index: int) -> Any:
        return self._rows[index]

    def rows(self) -> tuple[Any, ...]:
        return tuple(self._rows)

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns)

    def _raw(self, row: Any, key: str) -> Any:
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
        if role == Qt.ItemDataRole.ToolTipRole and isinstance(value, str) and len(value) > 40:
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        column = self._columns[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.header
        if role == Qt.ItemDataRole.ToolTipRole and column.tooltip:
            return column.tooltip
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


def make_table_view(
    model: QAbstractTableModel,
    parent: QWidget | None = None,
    proxy: QSortFilterProxyModel | None = None,
    *,
    sortable: bool = True,
    stretch_column: int | None = None,
) -> QTableView:
    """Tabla ordenable (por el valor crudo, no por el texto formateado), en el orden original al abrir.

    `stretch_column` indica la columna de texto que absorbe el ancho sobrante;
    si no se indica, lo absorbe la última columna.
    """
    proxy = proxy or QSortFilterProxyModel(parent)
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.ItemDataRole.UserRole)
    view = QTableView(parent)
    view.setModel(proxy)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(26)
    header = view.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch_column is None:
        header.setStretchLastSection(True)
    else:
        header.setStretchLastSection(False)
        header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    if sortable:
        # Sin orden al abrir: se respeta el orden de los registros hasta que el usuario elija una columna.
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        view.setSortingEnabled(True)
    return view


def wrap_rows(view: QTableView) -> None:
    """Parte en varias líneas los textos largos y ajusta el alto de cada fila, en vez de recortarlos."""
    view.setWordWrap(True)
    view.setTextElideMode(Qt.TextElideMode.ElideNone)
    view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.horizontalHeader().sectionResized.connect(lambda *_args: view.resizeRowsToContents())


def source_row(view: QTableView, proxy_row: int) -> int:
    """Fila del modelo de origen que corresponde a una fila visible (con orden o filtro aplicados)."""
    model = view.model()
    if isinstance(model, QSortFilterProxyModel):
        return model.mapToSource(model.index(proxy_row, 0)).row()
    return proxy_row


def selected_source_row(view: QTableView) -> int | None:
    """Fila de origen seleccionada en una tabla, o None si no hay selección."""
    selection = view.selectionModel()
    rows = selection.selectedRows() if selection is not None else []
    if not rows:
        return None
    return source_row(view, rows[0].row())


def section_title(text: str, parent: QWidget | None = None) -> QLabel:
    return QLabel(text, parent, objectName="sectionTitle")


def hint_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    """Texto secundario en gris, con salto de línea."""
    label = QLabel(text, parent, objectName="hint")
    label.setWordWrap(True)
    return label


def panel(parent: QWidget | None = None) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("panel")
    return frame
