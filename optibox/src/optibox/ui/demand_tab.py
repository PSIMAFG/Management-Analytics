"""Pestaña de demanda: tabla editable con validación por celda y mapa de la demanda registrada."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from optibox.domain.models import PRIORITY_LABELS, DemandItem
from optibox.domain.run import RunDetail
from optibox.domain.timegrid import WEEKDAY_NAMES
from optibox.errors import AppError
from optibox.services.master_data_service import MAX_SESSIONS_PER_BLOCK, DemandRow, MasterDataService
from optibox.ui.charts import draw_block_grid
from optibox.ui.dialogs import show_error
from optibox.ui.formatting import format_count, format_int
from optibox.ui.presentation import demand_grid
from optibox.ui.style import EDITED
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import ChartCanvas, hint_label, make_table_view

log = logging.getLogger(__name__)

ModelIndex = QModelIndex | QPersistentModelIndex

DAY_COL, BLOCK_COL, NAME_COL, SESSIONS_COL, PRIORITY_COL = range(5)
HEADERS = ("Día", "Bloque", "Tipo de atención", "Sesiones", "Prioridad")
DEMAND_HINT = (
    "Doble clic en Sesiones o Prioridad para editar. Los cambios se usan en la próxima optimización; "
    "las corridas guardadas conservan la demanda con que se calcularon."
)


def demand_value_error(column: int, value: Any) -> str | None:
    """Motivo por el que un valor no es válido para la columna editada, o None si es válido."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "Ingrese un número entero."
    if isinstance(value, float) and not value.is_integer():
        return "Ingrese un número entero."
    if column == SESSIONS_COL and not 0 <= number <= MAX_SESSIONS_PER_BLOCK:
        return f"Las sesiones requeridas deben estar entre 0 y {MAX_SESSIONS_PER_BLOCK}."
    if column == PRIORITY_COL and number not in PRIORITY_LABELS:
        return "La prioridad debe ser 1 (alta), 2 (media) o 3 (baja)."
    return None


@dataclass
class DemandCell:
    """Fila editable: los valores guardados y los actuales (con cambios sin guardar)."""

    row: DemandRow
    sessions: int
    priority: int

    @property
    def changed(self) -> bool:
        return self.sessions != self.row.sessions or self.priority != self.row.priority

    def item(self) -> DemandItem:
        return DemandItem(self.row.weekday, self.row.block_start, self.row.service_code, self.sessions, self.priority)


class DemandTableModel(QAbstractTableModel):
    """Modelo editable de la demanda con validación por celda y registro de cambios pendientes."""

    validation_failed = Signal(str)
    edits_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cells: list[DemandCell] = []

    def load(self, rows: Sequence[DemandRow]) -> None:
        self.beginResetModel()
        self._cells = [DemandCell(row, row.sessions, row.priority) for row in rows]
        self.endResetModel()
        self.edits_changed.emit()

    def cell(self, row: int) -> DemandCell:
        return self._cells[row]

    def cells(self) -> tuple[DemandCell, ...]:
        return tuple(self._cells)

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._cells)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if index.isValid() and index.column() in (SESSIONS_COL, PRIORITY_COL):
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        cell = self._cells[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(cell, column)
        if role in (Qt.ItemDataRole.EditRole, Qt.ItemDataRole.UserRole):
            return self._raw(cell, column)
        edited = self._edited(cell, column)
        if role == Qt.ItemDataRole.BackgroundRole and edited:
            return QColor(EDITED)
        if role == Qt.ItemDataRole.FontRole and edited:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.ToolTipRole and edited:
            saved = cell.row.sessions if column == SESSIONS_COL else PRIORITY_LABELS[cell.row.priority]
            return f"Cambio sin guardar. Valor guardado: {saved}"
        if role == Qt.ItemDataRole.TextAlignmentRole and column == SESSIONS_COL:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _edited(cell: DemandCell, column: int) -> bool:
        if column == SESSIONS_COL:
            return cell.sessions != cell.row.sessions
        if column == PRIORITY_COL:
            return cell.priority != cell.row.priority
        return False

    @staticmethod
    def _display(cell: DemandCell, column: int) -> str:
        values = {
            DAY_COL: cell.row.day_name,
            BLOCK_COL: cell.row.block_label,
            NAME_COL: cell.row.service_name,
            SESSIONS_COL: format_int(cell.sessions),
            PRIORITY_COL: PRIORITY_LABELS[cell.priority],
        }
        return values[column]

    @staticmethod
    def _raw(cell: DemandCell, column: int) -> Any:
        values = {
            DAY_COL: cell.row.weekday,
            BLOCK_COL: cell.row.block_start,
            NAME_COL: cell.row.service_name,
            SESSIONS_COL: cell.sessions,
            PRIORITY_COL: cell.priority,
        }
        return values[column]

    def setData(self, index: ModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        column = index.column()
        if column not in (SESSIONS_COL, PRIORITY_COL):
            return False
        error = demand_value_error(column, value)
        if error is not None:
            self.validation_failed.emit(error)
            return False
        cell = self._cells[index.row()]
        if column == SESSIONS_COL:
            cell.sessions = int(value)
        else:
            cell.priority = int(value)
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), len(HEADERS) - 1))
        self.edits_changed.emit()
        return True

    def pending_items(self) -> list[DemandItem]:
        return [cell.item() for cell in self._cells if cell.changed]

    def revert(self) -> None:
        self.beginResetModel()
        for cell in self._cells:
            cell.sessions, cell.priority = cell.row.sessions, cell.row.priority
        self.endResetModel()
        self.edits_changed.emit()

    def values(self) -> Iterable[tuple[int, int, str, int]]:
        for cell in self._cells:
            yield cell.row.weekday, cell.row.block_start, cell.row.service_code, cell.sessions

    def open_blocks(self) -> dict[int, list[int]]:
        blocks: dict[int, set[int]] = defaultdict(set)
        for cell in self._cells:
            blocks[cell.row.weekday].add(cell.row.block_start)
        return {weekday: sorted(values) for weekday, values in blocks.items()}


class DemandFilter(QSortFilterProxyModel):
    """Filtro por día, tipo de atención y filas con demanda."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.weekday: int | None = None
        self.service: str | None = None
        self.only_positive = False

    def set_filters(self, weekday: int | None, service: str | None, only_positive: bool) -> None:
        # Qt 6.10 reemplaza invalidateFilter por el par beginFilterChange/endFilterChange.
        modern = hasattr(self, "beginFilterChange")
        if modern:
            self.beginFilterChange()
        self.weekday, self.service, self.only_positive = weekday, service, only_positive
        if modern:
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, _source_parent: ModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, DemandTableModel):
            return True
        cell = model.cell(source_row)
        if self.weekday is not None and cell.row.weekday != self.weekday:
            return False
        if self.service is not None and cell.row.service_code != self.service:
            return False
        return not (self.only_positive and cell.sessions == 0 and cell.row.sessions == 0)


class SessionsDelegate(QStyledItemDelegate):
    """Editor de sesiones acotado al rango válido."""

    def createEditor(self, parent: QWidget, _option: QStyleOptionViewItem, _index: ModelIndex) -> QWidget:
        editor = QSpinBox(parent)
        editor.setRange(0, MAX_SESSIONS_PER_BLOCK)
        return editor

    def setEditorData(self, editor: QWidget, index: ModelIndex) -> None:
        if isinstance(editor, QSpinBox):
            editor.setValue(int(index.data(Qt.ItemDataRole.EditRole) or 0))

    def setModelData(self, editor: QWidget, model: QAbstractTableModel, index: ModelIndex) -> None:
        if isinstance(editor, QSpinBox):
            editor.interpretText()
            model.setData(index, editor.value(), Qt.ItemDataRole.EditRole)


class PriorityDelegate(QStyledItemDelegate):
    """Editor de prioridad con las tres opciones válidas."""

    def createEditor(self, parent: QWidget, _option: QStyleOptionViewItem, _index: ModelIndex) -> QWidget:
        editor = QComboBox(parent)
        for value, label in PRIORITY_LABELS.items():
            editor.addItem(label, value)
        return editor

    def setEditorData(self, editor: QWidget, index: ModelIndex) -> None:
        if isinstance(editor, QComboBox):
            editor.setCurrentIndex(max(0, editor.findData(index.data(Qt.ItemDataRole.EditRole))))

    def setModelData(self, editor: QWidget, model: QAbstractTableModel, index: ModelIndex) -> None:
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentData(), Qt.ItemDataRole.EditRole)


class DemandTab(TabPage):
    title = "Demanda"
    status_message = Signal(str)
    demand_saved = Signal(int)

    def __init__(self, master_data: MasterDataService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.master_data = master_data
        self.model = DemandTableModel(self)
        self.model.validation_failed.connect(self.status_message.emit)
        self.model.edits_changed.connect(self._on_edits_changed)
        self.proxy = DemandFilter(self)
        self.table = make_table_view(self.model, self, self.proxy, stretch_column=NAME_COL)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.table.setItemDelegateForColumn(SESSIONS_COL, SessionsDelegate(self.table))
        self.table.setItemDelegateForColumn(PRIORITY_COL, PriorityDelegate(self.table))

        self.day_combo = QComboBox()
        self.day_combo.addItem("Todos los días", None)
        for index, name in enumerate(WEEKDAY_NAMES):
            self.day_combo.addItem(name.capitalize(), index)
        self.service_combo = QComboBox()
        self.service_combo.setMinimumWidth(200)
        self.only_positive = QCheckBox("Solo bloques con demanda")
        self.only_positive.setChecked(True)
        for widget in (self.day_combo, self.service_combo):
            widget.currentIndexChanged.connect(self._apply_filters)
        self.only_positive.toggled.connect(self._apply_filters)
        self.pending_label = QLabel("")
        self.discard_button = QPushButton("Descartar cambios")
        self.discard_button.clicked.connect(self.discard_changes)
        self.save_button = QPushButton("Guardar cambios", objectName="primary")
        self.save_button.clicked.connect(self.save_changes)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Día"))
        controls.addWidget(self.day_combo)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Tipo de atención"))
        controls.addWidget(self.service_combo)
        controls.addSpacing(8)
        controls.addWidget(self.only_positive)
        controls.addStretch(1)
        controls.addWidget(self.pending_label)
        controls.addWidget(self.discard_button)
        controls.addWidget(self.save_button)

        self.total_label = hint_label()
        left, left_layout = vbox(self)
        left_layout.addWidget(self.table, 1)
        left_layout.addWidget(self.total_label)
        self.chart = ChartCanvas(self, min_height=300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(controls)
        layout.addWidget(hint_label(DEMAND_HINT))
        layout.addWidget(splitter(Qt.Orientation.Horizontal, [left, self.chart], [1, 1]), 1)
        self.reload()

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.chart,)

    def set_detail(self, detail: RunDetail | None) -> None:
        """La demanda editable no depende de la corrida mostrada."""

    @property
    def has_changes(self) -> bool:
        return bool(self.model.pending_items())

    def reload(self) -> None:
        """Vuelve a leer la demanda y los tipos de atención desde la base (descarta cambios pendientes)."""
        try:
            rows = self.master_data.demand_grid()
            services = self.master_data.service_types()
        except AppError as error:
            show_error(self, error.user_message)
            return
        current = self.service_combo.currentData()
        self.service_combo.blockSignals(True)
        self.service_combo.clear()
        self.service_combo.addItem("Todos los tipos", None)
        for service in sorted(services, key=lambda s: s.code):
            self.service_combo.addItem(service.name, service.code)
        self.service_combo.setCurrentIndex(max(0, self.service_combo.findData(current)))
        self.service_combo.blockSignals(False)
        self.model.load(rows)
        self._apply_filters()

    def _apply_filters(self) -> None:
        day = self.day_combo.currentData()
        service = self.service_combo.currentData()
        self.proxy.set_filters(
            int(day) if day is not None else None, str(service) if service else None, self.only_positive.isChecked()
        )
        self._update_labels()
        self._draw()

    def _on_edits_changed(self) -> None:
        self._update_labels()
        self._draw()

    def _update_labels(self) -> None:
        pending = len(self.model.pending_items())
        self.pending_label.setText(
            format_count(pending, "cambio sin guardar", "cambios sin guardar") if pending else ""
        )
        self.save_button.setEnabled(pending > 0)
        self.discard_button.setEnabled(pending > 0)
        visible = self.proxy.rowCount()
        total = sum(
            int(self.proxy.index(row, SESSIONS_COL).data(Qt.ItemDataRole.EditRole) or 0) for row in range(visible)
        )
        self.total_label.setText(
            f"{format_count(visible, 'fila', 'filas')} en la vista, "
            f"{format_count(total, 'sesión requerida', 'sesiones requeridas')}."
        )

    def _draw(self) -> None:
        if self.model.rowCount() == 0:
            self.chart.show_message("No hay bloques de demanda definidos: revise el horario del centro.")
            return
        service = self.service_combo.currentData()
        grid = demand_grid(self.model.values(), self.model.open_blocks(), str(service) if service else None)
        if not grid.total_required:
            self.chart.show_message("No hay demanda registrada para el filtro elegido.")
            return
        title = "Demanda semanal registrada por día y bloque"
        if service:
            title += f": {self.service_combo.currentText()}"
        if self.has_changes:
            title += " (con cambios sin guardar)"
        names = {cell.row.service_code: cell.row.service_name for cell in self.model.cells()}
        self.chart.render(partial(draw_block_grid, grid=grid, title=title, mode="demand", names=names))

    def save_changes(self) -> bool:
        items = self.model.pending_items()
        if not items:
            return True
        try:
            saved = self.master_data.save_demand(items)
        except AppError as error:
            show_error(self, error.user_message, "No se guardó la demanda")
            return False
        log.info("Demanda editada desde la interfaz: %d filas", saved)
        self.reload()
        self.status_message.emit(
            f"Demanda guardada: {format_count(saved, 'fila actualizada', 'filas actualizadas')}. "
            "Se usará en la próxima optimización."
        )
        self.demand_saved.emit(saved)
        return True

    def discard_changes(self) -> None:
        self.model.revert()
        self.status_message.emit("Cambios de demanda descartados.")

    def self_check(self) -> list[str]:
        problems = check_charts([("demanda registrada", self.chart)])
        if self.model.rowCount() == 0:
            problems.append("la tabla de demanda está vacía")
        return problems
