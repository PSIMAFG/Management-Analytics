"""Componentes reutilizables: tarjetas de totales, lienzo de gráficos y tablas."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.backend_bases import Event, MouseEvent
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.text import Text
from matplotlib.transforms import Bbox
from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QResizeEvent, QShowEvent
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

from staffing_simulator.ui.style import TONE_COLORS

ModelIndex = QModelIndex | QPersistentModelIndex
HoverText = str | Sequence[str]
Hover = Callable[[Artist, HoverText], None]
Painter = Callable[[Figure, Hover], None]

TOTAL_KEY = "_total"
GROUP_KEY = "_group"
GROUP_BACKGROUND = "#EEF1F5"
VALUE_POINTS = 15.0
MIN_VALUE_POINTS = 10.5
CARD_MARGIN = 10
# Ancho mínimo de la columna de relleno de una tabla antes de mostrar la barra horizontal.
MIN_FILL_WIDTH = 80
MAX_COLUMN_WIDTH = 260
MIN_COLUMN_WIDTH = 60


class KpiCard(QFrame):
    """Tarjeta con un total: título, valor destacado y una o dos líneas de contexto.

    Si la tarjeta queda angosta, el valor reduce su fuente y el título se
    abrevia con puntos suspensivos (el título completo queda en su ayuda).
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(120)
        self._title = title
        self._color = TONE_COLORS["neutral"]
        self._points = VALUE_POINTS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_MARGIN, 8, CARD_MARGIN, 8)
        layout.setSpacing(1)
        self.title_label = QLabel(title, objectName="kpiTitle")
        self.value_label = QLabel("-", objectName="kpiValue")
        self.caption_label = QLabel("", objectName="kpiCaption")
        self.caption_label.setWordWrap(True)
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        line_height = self.caption_label.fontMetrics().lineSpacing()
        self.caption_label.setMinimumHeight(2 * line_height + 2)
        for label in (self.title_label, self.value_label, self.caption_label):
            layout.addWidget(label)
        layout.addStretch(1)

    def set_value(self, value: str, caption: str = "", tone: str = "neutral", tooltip: str = "") -> None:
        self.value_label.setText(value)
        self._color = TONE_COLORS.get(tone, TONE_COLORS["neutral"])
        self.caption_label.setText(caption)
        self.setToolTip(tooltip)
        self._fit_value()

    def _available_width(self) -> int:
        return self.width() - 2 * CARD_MARGIN - 2

    def _fit_title(self) -> None:
        """Abrevia el título con puntos suspensivos si no cabe; el texto completo queda en su ayuda."""
        metrics = self.title_label.fontMetrics()
        shown = metrics.elidedText(self._title, Qt.TextElideMode.ElideRight, self._available_width())
        self.title_label.setText(shown)
        self.title_label.setToolTip("" if shown == self._title else self._title)

    def _fit_value(self) -> None:
        """Reduce el tamaño del valor cuando la tarjeta es angosta, para que nunca quede cortado."""
        available = self._available_width()
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        points = VALUE_POINTS
        while points > MIN_VALUE_POINTS:
            font.setPointSizeF(points)
            if QFontMetrics(font).horizontalAdvance(self.value_label.text()) <= available:
                break
            points -= 0.5
        self._points = points
        self.value_label.setStyleSheet(f"color: {self._color}; font-size: {points}pt;")

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_title()
        self._fit_value()

    def value_text(self) -> str:
        return self.value_label.text()

    def truncated_labels(self) -> list[str]:
        """Textos que no caben o que se abreviaron (para la autoprueba de diseño)."""
        problems = [self._title] if self.title_label.text() != self._title else []
        for label in (self.title_label, self.value_label):
            needed = label.fontMetrics().horizontalAdvance(label.text())
            if needed > label.contentsRect().width() + 1:
                problems.append(label.text())
        caption = self.caption_label
        if caption.text() and caption.heightForWidth(caption.width()) > caption.height() + 1:
            problems.append(caption.text())
        return problems


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
        self._layout.addWidget(card, 1)
        return card

    def card(self, key: str) -> KpiCard:
        return self._cards[key]

    def cards(self) -> dict[str, KpiCard]:
        return dict(self._cards)

    def set_value(self, key: str, value: str, caption: str = "", tone: str = "neutral", tooltip: str = "") -> None:
        self._cards[key].set_value(value, caption, tone, tooltip)

    def clear(self, caption: str = "") -> None:
        for card in self._cards.values():
            card.set_value("-", caption, "muted")


def draw_message(figure: Figure, message: str) -> None:
    """Aviso centrado en lugar del gráfico (estado vacío o error)."""
    ax = figure.add_subplot()
    ax.axis("off")
    ax.text(0.5, 0.5, message, ha="center", va="center", color=TONE_COLORS["muted"], fontsize=10, wrap=True)


def _title_texts(ax: Axes) -> list[Text]:
    """Títulos no vacíos del eje, en cualquiera de sus tres posiciones (izquierda, centro o derecha)."""
    titles = {ax.get_title(loc=loc) for loc in ("left", "center", "right")} - {""}
    return [child for child in ax.get_children() if isinstance(child, Text) and child.get_text() in titles]


class ChartCanvas(QWidget):
    """Figura de matplotlib embebida.

    El gráfico se describe con una función de dibujo (`plot`) y se dibuja
    cuando el lienzo está visible, lo que evita trabajo en pestañas ocultas.
    Los elementos registrados con `hover` muestran su valor al pasar el mouse.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.figure = Figure(layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.setMinimumSize(200, 160)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.canvas)
        self._painter: Painter | None = None
        self._pending = False
        self._message: str | None = None
        self._hover: list[tuple[Artist, HoverText]] = []
        self._tooltip_visible = False
        self.draw_count = 0
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("figure_leave_event", self._on_leave)

    @property
    def message(self) -> str | None:
        """Aviso mostrado en lugar del gráfico, o None si hay un gráfico con datos."""
        return self._message

    def plot(self, painter: Painter) -> None:
        """Registra cómo dibujar el gráfico; se dibuja ahora si está visible o al mostrarse."""
        self._painter = painter
        self._message = None
        self._pending = True
        if self.isVisible():
            self.draw_now()

    def show_message(self, message: str) -> None:
        """Muestra un aviso centrado cuando no hay datos que graficar."""
        self.plot(lambda figure, _hover: draw_message(figure, message))
        self._message = message

    def draw_now(self) -> None:
        """Dibuja de inmediato el gráfico registrado."""
        self._pending = False
        self._hover.clear()
        self.figure.clear()
        if self._painter is not None:
            self._painter(self.figure, self.hover)
        self.canvas.draw()
        self.draw_count += 1

    def draw_if_pending(self) -> None:
        """Dibuja solo si hay un gráfico registrado que aún no se dibujó (o que cambió)."""
        if self._pending or (self.draw_count == 0 and self._painter is not None):
            self.draw_now()

    def hover(self, artist: Artist, text: HoverText) -> None:
        """Asocia un texto (o un texto por punto, en líneas) a un elemento del gráfico."""
        if isinstance(artist, Line2D):
            artist.set_pickradius(8)
        self._hover.append((artist, text))

    def has_data(self) -> bool:
        return self._message is None and any(ax.has_data() for ax in self.figure.axes)

    def clipped_texts(self) -> list[str]:
        """Textos mal ubicados, para la autoprueba de diseño.

        Informa los gráficos cuyo contenido queda fuera de la figura y los
        títulos que invaden otro gráfico de la misma figura (un título más
        ancho que su columna no lo corrige el diseño automático de matplotlib).
        """
        renderer = self.canvas.get_renderer()
        bounds = self.figure.bbox

        def outside(box: Bbox | None) -> bool:
            if box is None:
                return False
            return box.x0 < bounds.x0 - 1 or box.x1 > bounds.x1 + 1 or box.y0 < bounds.y0 - 1 or box.y1 > bounds.y1 + 1

        axes = [ax for ax in self.figure.axes if ax.get_visible()]
        problems = [
            ax.get_title(loc="left") or ax.get_title() or "gráfico sin título"
            for ax in axes
            if outside(ax.get_tightbbox(renderer))
        ]
        problems += [
            text.get_text()
            for text in self.figure.texts
            if text.get_text() and outside(text.get_window_extent(renderer))
        ]
        for ax in axes:
            others = [box for other in axes if other is not ax and (box := other.get_tightbbox(renderer)) is not None]
            for title in _title_texts(ax):
                extent = title.get_window_extent(renderer).padded(-1)
                if any(extent.overlaps(box) for box in others):
                    problems.append(f"{title.get_text()} (invade otro gráfico)")
        return problems

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.draw_if_pending()

    def _hover_text(self, event: MouseEvent) -> str | None:
        for artist, text in reversed(self._hover):
            contains, details = artist.contains(event)
            if not contains:
                continue
            if isinstance(text, str):
                return text
            indexes = details.get("ind", [])
            if len(indexes):
                return text[int(indexes[0])]
        return None

    def _on_motion(self, event: Event) -> None:
        if not isinstance(event, MouseEvent) or event.inaxes is None or not self._hover:
            self._hide_tooltip()
            return
        text = self._hover_text(event)
        if text is None:
            self._hide_tooltip()
            return
        QToolTip.showText(QCursor.pos(), text, self.canvas)
        self._tooltip_visible = True

    def _on_leave(self, _event: Event) -> None:
        self._hide_tooltip()

    def _hide_tooltip(self) -> None:
        if self._tooltip_visible:
            QToolTip.hideText()
            self._tooltip_visible = False


@dataclass(frozen=True)
class Column:
    """Definición de una columna: clave del registro, encabezado y formato.

    - `sort_key`: clave alternativa con el valor por el que se ordena.
    - `tone`: función que devuelve un color de texto (o None) según el valor.
    - `tooltip_key`: clave con un texto de ayuda para la celda.
    - `header_tooltip`: texto de ayuda del encabezado.
    """

    key: str
    header: str
    formatter: Callable[[Any], str] = str
    numeric: bool = False
    sort_key: str | None = None
    tone: Callable[[Any], str | None] | None = None
    tooltip_key: str | None = None
    header_tooltip: str = ""


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _sortable(value: Any) -> Any:
    """Valor que Qt sabe comparar al ordenar (Decimal no lo es)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return QDate(value.year, value.month, value.day)
    return value


class RecordTableModel(QAbstractTableModel):
    """Modelo de solo lectura para listas de registros (dict o dataclass).

    Las filas con la clave `_total` verdadera se muestran en negrita; las filas
    con `_group` son títulos de sección (solo la primera columna, con fondo).
    """

    def __init__(self, columns: Sequence[Column], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[Any] = []

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def set_columns(self, columns: Sequence[Column], rows: Sequence[Any] | None = None) -> None:
        self.beginResetModel()
        self._columns = list(columns)
        if rows is not None:
            self._rows = list(rows)
        self.endResetModel()

    def columns(self) -> list[Column]:
        return list(self._columns)

    def rows(self) -> list[Any]:
        return list(self._rows)

    def row(self, index: int) -> Any:
        return self._rows[index]

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns)

    def display_text(self, row: int, column: int) -> str:
        """Texto que se muestra en la celda (útil para pruebas y exportaciones)."""
        return str(self.data(self.index(row, column), Qt.ItemDataRole.DisplayRole))

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        column = self._columns[index.column()]
        record = self._rows[index.row()]
        value = _get(record, column.key)
        group = bool(_get(record, GROUP_KEY, False))
        if role == Qt.ItemDataRole.DisplayRole:
            if group:
                return str(value) if index.column() == 0 and value is not None else ""
            return "-" if value is None else column.formatter(value)
        if role == Qt.ItemDataRole.UserRole:
            return _sortable(_get(record, column.sort_key) if column.sort_key else value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            horizontal = Qt.AlignmentFlag.AlignRight if column.numeric else Qt.AlignmentFlag.AlignLeft
            return int(horizontal | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and column.tone is not None and value is not None:
            color = column.tone(value)
            return None if color is None else QColor(color)
        if role == Qt.ItemDataRole.FontRole and (group or _get(record, TOTAL_KEY, False)):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.BackgroundRole and group:
            return QColor(GROUP_BACKGROUND)
        if role == Qt.ItemDataRole.ToolTipRole and column.tooltip_key:
            return _get(record, column.tooltip_key)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal or section >= len(self._columns):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._columns[section].header
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._columns[section].header_tooltip or None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            horizontal = Qt.AlignmentFlag.AlignRight if self._columns[section].numeric else Qt.AlignmentFlag.AlignLeft
            return int(horizontal | Qt.AlignmentFlag.AlignVCenter)
        return None


class RecordTableView(QTableView):
    """Tabla con columnas ajustadas a su contenido; el espacio sobrante va a una sola columna.

    Si falta espacio, esa misma columna (la de textos largos, como la persona)
    se angosta hasta MIN_FILL_WIDTH y sus textos se abrevian con puntos
    suspensivos; solo por debajo de ese mínimo aparece la barra horizontal.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.fill_column: int | None = None

    def set_fill_column(self, column: int | None) -> None:
        self.fill_column = column
        self.fit_columns()

    def fit_columns(self) -> None:
        header = self.horizontalHeader()
        count = header.count()
        if count == 0:
            return
        self.resizeColumnsToContents()
        # Un solo valor inusualmente largo (por ejemplo, el nombre de una sede o un cargo
        # extenso) no debe ensanchar su columna más de la cuenta: se acota y el texto se
        # abrevia con puntos suspensivos (comportamiento por defecto de la tabla).
        for section in range(count):
            if header.sectionSize(section) > MAX_COLUMN_WIDTH:
                header.resizeSection(section, MAX_COLUMN_WIDTH)
        column = count - 1 if self.fill_column is None or self.fill_column >= count else self.fill_column
        scrollbar = self.verticalScrollBar()
        reserve = 0 if scrollbar.isVisible() else scrollbar.sizeHint().width()
        spare = self.viewport().width() - header.length() - reserve
        current = header.sectionSize(column)
        if spare > 0:
            header.resizeSection(column, current + spare)
        elif spare < 0 and current > MIN_FILL_WIDTH:
            header.resizeSection(column, max(MIN_FILL_WIDTH, current + spare))
        self._shrink_to_fit(reserve)

    def _shrink_to_fit(self, reserve: int) -> None:
        """Si aun así las columnas no caben (muchas con texto largo y una ventana angosta),
        angosta todas proporcionalmente hasta un mínimo, abreviando con puntos suspensivos,
        en vez de dejar aparecer la barra de desplazamiento horizontal.
        """
        header = self.horizontalHeader()
        count = header.count()
        overflow = header.length() - (self.viewport().width() - reserve)
        if overflow <= 0:
            return
        shrinkable = [section for section in range(count) if header.sectionSize(section) > MIN_COLUMN_WIDTH]
        while overflow > 0 and shrinkable:
            share = max(1, overflow // len(shrinkable))
            for section in list(shrinkable):
                size = header.sectionSize(section)
                reduction = min(share, size - MIN_COLUMN_WIDTH, overflow)
                if reduction <= 0:
                    shrinkable.remove(section)
                    continue
                header.resizeSection(section, size - reduction)
                overflow -= reduction
                if overflow <= 0:
                    break

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.fit_columns()


def make_table_view(
    model: QAbstractTableModel,
    parent: QWidget | None = None,
    *,
    sortable: bool = True,
    stretch_column: int | None = None,
) -> RecordTableView:
    """Tabla ordenable (por el valor crudo, no por el texto formateado).

    Mantiene el orden del modelo hasta que el usuario pulse un encabezado.
    `stretch_column` indica la columna que recibe el espacio sobrante (por
    defecto, la última).
    """
    proxy = QSortFilterProxyModel(parent)
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.ItemDataRole.UserRole)
    proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    proxy.setFilterKeyColumn(-1)
    view = RecordTableView(parent)
    view.setModel(proxy)
    view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    view.setSortingEnabled(sortable)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setWordWrap(False)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(24)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setSortIndicatorShown(sortable)
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    view.fill_column = stretch_column
    for signal in (proxy.modelReset, proxy.layoutChanged, proxy.rowsInserted, proxy.rowsRemoved):
        signal.connect(view.fit_columns)
    return view


def set_stretch_column(view: QTableView, column: int | None) -> None:
    """Columna que recibe el espacio sobrante de la tabla (None: la última)."""
    if isinstance(view, RecordTableView):
        view.set_fill_column(column)


def proxy_of(view: QTableView) -> QSortFilterProxyModel:
    return cast(QSortFilterProxyModel, view.model())


def selected_records(view: QTableView, model: RecordTableModel) -> list[Any]:
    """Registros seleccionados en la vista, en el orden en que se muestran."""
    proxy = proxy_of(view)
    rows = sorted({index.row() for index in view.selectionModel().selectedRows()})
    return [model.row(proxy.mapToSource(proxy.index(row, 0)).row()) for row in rows]


def section_title(text: str, parent: QWidget | None = None) -> QLabel:
    return QLabel(text, parent, objectName="sectionTitle")


def muted_label(text: str = "", parent: QWidget | None = None, *, wrap: bool = True) -> QLabel:
    label = QLabel(text, parent, objectName="muted")
    label.setWordWrap(wrap)
    return label


def panel(parent: QWidget | None = None) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("panel")
    return frame
