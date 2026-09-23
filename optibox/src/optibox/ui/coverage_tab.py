"""Pestaña de cobertura: mapa de calor día por bloque, cobertura por tipo y detalle del bloque elegido."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from optibox.domain.run import RunDetail
from optibox.ui.charts import Region, draw_block_grid, draw_service_coverage
from optibox.ui.formatting import format_int
from optibox.ui.presentation import BlockGrid, coverage_grid, day_label
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import ChartCanvas, Column, RecordTableModel, hint_label, make_table_view, section_title


@dataclass(frozen=True)
class BlockDetailRow:
    """Fila del detalle de un bloque: un tipo de atención con su cobertura y la causa del faltante."""

    name: str
    required: int
    covered: int
    cause: str

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.covered)

    @property
    def pct(self) -> float | None:
        return min(self.covered, self.required) / self.required if self.required else None

    @property
    def coverage_text(self) -> str:
        return f"{format_int(self.covered)} de {format_int(self.required)}"


def default_block(grid: BlockGrid) -> tuple[int, int] | None:
    """Bloque que se destaca al abrir: el de mayor faltante (el más temprano si hay empate)."""
    cells = [cell for cell in grid.iter_cells() if cell.required]
    if not cells:
        return None
    best = max(cells, key=lambda c: (c.shortfall, -c.day, -c.block))
    return (best.day, best.block)


def keep_block(grid: BlockGrid, selected: tuple[int, int] | None) -> tuple[int, int] | None:
    """Mantiene el bloque elegido si tiene demanda en la grilla; si no, vuelve al bloque por defecto."""
    cell = grid.cell(*selected) if selected is not None else None
    if cell is not None and cell.required:
        return selected
    return default_block(grid)


def block_detail_rows(detail: RunDetail, day: int, block: int, service: str | None = None) -> list[BlockDetailRow]:
    """Tipos de atención con demanda en un bloque (o solo el tipo filtrado), con la causa de su faltante."""
    names = {s.code: s.name for s in detail.instance.service_types}
    causes = {(u.day, u.block, u.service): u.cause_label for u in detail.unmet}
    return [
        BlockDetailRow(
            names.get(row.service, row.service),
            row.required,
            row.covered,
            causes.get((day, block, row.service), ""),
        )
        for row in detail.metrics.coverage_by_block_service
        if row.service is not None
        and (service is None or row.service == service)
        and row.day == day
        and row.block == block
        and row.required
    ]


class CoverageTab(TabPage):
    title = "Cobertura por bloque"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.detail: RunDetail | None = None
        self.grid: BlockGrid | None = None
        self.selected: tuple[int, int] | None = None

        self.service_combo = QComboBox()
        self.service_combo.setMinimumWidth(220)
        self.service_combo.currentIndexChanged.connect(self._on_service_changed)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Tipo de atención"))
        filters.addWidget(self.service_combo)
        filters.addSpacing(12)
        filters.addWidget(hint_label("Pase el mouse sobre un bloque para ver el detalle; haga clic para fijarlo."), 1)

        self.heatmap = ChartCanvas(self, min_height=320)
        self.heatmap.on_click = self._on_cell_clicked
        self.services_chart = ChartCanvas(self, min_height=200)
        self.block_title = section_title("Detalle del bloque")
        self.block_model = RecordTableModel(
            [
                Column("name", "Tipo de atención"),
                Column("coverage_text", "Cubiertas", numeric=True),
                Column("cause", "Causa del faltante"),
            ]
        )
        self.block_table = make_table_view(self.block_model, self)
        right, right_layout = vbox(self)
        right_layout.addWidget(self.services_chart, 3)
        right_layout.addWidget(self.block_title)
        right_layout.addWidget(self.block_table, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(filters)
        layout.addWidget(splitter(Qt.Orientation.Horizontal, [self.heatmap, right], [4, 3]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.heatmap, self.services_chart)

    def set_detail(self, detail: RunDetail | None) -> None:
        self.detail = detail
        current = self.service_combo.currentData()
        self.service_combo.blockSignals(True)
        self.service_combo.clear()
        self.service_combo.addItem("Todos los tipos", None)
        if detail is not None:
            for service in detail.instance.service_types:
                self.service_combo.addItem(service.name, service.code)
        index = self.service_combo.findData(current)
        self.service_combo.setCurrentIndex(max(0, index))
        self.service_combo.blockSignals(False)
        self.selected = None
        self._refresh()

    def _service(self) -> str | None:
        data = self.service_combo.currentData()
        return str(data) if data else None

    def _on_service_changed(self) -> None:
        self._refresh(keep_selection=True)

    def _refresh(self, keep_selection: bool = False) -> None:
        detail = self.detail
        if detail is None:
            self.grid = None
            self.heatmap.show_message("Aún no hay corridas. Elija una semana y presione Optimizar.")
            self.services_chart.show_message("Sin datos de cobertura.")
            self.block_model.set_rows([])
            return
        self.grid = coverage_grid(detail, self._service())
        self.selected = keep_block(self.grid, self.selected if keep_selection else None)
        self._draw_heatmap()
        self.services_chart.render(
            partial(draw_service_coverage, services=detail.metrics.services, highlight=self._service())
        )
        self._fill_block_table()

    def _draw_heatmap(self) -> None:
        detail, grid = self.detail, self.grid
        if detail is None or grid is None:
            return
        if not grid.total_required:
            name = self.service_combo.currentText()
            self.heatmap.show_message(f"No hay demanda registrada de {name.lower()} en la semana.")
            return
        names = {service.code: service.name for service in detail.instance.service_types}
        service = self._service()
        title = "Cobertura de la demanda por día y bloque"
        if service is not None:
            title += f": {names.get(service, service)}"
        self.heatmap.render(
            partial(draw_block_grid, grid=grid, title=title, mode="coverage", selected=self.selected, names=names)
        )

    def _on_cell_clicked(self, region: Region) -> None:
        self.selected = region.key
        self._draw_heatmap()
        self._fill_block_table()

    def _fill_block_table(self) -> None:
        detail = self.detail
        if detail is None or self.selected is None:
            self.block_title.setText("Detalle del bloque")
            self.block_model.set_rows([])
            return
        day, block = self.selected
        rows = block_detail_rows(detail, day, block, self._service())
        planning_day = detail.instance.days[day]
        cell = self.grid.cell(day, block) if self.grid else None
        time_label = cell.time_label if cell else ""
        self.block_title.setText(f"Detalle del bloque {day_label(day, planning_day.date)}, {time_label}")
        self.block_model.set_rows(rows)

    def self_check(self) -> list[str]:
        problems = check_charts([("cobertura por bloque", self.heatmap), ("cobertura por tipo", self.services_chart)])
        if not self.heatmap.regions:
            problems.append("el mapa de cobertura no tiene bloques")
        if self.block_model.rowCount() == 0:
            problems.append("el detalle del bloque seleccionado está vacío")
        return problems
