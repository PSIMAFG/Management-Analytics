"""Tabla resumen período por programa con totales por fila y por columna.

Los montos de cada celda, los subtotales por programa y el total general
vienen calculados en SQL por el servicio de informes; aquí solo se ordenan en
una matriz (una fila por período, una columna por programa) y el total de
cada período se obtiene sumando sus celdas.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QColor, QFont

from receipt_reader.domain.dates import Period
from receipt_reader.domain.reporting import NO_PERIOD_LABEL, SummaryRow, SummaryTable, Totals
from receipt_reader.ui.formatting import format_int, month_label, plural
from receipt_reader.ui.widgets import SORT_ROLE, ModelIndex

MEASURES: tuple[tuple[str, str], ...] = (
    ("gross", "Bruto"),
    ("retention", "Retención"),
    ("net", "Líquido"),
    ("count", "N° de boletas"),
)
TOTAL_LABEL = "Total"
TOTAL_BACKGROUND = "#EEF1F5"


def period_header(period: Period | None) -> str:
    return month_label(period.year, period.month) if period is not None else NO_PERIOD_LABEL


def _measure(item: SummaryRow | Totals, measure: str) -> int:
    return int(getattr(item, measure))


@dataclass(frozen=True)
class SummaryMatrix:
    """Matriz lista para mostrar: filas de períodos más la fila Total, columnas de programas más Total."""

    measure: str
    row_labels: tuple[str, ...]
    column_labels: tuple[str, ...]
    cells: tuple[tuple[int | None, ...], ...]
    counts: tuple[tuple[int | None, ...], ...]

    @property
    def grand_total(self) -> int:
        return self.cells[-1][-1] or 0

    @property
    def is_empty(self) -> bool:
        return len(self.column_labels) <= 1

    def column_total(self, column: int) -> int:
        return self.cells[-1][column] or 0


def ordered_programs(summary: SummaryTable, program_order: Sequence[int] = ()) -> list[SummaryRow]:
    """Subtotales por programa en el orden del catálogo (los que no están en él van al final por nombre)."""
    rank = {program_id: index for index, program_id in enumerate(program_order)}
    return sorted(
        summary.program_totals,
        key=lambda row: (
            rank.get(row.program_id, len(rank)) if row.program_id is not None else len(rank) + 1,
            row.program_name,
        ),
    )


def build_summary_matrix(
    summary: SummaryTable,
    measure: str = "gross",
    short_names: Mapping[str, str] | None = None,
    program_order: Sequence[int] = (),
) -> SummaryMatrix:
    """Ordena el resumen en una matriz período por programa para la medida elegida."""
    if measure not in {key for key, _label in MEASURES}:
        raise ValueError(f"Medida desconocida: {measure}")
    names = short_names or {}
    periods = summary.periods()
    programs = ordered_programs(summary, program_order)
    cells_by_key = {(row.program_id, row.period): row for row in summary.rows}
    cells: list[tuple[int | None, ...]] = []
    counts: list[tuple[int | None, ...]] = []
    for period in periods:
        row_values: list[int | None] = []
        row_counts: list[int | None] = []
        for program in programs:
            cell = cells_by_key.get((program.program_id, period))
            row_values.append(None if cell is None else _measure(cell, measure))
            row_counts.append(None if cell is None else cell.count)
        period_total = sum(value for value in row_values if value is not None)
        period_count = sum(count for count in row_counts if count is not None)
        cells.append((*row_values, period_total))
        counts.append((*row_counts, period_count))
    cells.append((*(_measure(program, measure) for program in programs), _measure(summary.grand_total, measure)))
    counts.append((*(program.count for program in programs), summary.grand_total.count))
    return SummaryMatrix(
        measure=measure,
        row_labels=(*(period_header(period) for period in periods), TOTAL_LABEL),
        column_labels=(*(names.get(p.program_name, p.program_name) for p in programs), TOTAL_LABEL),
        cells=tuple(cells),
        counts=tuple(counts),
    )


class SummaryMatrixModel(QAbstractTableModel):
    """Modelo de solo lectura de la matriz; la fila y la columna de totales van en negrita."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._matrix: SummaryMatrix | None = None
        self._bold = QFont()
        self._bold.setBold(True)
        self._total_brush = QColor(TOTAL_BACKGROUND)

    def set_matrix(self, matrix: SummaryMatrix | None) -> None:
        self.beginResetModel()
        self._matrix = matrix
        self.endResetModel()

    def matrix(self) -> SummaryMatrix | None:
        return self._matrix

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid() or self._matrix is None or self._matrix.is_empty:
            return 0
        return len(self._matrix.row_labels)

    def columnCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid() or self._matrix is None:
            return 0
        return len(self._matrix.column_labels) + 1

    def _is_total(self, row: int, column: int) -> bool:
        """Celda de la fila Total o de la columna Total (la columna 0 es el período)."""
        if self._matrix is None:
            return False
        return row == len(self._matrix.row_labels) - 1 or column == len(self._matrix.column_labels)

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or self._matrix is None:
            return None
        row, column = index.row(), index.column()
        value: int | None = None if column == 0 else self._matrix.cells[row][column - 1]
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return self._matrix.row_labels[row]
            return "-" if value is None else format_int(value)
        if role in (Qt.ItemDataRole.UserRole, SORT_ROLE):
            return self._matrix.row_labels[row] if column == 0 else value
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignLeft if column == 0 else Qt.AlignmentFlag.AlignRight
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole:
            return self._bold if self._is_total(row, column) else None
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._total_brush if self._is_total(row, column) else None
        if role == Qt.ItemDataRole.ToolTipRole and column > 0:
            count = self._matrix.counts[row][column - 1]
            if not count:
                return "Sin boletas válidas."
            return (
                f"{self._matrix.column_labels[column - 1]}, {self._matrix.row_labels[row]}: {plural(count, 'boleta')}"
            )
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal or self._matrix is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return "Período" if section == 0 else self._matrix.column_labels[section - 1]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignLeft if section == 0 else Qt.AlignmentFlag.AlignRight
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None
