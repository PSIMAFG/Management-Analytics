"""Componentes reutilizables: tarjetas de totales, lienzo de gráficos, tablas, leyendas y paneles."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from kpi_monitor.domain.enums import Status
from kpi_monitor.ui.style import BORDER, STATUS_COLORS, STATUS_FILLS, TEXT, TONE_COLORS

ModelIndex = QModelIndex | QPersistentModelIndex


class KpiCard(QFrame):
    """Tarjeta con un total: título, valor destacado y una línea de contexto."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(1)
        self._title = QLabel(title, objectName="kpiTitle")
        self._value = QLabel("-", objectName="kpiValue")
        self._caption = QLabel("", objectName="kpiCaption")
        for label in (self._title, self._value, self._caption):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(label)

    @property
    def value_text(self) -> str:
        return self._value.text()

    @property
    def caption_text(self) -> str:
        return self._caption.text()

    def set_value(self, value: str, caption: str = "", tone: str = "neutral", tooltip: str = "") -> None:
        self._value.setText(value)
        self._value.setStyleSheet(f"color: {TONE_COLORS.get(tone, TONE_COLORS['neutral'])};")
        self._caption.setText(caption)
        self.setToolTip(tooltip)


class KpiStrip(QWidget):
    """Fila de tarjetas de totales siempre visible sobre el contenido."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._cards: dict[str, KpiCard] = {}

    def add_card(self, key: str, title: str, stretch: int = 1) -> KpiCard:
        card = KpiCard(title, self)
        self._cards[key] = card
        self._layout.addWidget(card, stretch)
        return card

    def card(self, key: str) -> KpiCard:
        return self._cards[key]

    def card_keys(self) -> list[str]:
        return list(self._cards)

    def set_value(self, key: str, value: str, caption: str = "", tone: str = "neutral", tooltip: str = "") -> None:
        self._cards[key].set_value(value, caption, tone, tooltip)


class ChartCanvas(QWidget):
    """Figura de matplotlib embebida que se redibuja a pedido.

    `has_data` indica si el último dibujo mostró datos (y no solo un aviso), lo
    que permite a la autoprueba detectar gráficos vacíos.
    """

    def __init__(self, parent: QWidget | None = None, min_height: int = 220) -> None:
        super().__init__(parent)
        self.figure = Figure(layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(min_height)
        self.has_data = False
        self.message = ""
        self._tooltip: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.canvas)

    def clear(self) -> Figure:
        self.figure.clear()
        self.figure.set_layout_engine("constrained")
        self.has_data = True
        self.message = ""
        return self.figure

    def redraw(self) -> None:
        self.canvas.draw_idle()

    def draw_now(self) -> None:
        """Dibuja de inmediato si hay cambios pendientes (la autoprueba lo usa para detectar errores de dibujo)."""
        if self.figure.stale:
            self.canvas.draw()

    def show_tooltip(self, text: str | None) -> None:
        """Ayuda emergente junto al puntero con el detalle del elemento señalado (None la oculta)."""
        if text == self._tooltip:
            return
        self._tooltip = text
        if text:
            QToolTip.showText(QCursor.pos(), text, self.canvas)
        else:
            QToolTip.hideText()

    def show_message(self, message: str) -> None:
        """Muestra un aviso centrado cuando no hay datos que graficar."""
        figure = self.clear()
        # Un aviso no necesita diseño automático (y así no falla con el lienzo aún sin tamaño).
        figure.set_layout_engine("none")
        self.has_data = False
        self.message = message
        ax = figure.add_axes((0, 0, 1, 1))
        ax.axis("off")
        ax.text(0.5, 0.5, message, ha="center", va="center", color=TONE_COLORS["muted"], wrap=True)
        self.redraw()


@dataclass(frozen=True)
class Column:
    """Definición de una columna: clave del registro, encabezado, formato y estilo opcional.

    `sort_key` define el valor por el que se ordena (por defecto, el valor crudo);
    `background`, `foreground` y `tooltip` reciben el valor crudo de la celda.
    """

    key: str
    header: str
    formatter: Callable[[Any], str] = str
    numeric: bool = False
    sort_key: Callable[[Any], Any] | None = None
    background: Callable[[Any], str | None] | None = None
    foreground: Callable[[Any], str | None] | None = None
    tooltip: Callable[[Any], str] | None = None
    center: bool = False


class RecordTableModel(QAbstractTableModel):
    """Modelo de solo lectura para listas de registros (dict o dataclass)."""

    def __init__(
        self,
        columns: Sequence[Column],
        parent: QWidget | None = None,
        row_bold: Callable[[Any], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[Any] = []
        self._row_bold = row_bold
        self._bold = QFont()
        self._bold.setBold(True)

    @property
    def columns(self) -> list[Column]:
        return list(self._columns)

    def set_columns(self, columns: Sequence[Column]) -> None:
        self.beginResetModel()
        self._columns = list(columns)
        self.endResetModel()

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[Any]:
        return list(self._rows)

    def row(self, index: int) -> Any:
        return self._rows[index]

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns)

    @staticmethod
    def _raw(row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        return getattr(row, key)

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        column = self._columns[index.column()]
        record = self._rows[index.row()]
        value = self._raw(record, column.key)
        if role == Qt.ItemDataRole.DisplayRole:
            return "-" if value is None else column.formatter(value)
        if role == Qt.ItemDataRole.UserRole:
            return column.sort_key(value) if column.sort_key is not None else value
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column.center:
                return int(Qt.AlignmentFlag.AlignCenter)
            if column.numeric:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.BackgroundRole and column.background is not None:
            color = column.background(value)
            return QBrush(QColor(color)) if color else None
        if role == Qt.ItemDataRole.ForegroundRole and column.foreground is not None:
            color = column.foreground(value)
            return QBrush(QColor(color)) if color else None
        if role == Qt.ItemDataRole.ToolTipRole:
            if column.tooltip is not None:
                return column.tooltip(value) or None
            return None if value is None else column.formatter(value)
        if role == Qt.ItemDataRole.FontRole and self._row_bold is not None and self._row_bold(record):
            return self._bold
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._columns[section].header
        if role == Qt.ItemDataRole.TextAlignmentRole:
            # El encabezado se alinea igual que su columna: números a la derecha, texto a la izquierda.
            column = self._columns[section]
            if column.center:
                return int(Qt.AlignmentFlag.AlignCenter)
            horizontal = Qt.AlignmentFlag.AlignRight if column.numeric else Qt.AlignmentFlag.AlignLeft
            return int(horizontal | Qt.AlignmentFlag.AlignVCenter)
        return None


def distribute_widths(widths: Sequence[int], available: int) -> list[int]:
    """Anchos de columna que llenan el espacio disponible.

    Si el contenido no cabe, cada columna queda a su medida (la tabla se desplaza
    en horizontal); si sobra espacio, se reparte en proporción al ancho de cada una.
    """
    total = sum(widths)
    extra = available - total
    if extra <= 0 or total <= 0:
        return list(widths)
    result = [width + extra * width // total for width in widths]
    result[-1] += available - sum(result)
    return result


class ColumnFitter(QObject):
    """Ajusta las columnas de una tabla a su contenido y reparte el espacio que sobra.

    Evita dos defectos de estirar solo la última columna: una columna final
    desmedida cuando sobra espacio y una columna recortada cuando falta.
    """

    def __init__(self, view: QTableView) -> None:
        super().__init__(view)
        self._view = view
        self.enabled = True
        view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)
        view.model().modelReset.connect(self.schedule)
        view.model().layoutChanged.connect(self.schedule)
        view.horizontalHeader().sortIndicatorChanged.connect(self.schedule)

    def schedule(self) -> None:
        """Ajusta después de que la tabla procese el contenido nuevo."""
        QTimer.singleShot(0, self, self.fit)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Se ajusta en diferido: al mostrarse la tabla, la vista interior cambia de tamaño después del aviso.
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.schedule()
        return super().eventFilter(watched, event)

    def content_widths(self) -> list[int]:
        """Ancho que necesita cada columna para mostrar su encabezado y una muestra de filas."""
        header = self._view.horizontalHeader()
        return [
            max(self._view.sizeHintForColumn(column), header.sectionSizeFromContents(column).width())
            for column in range(header.count())
        ]

    def fit(self) -> None:
        header = self._view.horizontalHeader()
        if not self.enabled or header.count() == 0:
            return
        widths = distribute_widths(self.content_widths(), self._view.viewport().width())
        for column, width in enumerate(widths):
            header.resizeSection(column, width)


def make_table_view(model: QAbstractTableModel, parent: QWidget | None = None, sortable: bool = True) -> QTableView:
    """Tabla ordenable (por el valor crudo, no por el texto formateado)."""
    proxy = QSortFilterProxyModel(parent)
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.ItemDataRole.UserRole)
    view = QTableView(parent)
    view.setModel(proxy)
    header = view.horizontalHeader()
    # Sin orden inicial: las filas llegan en el orden del catálogo o de gravedad; el usuario ordena con un clic.
    header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    view.setSortingEnabled(sortable)
    # La flecha de orden reserva espacio en cada encabezado; se muestra recién cuando el usuario ordena.
    header.setSortIndicatorShown(False)
    if sortable:
        header.sortIndicatorChanged.connect(lambda *_args: header.setSortIndicatorShown(True))
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(26)
    # El ancho se calcula con una muestra de filas: con miles de observaciones medirlas todas es lento.
    header.setResizeContentsPrecision(120)
    header.setHighlightSections(False)
    ColumnFitter(view)
    return view


def source_row(view: QTableView, index: QModelIndex | None = None) -> int | None:
    """Fila del modelo de origen para el índice dado o la fila seleccionada de una tabla ordenable."""
    if index is None:
        selected = view.selectionModel().selectedRows() if view.selectionModel() else []
        if not selected:
            return None
        index = selected[0]
    model = view.model()
    if isinstance(model, QSortFilterProxyModel):
        index = model.mapToSource(index)
    return index.row() if index.isValid() else None


def set_stretch_columns(view: QTableView, columns: Sequence[int]) -> None:
    """Reparte el espacio de la tabla entre las columnas indicadas (el resto queda a la medida del contenido)."""
    fitter = view.findChild(ColumnFitter)
    if fitter is not None:
        fitter.enabled = False
    header = view.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for column in columns:
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)


def set_stretch_column(view: QTableView, column: int) -> None:
    """Deja una columna de texto largo ocupando el espacio sobrante sin ensanchar la tabla."""
    set_stretch_columns(view, [column])


def section_title(text: str, parent: QWidget | None = None) -> QLabel:
    return QLabel(text, parent, objectName="sectionTitle")


def field_label(text: str, parent: QWidget | None = None) -> QLabel:
    return QLabel(text, parent, objectName="fieldLabel")


def panel(parent: QWidget | None = None, name: str = "panel") -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName(name)
    return frame


def chip_style(status: Status) -> str:
    """Estilo de una etiqueta de estado: fondo suave del semáforo y borde del color del estado."""
    return f"background: {STATUS_FILLS[status]}; color: {TEXT}; border: 1px solid {STATUS_COLORS[status]};"


class StatusChip(QLabel):
    """Etiqueta con el nombre del estado sobre su color de semáforo (nunca solo color)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent, objectName="chip")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_status(self, status: Status, text: str | None = None) -> None:
        self.setText(text or status.label)
        self.setStyleSheet(chip_style(status))


class StatusLegend(QWidget):
    """Leyenda horizontal de estados: cuadro de color y nombre."""

    def __init__(self, items: Sequence[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for color, text in items:
            swatch = QLabel()
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background: {color}; border: 1px solid {BORDER}; border-radius: 2px;")
            item = QHBoxLayout()
            item.setSpacing(5)
            item.addWidget(swatch)
            item.addWidget(QLabel(text, objectName="legendText"))
            layout.addLayout(item)
        layout.addStretch(1)


class InfoPanel(QFrame):
    """Panel de pares campo-valor con un título y una etiqueta de estado."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("infoPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self._title = QLabel(title, objectName="infoTitle")
        self._title.setWordWrap(True)
        self.chip = StatusChip(self)
        layout.addWidget(self._title)
        layout.addWidget(self.chip)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        layout.addLayout(self._grid)
        self._note = QLabel("", objectName="hint")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        layout.addStretch(1)
        self._pairs: list[tuple[str, str]] = []
        self._labels: list[tuple[QLabel, QLabel]] = []

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return list(self._pairs)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def _ensure_rows(self, count: int) -> None:
        """Crea las filas que falten; las etiquetas se reutilizan entre actualizaciones."""
        while len(self._labels) < count:
            row = len(self._labels)
            key_label = QLabel("", objectName="infoKey")
            key_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            key_label.setWordWrap(True)
            key_label.setFixedWidth(118)
            value_label = QLabel("", objectName="infoValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._grid.addWidget(key_label, row, 0)
            self._grid.addWidget(value_label, row, 1)
            self._labels.append((key_label, value_label))

    def set_rows(self, pairs: Sequence[tuple[str, str]], note: str = "") -> None:
        self._ensure_rows(len(pairs))
        for row, (key_label, value_label) in enumerate(self._labels):
            visible = row < len(pairs)
            if visible:
                key_label.setText(pairs[row][0])
                value_label.setText(pairs[row][1])
            key_label.setVisible(visible)
            value_label.setVisible(visible)
        self._pairs = list(pairs)
        self._note.setText(note)
        self._note.setVisible(bool(note))


class ViewSwitch(QWidget):
    """Alterna entre vistas del mismo contenido (por ejemplo, gráfico y tabla equivalente).

    Arriba muestra los botones de cada vista y un texto de contexto; debajo, la vista elegida.
    """

    def __init__(self, pages: Sequence[tuple[str, QWidget]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stack = QStackedWidget(self)
        self.caption = QLabel("", objectName="tableCaption")
        self.caption.setWordWrap(True)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        header = QHBoxLayout()
        header.setContentsMargins(8, 6, 8, 0)
        header.setSpacing(0)
        self.buttons: list[QPushButton] = []
        for index, (label, widget) in enumerate(pages):
            button = QPushButton(label, objectName="segment")
            button.setCheckable(True)
            button.setProperty("position", "first" if index == 0 else "last" if index == len(pages) - 1 else "middle")
            self._group.addButton(button, index)
            self.buttons.append(button)
            header.addWidget(button)
            self.stack.addWidget(widget)
        header.addSpacing(12)
        header.addWidget(self.caption, 1)
        self._group.idClicked.connect(self.stack.setCurrentIndex)
        self.buttons[0].setChecked(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.stack, 1)

    def set_page(self, index: int) -> None:
        self.buttons[index].setChecked(True)
        self.stack.setCurrentIndex(index)

    def current_page(self) -> int:
        return self.stack.currentIndex()


class ElidedComboBox(QComboBox):
    """Combo que abrevia con puntos suspensivos el texto que no cabe, en vez de cortarlo.

    La lista desplegable muestra siempre el texto completo, y la ayuda emergente también.
    """

    def __init__(self, parent: QWidget | None = None, popup_width: int = 0) -> None:
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        if popup_width:
            self.view().setMinimumWidth(popup_width)
        self.currentTextChanged.connect(self.setToolTip)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, self
        )
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, max(0, field.width() - 4)
        )
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
