"""Gráficos de matplotlib de la interfaz.

Cada función dibuja sobre una figura vacía y devuelve las regiones sensibles
(rectángulos en coordenadas de datos con su texto de ayuda) que el lienzo usa
para mostrar el detalle al pasar el mouse o al hacer clic. Los números se
muestran con formato chileno y los textos nunca usan el color de la serie.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import matplotlib as mpl
from matplotlib.axes import Axes
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.textpath import TextPath
from matplotlib.ticker import FuncFormatter, MaxNLocator, MultipleLocator

from optibox.domain.agenda import AgendaEntry, AgendaKind
from optibox.domain.diagnosis import UnmetCause
from optibox.domain.instance import PlanningDay
from optibox.domain.metrics import DayUtilization, RoomUse, ServiceSummary, StaffHours, StaffLoad
from optibox.domain.run import RunSummary
from optibox.domain.timegrid import BLOCK_MINUTES, format_minute
from optibox.ui.formatting import format_count, format_hours, format_int, format_pct
from optibox.ui.presentation import (
    HOURS_PARTS,
    LOAD_PARTS,
    AgendaBox,
    BlockGrid,
    GridCell,
    OccupancyBand,
    UnmetBar,
    cause_label,
    causes_present,
    day_label,
    hours_parts,
    layout_agenda,
    load_parts,
    run_chart_label,
    shorten,
    staff_label,
    text_color_on,
)
from optibox.ui.style import (
    BASELINE,
    BLUE_RAMP,
    BORDER,
    CLOSED,
    GRID,
    ORANGE_RAMP,
    PRIMARY,
    SERIES,
    SURFACE,
    TEXT,
    TEXT_MUTED,
    TRACK,
)

# Color fijo por causa. Las cuatro más frecuentes forman un conjunto que se distingue en cualquier par, aun
# con daltonismo. Las menos frecuentes se apoyan además en la leyenda, la ayuda emergente y la tabla, y la
# búsqueda que no alcanzó el óptimo (que no es una falta de capacidad) va en gris neutro.
CAUSE_COLORS: dict[UnmetCause, str] = {
    UnmetCause.NO_CANDIDATES: SERIES[0],
    UnmetCause.CAP: SERIES[1],
    UnmetCause.CONTRACT: SERIES[2],
    UnmetCause.STAFF_BUSY: SERIES[6],
    UnmetCause.ROOM_BUSY: SERIES[3],
    UnmetCause.ADMIN: SERIES[4],
    UnmetCause.COVERAGE_FLOOR: SERIES[5],
    UnmetCause.SEARCH_LIMIT: BASELINE,
}
LOAD_COLORS = {
    "Atención": SERIES[0],
    "Administrativo": SERIES[1],
    "Reuniones y bloqueos": SERIES[2],
    "Tareas no asistenciales": SERIES[6],
    "Libre": TRACK,
}
HOURS_COLORS = {
    "Atención": SERIES[0],
    "Administrativo": SERIES[1],
    "Reuniones y bloqueos": SERIES[2],
    "Permisos": SERIES[5],
    "Ociosas": TRACK,
}
KIND_COLORS = {
    AgendaKind.ADMIN: "#D5DEE9",
    AgendaKind.BLOCKING: "#E4E1D8",
    AgendaKind.ABSENCE: "#F2DFD3",
    AgendaKind.HOLIDAY: "#E3E6EA",
}
KIND_LABELS = {
    AgendaKind.SESSION: "Sesión",
    AgendaKind.ADMIN: "Administrativo",
    AgendaKind.BLOCKING: "Bloqueo o reunión",
    AgendaKind.ABSENCE: "Ausencia",
    AgendaKind.HOLIDAY: "Feriado",
}
NO_DEMAND_FILL = "#F5F6F8"
# Paso claro de la misma rampa azul: atenúa las barras que no están destacadas sin cambiar su identidad.
MUTED_BAR = BLUE_RAMP[2]
# Ocupación de la sala administrativa: tres pasos de la rampa azul hasta la capacidad recomendada y un tono
# de advertencia sobre ella (es un estado, no una serie).
OCCUPANCY_FILLS = (BLUE_RAMP[1], BLUE_RAMP[2], BLUE_RAMP[3], "#F5C77E")
MIN_COLUMN_SLOTS = 8
MIN_ROW_SLOTS = 5
GAP = 1.5
LABEL_SIZE = 8
SMALL_SIZE = 7.5
# Leyenda bajo el título: separación del título para una fila de leyenda y alto de cada fila adicional (puntos).
LEGEND_TITLE_PAD = 24
LEGEND_ROW_PT = 13
LEGEND_HANDLE_LENGTH = 1.2
LEGEND_COLUMN_SPACING = 1.2
# Separación mínima (puntos) entre los rótulos de dos barras vecinas.
TICK_GAP_PT = 3


@dataclass(frozen=True)
class Region:
    """Zona sensible de un gráfico: rectángulo en coordenadas de datos, texto de ayuda y clave opcional."""

    axes: Axes
    x0: float
    x1: float
    y0: float
    y1: float
    text: str
    key: Any = None

    def contains(self, axes: Axes | None, x: float | None, y: float | None) -> bool:
        if axes is not self.axes or x is None or y is None:
            return False
        return min(self.x0, self.x1) <= x <= max(self.x0, self.x1) and min(self.y0, self.y1) <= y <= max(
            self.y0, self.y1
        )


def pct_formatter(decimals: int = 0) -> FuncFormatter:
    return FuncFormatter(lambda value, _pos: format_pct(value, decimals))


def int_formatter() -> FuncFormatter:
    return FuncFormatter(lambda value, _pos: format_int(value))


def _run_layout(figure: Figure) -> None:
    """Calcula el diseño de la figura para conocer el tamaño real de los ejes antes de agregar rótulos."""
    layout = figure.get_layout_engine()
    if layout is not None:
        layout.execute(figure)


def _text_width_pt(text: str, prop: FontProperties) -> float:
    return float(TextPath((0, 0), text, prop=prop).get_extents().width)


def legend_columns(labels: Sequence[str], available_pt: float, requested: int) -> int:
    """Mayor cantidad de columnas (hasta `requested`) con que la leyenda cabe en `available_pt` puntos de ancho.

    Replica el armado de matplotlib: las entradas llenan cada columna de arriba
    abajo y cada columna mide lo que su rótulo más largo más el cuadro de color.
    """
    prop = FontProperties(size=mpl.rcParams["legend.fontsize"])
    em = prop.get_size_in_points()
    widths = [_text_width_pt(label, prop) for label in labels]
    item = (LEGEND_HANDLE_LENGTH + 0.8) * em
    for columns in range(min(requested, len(labels)), 1, -1):
        rows = math.ceil(len(labels) / columns)
        groups = [widths[start : start + rows] for start in range(0, len(widths), rows)]
        total = sum(max(group) + item for group in groups) + LEGEND_COLUMN_SPACING * em * (len(groups) - 1) + em
        if total <= available_pt:
            return columns
    return 1


def _title_with_legend(ax: Axes, title: str, handles: Sequence[Any] = (), ncols: int = 4) -> None:
    """Título a la izquierda y, si hay más de una serie, la leyenda bajo el título.

    La leyenda usa tantas columnas como quepan en el ancho real de los ejes
    (hasta `ncols`) y ocupa más filas si hace falta, en vez de ensanchar la
    figura y achicar el gráfico.
    """
    if not handles:
        ax.set_title(title)
        return
    figure = ax.get_figure()
    assert isinstance(figure, Figure)
    _run_layout(figure)
    available = ax.get_window_extent().width * 72 / figure.dpi
    columns = legend_columns([str(handle.get_label()) for handle in handles], available, ncols)
    rows = math.ceil(len(handles) / columns)
    ax.set_title(title, pad=LEGEND_TITLE_PAD + LEGEND_ROW_PT * (rows - 1))
    ax.legend(
        handles=list(handles),
        loc="lower left",
        bbox_to_anchor=(0.0, 1.0),
        ncols=columns,
        borderaxespad=0.2,
        handlelength=LEGEND_HANDLE_LENGTH,
        handleheight=0.9,
        columnspacing=LEGEND_COLUMN_SPACING,
    )


def _horizontal_grid(ax: Axes) -> None:
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.yaxis.grid(False)
    ax.tick_params(axis="y", length=0)


def _vertical_grid(ax: Axes) -> None:
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.grid(False)
    ax.tick_params(axis="x", length=0)


def draw_block_grid(
    figure: Figure,
    grid: BlockGrid,
    *,
    title: str,
    mode: str = "coverage",
    selected: tuple[int, int] | None = None,
    names: Mapping[str, str] | None = None,
) -> list[Region]:
    """Mapa de calor día por bloque.

    En modo "coverage" el color muestra la cobertura (más oscuro = más
    demanda sin cubrir) y cada celda indica cubiertas/requeridas. En modo
    "demand" el color muestra las sesiones requeridas.
    """
    names = names or {}
    ax = figure.add_subplot()
    columns, rows = len(grid.days), len(grid.blocks)
    if mode == "coverage":
        cmap = LinearSegmentedColormap.from_list("cobertura", list(reversed(ORANGE_RAMP)))
        norm = Normalize(0.0, 1.0)
    else:
        cmap = LinearSegmentedColormap.from_list("demanda", list(BLUE_RAMP[:-1]))
        norm = Normalize(0.0, max(1, grid.max_required))
    regions: list[Region] = []
    small = SMALL_SIZE if rows <= 10 else 6.5
    for row_index, row in enumerate(grid.cells):
        for col_index, cell in enumerate(row):
            if not cell.is_open:
                continue
            value = cell.pct if mode == "coverage" else float(cell.required)
            fill = NO_DEMAND_FILL if value is None or (mode == "demand" and cell.required == 0) else cmap(norm(value))
            rect = Rectangle((col_index, row_index), 1, 1, facecolor=fill, edgecolor=SURFACE, linewidth=2.5)
            ax.add_patch(rect)
            center_x, center_y = col_index + 0.5, row_index + 0.5
            fill_hex = fill if isinstance(fill, str) else _to_hex(fill)
            ink = text_color_on(fill_hex)
            if mode == "coverage":
                if cell.required:
                    ax.text(
                        center_x,
                        center_y - 0.1,
                        f"{cell.covered}/{cell.required}",
                        ha="center",
                        va="center",
                        fontsize=LABEL_SIZE + 0.5,
                        fontweight="bold",
                        color=ink,
                    )
                    ax.text(
                        center_x,
                        center_y + 0.2,
                        format_pct(cell.pct, 0),
                        ha="center",
                        va="center",
                        fontsize=small,
                        color=ink,
                    )
                else:
                    ax.text(center_x, center_y, "-", ha="center", va="center", fontsize=small, color=TEXT_MUTED)
            elif cell.required:
                ax.text(
                    center_x,
                    center_y,
                    format_int(cell.required),
                    ha="center",
                    va="center",
                    fontsize=LABEL_SIZE + 0.5,
                    fontweight="bold",
                    color=ink,
                )
            regions.append(
                Region(
                    ax,
                    col_index,
                    col_index + 1,
                    row_index,
                    row_index + 1,
                    _grid_tooltip(grid, cell, mode, names),
                    key=(cell.day, cell.block),
                )
            )
    for col_index in range(columns):
        if rows and not any(row[col_index].is_open for row in grid.cells):
            ax.add_patch(Rectangle((col_index, 0), 1, rows, facecolor=CLOSED, edgecolor=SURFACE, linewidth=2.5))
            ax.text(
                col_index + 0.5,
                rows / 2,
                "Día sin atención",
                ha="center",
                va="center",
                rotation=90,
                fontsize=LABEL_SIZE,
                color=TEXT_MUTED,
            )
    if selected is not None and selected[0] in grid.days and selected[1] in grid.blocks:
        col_index, row_index = grid.days.index(selected[0]), grid.blocks.index(selected[1])
        ax.add_patch(
            Rectangle((col_index + 0.04, row_index + 0.04), 0.92, 0.92, fill=False, edgecolor=PRIMARY, linewidth=2)
        )
    ax.set_xlim(0, columns)
    ax.set_ylim(rows, 0)
    ax.xaxis.tick_top()
    ax.set_xticks([index + 0.5 for index in range(columns)], list(grid.day_labels))
    ax.set_yticks([index + 0.5 for index in range(rows)], [format_minute(block) for block in grid.blocks])
    ax.tick_params(length=0)
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_ylabel("Inicio del bloque")
    ax.set_title(title, pad=10)
    colorbar = figure.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax, fraction=0.04, pad=0.02, aspect=28)
    colorbar.outline.set_visible(False)
    colorbar.ax.tick_params(length=0, labelsize=SMALL_SIZE + 0.5)
    if mode == "coverage":
        colorbar.ax.yaxis.set_major_formatter(pct_formatter())
        colorbar.set_label("Cobertura del bloque (más oscuro = más faltante)", color=TEXT_MUTED)
    else:
        colorbar.ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
        colorbar.ax.yaxis.set_major_formatter(int_formatter())
        colorbar.set_label("Sesiones requeridas", color=TEXT_MUTED)
    return regions


def _to_hex(rgba: tuple[float, ...]) -> str:
    red, green, blue = (round(channel * 255) for channel in rgba[:3])
    return f"#{red:02X}{green:02X}{blue:02X}"


def _grid_tooltip(grid: BlockGrid, cell: GridCell, mode: str, names: Mapping[str, str]) -> str:
    label = grid.day_labels[grid.days.index(cell.day)]
    header = f"{label}, bloque {cell.time_label}"
    if not cell.required:
        return f"{header}\nSin demanda registrada"
    if mode == "coverage":
        lines = [header, f"Cubiertas {cell.covered} de {cell.required} ({format_pct(cell.pct, 0)})"]
        lines += [f"{names.get(code, code)}: {covered} de {required}" for code, required, covered in cell.details]
    else:
        lines = [header, f"Sesiones requeridas: {format_int(cell.required)}"]
        lines += [f"{names.get(code, code)}: {required}" for code, required, _ in cell.details]
    return "\n".join(lines)


def draw_service_coverage(
    figure: Figure, services: Sequence[ServiceSummary], highlight: str | None = None
) -> list[Region]:
    """Barras de cobertura por tipo de atención: pista gris con lo requerido y barra con lo cubierto.

    Si se indica `highlight`, ese tipo conserva el color y los demás se atenúan.
    """
    rows = sorted((s for s in services if s.required or s.sessions), key=lambda s: (-s.required, s.code))
    ax = figure.add_subplot()
    positions = list(range(len(rows)))
    ax.barh(positions, [s.required for s in rows], height=0.62, color=TRACK)
    colors = [SERIES[0] if highlight in (None, s.code) else MUTED_BAR for s in rows]
    ax.barh(positions, [min(s.covered, s.required) for s in rows], height=0.62, color=colors)
    top = max((s.required for s in rows), default=1)
    regions: list[Region] = []
    for position, service in zip(positions, rows, strict=True):
        ax.text(
            service.required + top * 0.02,
            position,
            f"{service.covered}/{service.required}  {format_pct(service.pct, 0)}",
            va="center",
            fontsize=LABEL_SIZE,
            color=TEXT if highlight in (None, service.code) else TEXT_MUTED,
            fontweight="bold" if highlight == service.code else "normal",
        )
        tooltip = (
            f"{service.name}\nCubiertas {service.covered} de {service.required} ({format_pct(service.pct)})\n"
            f"Sesiones {service.sessions}, atenciones {service.attentions}\nTiempo de atención "
            f"{format_hours(service.minutes)}"
        )
        regions.append(Region(ax, 0, top * 1.4, position - 0.45, position + 0.45, tooltip, key=service.code))
    ax.set_yticks(positions, [shorten(s.name, 28) for s in rows])
    for label, service in zip(ax.get_yticklabels(), rows, strict=True):
        if service.code == highlight:
            label.set_fontweight("bold")
    ax.set_xlim(0, top * 1.42)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))
    ax.xaxis.set_major_formatter(int_formatter())
    ax.set_xlabel("Sesiones en la semana")
    _horizontal_grid(ax)
    handles = [Patch(color=TRACK, label="Requeridas"), Patch(color=SERIES[0], label="Cubiertas")]
    _title_with_legend(ax, "Cobertura por tipo de atención", handles)
    return regions


def draw_staff_load(figure: Figure, loads: Sequence[StaffLoad]) -> list[Region]:
    """Barras horizontales apiladas por persona con la marca de su contrato."""
    ax = figure.add_subplot()
    positions = list(range(len(loads)))
    labels_used: set[str] = set()
    for position, load in zip(positions, loads, strict=True):
        left = 0.0
        for label, minutes in load_parts(load):
            if minutes <= 0:
                continue
            hours = minutes / 60
            ax.barh(position, hours, left=left, height=0.64, color=LOAD_COLORS[label], edgecolor=SURFACE, linewidth=GAP)
            left += hours
            labels_used.add(label)
        contract_h = load.contract_min / 60
        ax.plot(
            [contract_h, contract_h],
            [position - 0.42, position + 0.42],
            color=TEXT,
            linewidth=1.6,
            solid_capstyle="butt",
        )
        ax.text(
            max(left, contract_h) + 0.6,
            position,
            format_pct(load.usage_pct, 0),
            va="center",
            fontsize=LABEL_SIZE,
            color=TEXT,
        )
    top = max((max(load.contract_min, load.assigned_min) / 60 for load in loads), default=1)
    regions = [
        Region(ax, 0, top * 1.12, position - 0.45, position + 0.45, _load_tooltip(load), key=load.code)
        for position, load in zip(positions, loads, strict=True)
    ]
    ax.set_yticks(positions, [staff_label(load.code, load.name) for load in loads])
    ax.set_ylim(len(loads) - 0.5, -0.5)
    ax.set_xlim(0, top * 1.12)
    ax.xaxis.set_major_locator(MultipleLocator(5 if top <= 50 else 10))
    ax.xaxis.set_major_formatter(int_formatter())
    ax.set_xlabel("Horas en la semana")
    _horizontal_grid(ax)
    handles: list[Any] = [
        Patch(color=LOAD_COLORS[label], label=label) for _, label in LOAD_PARTS if label in labels_used
    ]
    handles.append(Line2D([], [], color=TEXT, linewidth=1.6, label="Contrato"))
    _title_with_legend(ax, "Carga semanal por persona frente a su contrato", handles, ncols=6)
    return regions


def _load_tooltip(load: StaffLoad) -> str:
    lines = [f"{load.code} {load.name} ({load.role})", f"Contrato: {format_hours(load.contract_min)}"]
    if load.delivers_services:
        lines.append(
            f"Atención: {format_hours(load.session_min)} ({format_count(load.sessions, 'sesión', 'sesiones')}, "
            f"{format_count(load.attentions, 'atención', 'atenciones')})"
        )
        lines.append(
            f"Administrativo: {format_hours(load.admin_min)} (asociado a sesiones "
            f"{format_hours(load.admin_required_min)})"
        )
    lines.append(f"Reuniones y bloqueos: {format_hours(load.meeting_min)}")
    if load.non_service_min:
        lines.append(f"Tareas no asistenciales: {format_hours(load.non_service_min)}")
    lines.append(f"Libre: {format_hours(load.free_min)}")
    lines.append(f"Uso del contrato: {format_pct(load.usage_pct)}")
    return "\n".join(lines)


def draw_staff_hours(figure: Figure, rows: Sequence[StaffHours]) -> list[Region]:
    """Barras horizontales apiladas del rendimiento de horas por persona (frente al tiempo contratado disponible)."""
    ax = figure.add_subplot()
    positions = list(range(len(rows)))
    labels_used: set[str] = set()
    for position, row in zip(positions, rows, strict=True):
        left = 0.0
        for label, minutes in hours_parts(row):
            if minutes <= 0:
                continue
            hours_value = minutes / 60
            ax.barh(
                position,
                hours_value,
                left=left,
                height=0.64,
                color=HOURS_COLORS[label],
                edgecolor=SURFACE,
                linewidth=GAP,
            )
            left += hours_value
            labels_used.add(label)
        ax.text(left + 0.6, position, format_pct(row.idle_pct, 0), va="center", fontsize=LABEL_SIZE, color=TEXT)
    top = max((row.contracted_min / 60 for row in rows), default=1)
    regions = [
        Region(ax, 0, top * 1.16, position - 0.45, position + 0.45, _hours_tooltip(row), key=row.code)
        for position, row in zip(positions, rows, strict=True)
    ]
    ax.set_yticks(positions, [staff_label(row.code, row.name) for row in rows])
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xlim(0, top * 1.16)
    ax.xaxis.set_major_locator(MultipleLocator(5 if top <= 50 else 10))
    ax.xaxis.set_major_formatter(int_formatter())
    ax.set_xlabel("Horas en la semana")
    _horizontal_grid(ax)
    handles: list[Any] = [
        Patch(color=HOURS_COLORS[label], label=label) for _, label in HOURS_PARTS if label in labels_used
    ]
    _title_with_legend(ax, "Rendimiento de horas por persona (% ocioso a la derecha)", handles, ncols=5)
    return regions


def _hours_tooltip(row: StaffHours) -> str:
    lines = [
        f"{row.code} {row.name} ({row.role})",
        f"Jornada programada: {format_hours(row.scheduled_min)}, contrato: {format_hours(row.contract_min)}",
        f"Tiempo contratado disponible: {format_hours(row.contracted_min)}",
    ]
    if row.leave_min:
        lines.append(f"Permisos: {format_hours(row.leave_min)}")
    if row.delivers_services:
        lines.append(f"Atención: {format_hours(row.session_min)}")
    lines.append(f"Administrativo: {format_hours(row.admin_min)}")
    lines.append(f"Reuniones y bloqueos: {format_hours(row.meeting_min)}")
    lines.append(f"Ociosas: {format_hours(row.idle_min)} ({format_pct(row.idle_pct)})")
    lines.append(f"Productivo: {format_pct(row.productive_pct)}, clínico: {format_pct(row.clinical_pct)}")
    return "\n".join(lines)


def draw_room_hours(figure: Figure, rooms: Sequence[RoomUse]) -> list[Region]:
    """Horas ocupadas frente a ociosas por sala, de mayor a menor ociosidad."""
    ordered = sorted(rooms, key=lambda r: (-r.idle_min, r.code))
    ax = figure.add_subplot()
    positions = list(range(len(ordered)))
    for position, room in zip(positions, ordered, strict=True):
        occupied_h = room.session_min / 60
        idle_h = room.idle_min / 60
        ax.barh(position, occupied_h, height=0.5, color=SERIES[0])
        ax.barh(position, idle_h, left=occupied_h, height=0.5, color=TRACK)
        total_h = occupied_h + idle_h
        ax.text(total_h + 0.6, position, format_pct(room.pct, 0), va="center", fontsize=LABEL_SIZE, color=TEXT)
    top = max((room.capacity_min / 60 for room in ordered), default=1)
    regions = [
        Region(ax, 0, top * 1.18, position - 0.45, position + 0.45, _room_hours_tooltip(room), key=room.code)
        for position, room in zip(positions, ordered, strict=True)
    ]
    ax.set_yticks(positions, [shorten(room.name, 22) for room in ordered])
    ax.set_ylim(len(ordered) - 0.5, -0.5)
    ax.set_xlim(0, top * 1.18)
    ax.xaxis.set_major_formatter(int_formatter())
    ax.set_xlabel("Horas en la semana")
    _horizontal_grid(ax)
    handles = [Patch(color=SERIES[0], label="Ocupadas"), Patch(color=TRACK, label="Ociosas")]
    _title_with_legend(ax, "Horas ociosas por sala", handles, ncols=2)
    return regions


def _room_hours_tooltip(room: RoomUse) -> str:
    return (
        f"{room.name} ({room.code})\n"
        f"Abiertas: {format_hours(room.capacity_min)}, ocupadas: {format_hours(room.session_min)}\n"
        f"Ociosas: {format_hours(room.idle_min)}\nOcupación: {format_pct(room.pct)}"
    )


def draw_daily_utilization(
    figure: Figure, daily: Sequence[DayUtilization], days: Sequence[PlanningDay]
) -> list[Region]:
    """Barras de utilización diaria de las salas y línea de utilización acumulada (una sola escala)."""
    ax = figure.add_subplot()
    labels = [day_label(day.index, day.date) for day in days]
    positions = list(range(len(daily)))
    regions: list[Region] = []
    for position, row in zip(positions, daily, strict=True):
        if row.pct is None:
            ax.text(position, 0.02, "Feriado", ha="center", va="bottom", fontsize=LABEL_SIZE, color=TEXT_MUTED)
            continue
        ax.bar(position, row.pct, width=0.44, color=SERIES[0])
        # El valor va dentro de la barra, junto a la base, para no chocar con la línea acumulada.
        if row.pct >= 0.08:
            ax.text(
                position,
                0.02,
                format_pct(row.pct, 0),
                ha="center",
                va="bottom",
                fontsize=LABEL_SIZE,
                color=text_color_on(SERIES[0]),
                fontweight="bold",
            )
        else:
            ax.text(
                position,
                row.pct + 0.015,
                format_pct(row.pct, 0),
                ha="center",
                va="bottom",
                fontsize=LABEL_SIZE,
                color=TEXT,
            )
    cumulative = [(p, row.cumulative_pct) for p, row in zip(positions, daily, strict=True) if row.cumulative_pct]
    week_pct = daily[-1].cumulative_pct if daily else None
    if cumulative:
        xs, ys = zip(*cumulative, strict=True)
        ax.plot(
            xs,
            ys,
            color=SERIES[1],
            linewidth=2,
            marker="o",
            markersize=7,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
            solid_capstyle="round",
        )
    for position, row in zip(positions, daily, strict=True):
        tooltip = (
            f"{labels[position]}\nUtilización del día: {format_pct(row.pct)} "
            f"({format_hours(row.session_min)} de {format_hours(row.capacity_min)})\n"
            f"Acumulada hasta este día: {format_pct(row.cumulative_pct)}"
        )
        regions.append(Region(ax, position - 0.4, position + 0.4, 0, 1.05, tooltip, key=row.day))
    ax.set_xticks(positions, labels)
    ax.set_xlim(-0.6, len(daily) - 0.4)
    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_locator(MultipleLocator(0.2))
    ax.yaxis.set_major_formatter(pct_formatter())
    ax.set_ylabel("Minutos de sesión / minutos disponibles")
    _vertical_grid(ax)
    handles = [
        Patch(color=SERIES[0], label="Utilización del día"),
        Line2D(
            [],
            [],
            color=SERIES[1],
            linewidth=2,
            marker="o",
            markersize=6,
            markeredgecolor=SURFACE,
            label=f"Acumulada de la semana ({format_pct(week_pct, 1)})",
        ),
    ]
    _title_with_legend(ax, "Utilización diaria de las salas de atención", handles)
    return regions


def draw_room_utilization(figure: Figure, rooms: Sequence[RoomUse]) -> list[Region]:
    """Utilización semanal por sala, de mayor a menor."""
    ordered = sorted(rooms, key=lambda r: (-(r.pct or 0.0), r.code))
    ax = figure.add_subplot()
    positions = list(range(len(ordered)))
    ax.barh(positions, [1.0] * len(ordered), height=0.5, color=TRACK)
    ax.barh(positions, [room.pct or 0.0 for room in ordered], height=0.5, color=SERIES[0])
    regions: list[Region] = []
    for position, room in zip(positions, ordered, strict=True):
        ax.text(1.02, position, format_pct(room.pct, 0), va="center", fontsize=LABEL_SIZE, color=TEXT)
        tooltip = (
            f"{room.name} ({room.code})\n{format_count(room.sessions, 'sesión', 'sesiones')}\n"
            f"{format_hours(room.session_min)} de sesión de "
            f"{format_hours(room.capacity_min)} disponibles\nUtilización: {format_pct(room.pct)}"
        )
        regions.append(Region(ax, 0, 1.15, position - 0.45, position + 0.45, tooltip, key=room.code))
    ax.set_yticks(positions, [shorten(room.name, 22) for room in ordered])
    ax.set_ylim(len(ordered) - 0.5, -0.5)
    ax.set_xlim(0, 1.16)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.xaxis.set_major_formatter(pct_formatter())
    _horizontal_grid(ax)
    _title_with_legend(ax, "Utilización semanal por sala")
    return regions


def draw_unmet(
    figure: Figure, bars: Sequence[UnmetBar], days: Sequence[PlanningDay], names: Mapping[str, str]
) -> list[Region]:
    """Faltante por (día, bloque) apilado por causa; el color sigue siempre a la causa."""
    ax = figure.add_subplot()
    dates = {day.index: day.date for day in days}
    positions = list(range(len(bars)))
    width = 0.56
    for position, bar in zip(positions, bars, strict=True):
        bottom = 0
        for cause, amount in bar.by_cause:
            ax.bar(
                position,
                amount,
                bottom=bottom,
                width=width,
                color=CAUSE_COLORS[cause],
                edgecolor=SURFACE,
                linewidth=GAP,
            )
            bottom += amount
        ax.text(
            position, bar.total + 0.05, format_int(bar.total), ha="center", va="bottom", fontsize=LABEL_SIZE, color=TEXT
        )
    top = max((bar.total for bar in bars), default=1)
    regions: list[Region] = []
    for position, bar in zip(positions, bars, strict=True):
        header = (
            f"{day_label(bar.day, dates.get(bar.day))}, bloque {format_minute(bar.block)}-"
            f"{format_minute(bar.block + BLOCK_MINUTES)}"
        )
        lines = [header, f"Sin cubrir: {bar.total}"]
        lines += [
            f"{names.get(item.service, item.service)}: faltan {item.shortfall} ({item.cause_label.lower()})"
            for item in bar.items
        ]
        regions.append(Region(ax, position - 0.45, position + 0.45, 0, top * 1.2, "\n".join(lines), key=bar))
    ax.set_xticks(positions, [""] * len(bars))
    # Con pocas barras se reserva el espacio de varias para que no se vuelvan bloques anchos.
    pad = max(0, MIN_COLUMN_SLOTS - len(bars)) / 2
    ax.set_xlim(-0.6 - pad, len(bars) - 0.4 + pad)
    ax.set_ylim(0, top * 1.18 + 0.2)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
    ax.yaxis.set_major_formatter(int_formatter())
    ax.set_ylabel("Sesiones sin cubrir")
    _vertical_grid(ax)
    handles = [Patch(color=CAUSE_COLORS[cause], label=cause_label(cause)) for cause in causes_present(bars)]
    _title_with_legend(ax, "Turnos sin cubrir por bloque y causa", handles, ncols=4)
    _block_tick_labels(ax, bars)
    return regions


def _block_tick_labels(ax: Axes, bars: Sequence[UnmetBar]) -> None:
    """Día y hora bajo cada barra.

    Si los rótulos de dos líneas no caben lado a lado se rotan en una sola
    línea, y si aun rotados chocan se rotula una barra de cada n (la ayuda
    emergente de cada barra conserva el detalle completo).
    """
    figure = ax.get_figure()
    assert isinstance(figure, Figure)
    _run_layout(figure)
    left, right = ax.get_xlim()
    spacing = ax.get_window_extent().width * 72 / figure.dpi / max(right - left, 1.0)
    prop = FontProperties(size=LABEL_SIZE)
    widest = max(
        (max(_text_width_pt(day_label(bar.day), prop), _text_width_pt(format_minute(bar.block), prop)) for bar in bars),
        default=0.0,
    )
    positions = list(range(len(bars)))
    if spacing >= widest + TICK_GAP_PT:
        ax.set_xticks(positions, [f"{day_label(bar.day)}\n{format_minute(bar.block)}" for bar in bars])
        ax.tick_params(axis="x", labelsize=LABEL_SIZE)
        return
    step = max(1, math.ceil((LABEL_SIZE * 1.25 + TICK_GAP_PT) / spacing))
    labels = [
        f"{day_label(bar.day)} {format_minute(bar.block)}" if index % step == 0 else ""
        for index, bar in enumerate(bars)
    ]
    ax.set_xticks(positions, labels)
    ax.tick_params(axis="x", labelsize=LABEL_SIZE, labelrotation=90)


def occupancy_colors(bands: Sequence[OccupancyBand]) -> tuple[str, ...]:
    """Relleno de cada rango de ocupación: los pasos más oscuros de la rampa para los rangos más altos."""
    inside = sum(1 for band in bands if not band.over)
    colors: list[str] = []
    for index, band in enumerate(bands):
        colors.append(OCCUPANCY_FILLS[-1] if band.over else OCCUPANCY_FILLS[3 - inside + index])
    return tuple(colors)


def draw_agenda(
    figure: Figure,
    entries: Sequence[AgendaEntry],
    days: Sequence[PlanningDay],
    *,
    title: str,
    names: Mapping[str, str],
    show_staff: bool,
    fill_of: Callable[[AgendaEntry], str | None] | None = None,
    legend: Sequence[tuple[str, str]] | None = None,
) -> list[Region]:
    """Agenda semanal en columnas por día; las entradas solapadas se dividen en carriles.

    Primero se dibujan los tramos, los ejes y la leyenda; después se calcula el
    diseño para conocer el tamaño real de cada columna y solo entonces se
    agregan los rótulos que caben dentro de cada tramo. `fill_of` permite
    colorear una entrada según otro dato (por ejemplo la ocupación) y `legend`
    reemplaza la leyenda por pares (color, texto).
    """
    ax = figure.add_subplot()
    first = min((day.hours.open_min for day in days), default=480)
    last = max((day.hours.close_min for day in days), default=1020)
    first = min([first, *(entry.start for entry in entries)])
    last = max([last, *(entry.end for entry in entries)])
    first, last = first // 60 * 60, -(-last // 60) * 60
    for day in days:
        closed = [
            (first, day.hours.open_min),
            (day.hours.lunch_start, day.hours.lunch_end),
            (day.hours.close_min, last),
        ]
        for start, end in closed:
            if end > start:
                ax.add_patch(Rectangle((day.index, start), 1, end - start, facecolor=CLOSED, edgecolor="none"))
        if day.hours.has_lunch:
            middle = (day.hours.lunch_start + day.hours.lunch_end) / 2
            ax.text(
                day.index + 0.5, middle, "Almuerzo", ha="center", va="center", fontsize=SMALL_SIZE, color=TEXT_MUTED
            )
    boxes = layout_agenda(entries)
    fills: list[str] = []
    regions: list[Region] = []
    dates = {day.index: day.date for day in days}
    for box in boxes:
        entry = box.entry
        fill = fill_of(entry) if fill_of is not None else None
        if fill is None:
            is_session = entry.kind is AgendaKind.SESSION and entry.color
            fill = entry.color if is_session else KIND_COLORS.get(entry.kind, CLOSED)
        fills.append(fill)
        ax.add_patch(
            Rectangle((box.x, entry.start), box.width, entry.duration, facecolor=fill, edgecolor=SURFACE, linewidth=1.2)
        )
        tooltip = (
            f"{day_label(entry.day, dates.get(entry.day))}, {entry.time_label}\n"
            f"{KIND_LABELS[entry.kind]}: {entry.label}"
        )
        if entry.kind is AgendaKind.SESSION and entry.staff and entry.room:
            tooltip += f"\nPersona {entry.staff}, sala {entry.room}"
        regions.append(Region(ax, box.x, box.x + box.width, entry.start, entry.end, tooltip, key=entry))
    for index in range(1, len(days)):
        ax.axvline(index, color=BORDER, linewidth=0.8)
    ax.set_xlim(0, len(days))
    ax.set_ylim(last, first)
    ax.xaxis.tick_top()
    ax.set_xticks([day.index + 0.5 for day in days], [day_label(day.index, day.date) for day in days])
    ax.set_yticks(list(range(first, last + 1, 60)), [format_minute(m) for m in range(first, last + 1, 60)])
    ax.tick_params(length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, pad=10)
    if legend is not None:
        handles: list[Any] = [Patch(color=color, label=label) for color, label in legend]
    else:
        handles = _agenda_handles(entries, names)
    if handles:
        # Columnas según el rótulo más largo (unos 0,55 em por carácter, más el cuadro de color y el espacio).
        width_px = figure.get_size_inches()[0] * figure.dpi
        longest = max(len(str(handle.get_label())) for handle in handles)
        per_item = LABEL_SIZE * figure.dpi / 72 * (0.55 * max(longest, 12) + 3.5)
        figure.legend(
            handles=handles,
            loc="outside lower left",
            ncols=max(1, min(len(handles), int(width_px // per_item))),
            handlelength=1.2,
            handleheight=0.9,
            columnspacing=1.2,
        )
    _agenda_labels(figure, ax, boxes, fills, (first, last), len(days), names, show_staff)
    return regions


def _agenda_labels(
    figure: Figure,
    ax: Axes,
    boxes: Sequence[AgendaBox],
    fills: Sequence[str],
    span: tuple[int, int],
    day_count: int,
    names: Mapping[str, str],
    show_staff: bool,
) -> None:
    """Rótulos dentro de los tramos de la agenda, recortados según el espacio real disponible."""
    layout = figure.get_layout_engine()
    if layout is not None:
        layout.execute(figure)
    extent = ax.get_window_extent()
    px_per_min = extent.height / max(1, span[1] - span[0])
    px_per_day = extent.width / max(1, day_count)
    line_px = SMALL_SIZE * figure.dpi / 72 * 1.3
    char_px = SMALL_SIZE * figure.dpi / 72 * 0.6
    for box, fill in zip(boxes, fills, strict=True):
        entry = box.entry
        height_px = entry.duration * px_per_min
        chars = int((box.width * px_per_day - 8) / char_px)
        lines = _agenda_lines(entry, names, show_staff)
        ink = text_color_on(fill)
        if height_px >= 2 * line_px + 4 and chars >= 6:
            text = "\n".join(shorten(line, chars) for line in lines[:2])
            label = ax.text(
                box.x + 0.03,
                entry.start + 1.5,
                text,
                ha="left",
                va="top",
                fontsize=SMALL_SIZE,
                color=ink,
                linespacing=1.15,
                clip_on=True,
            )
        elif height_px >= line_px and chars >= 4:
            label = ax.text(
                box.x + 0.03,
                entry.start + entry.duration / 2,
                shorten(lines[0], chars),
                ha="left",
                va="center",
                fontsize=6.5,
                color=ink,
                clip_on=True,
            )
        else:
            continue
        label.set_in_layout(False)


def _agenda_lines(entry: AgendaEntry, names: Mapping[str, str], show_staff: bool) -> list[str]:
    if entry.kind is AgendaKind.SESSION:
        service = names.get(entry.service or "", entry.service or "")
        where = entry.staff if show_staff else entry.room
        return [service, f"{entry.time_label} {where or ''}".strip()]
    return [entry.label, entry.time_label]


def _agenda_handles(entries: Sequence[AgendaEntry], names: Mapping[str, str]) -> list[Any]:
    services: dict[str, str] = {}
    kinds: list[AgendaKind] = []
    for entry in entries:
        if entry.kind is AgendaKind.SESSION and entry.service and entry.color:
            services.setdefault(entry.service, entry.color)
        elif entry.kind not in kinds and entry.kind is not AgendaKind.SESSION:
            kinds.append(entry.kind)
    handles: list[Any] = [
        Patch(color=color, label=shorten(names.get(code, code), 26)) for code, color in sorted(services.items())
    ]
    handles += [Patch(color=KIND_COLORS[kind], label=KIND_LABELS[kind]) for kind in AgendaKind if kind in kinds]
    return handles


def draw_runs(figure: Figure, runs: Sequence[RunSummary], current_id: int | None) -> list[Region]:
    """Cobertura optimizada frente a la heurística voraz en las corridas más recientes."""
    ax = figure.add_subplot()
    positions = list(range(len(runs)))
    height = 0.36
    for position, run in zip(positions, runs, strict=True):
        optimized = run.coverage_pct or 0.0
        greedy = run.greedy_coverage_pct or 0.0
        ax.barh(position - height / 2, optimized, height=height, color=SERIES[0], edgecolor=SURFACE, linewidth=1)
        ax.barh(position + height / 2, greedy, height=height, color=BASELINE, edgecolor=SURFACE, linewidth=1)
        ax.text(
            optimized + 0.01,
            position - height / 2,
            format_pct(optimized, 1),
            va="center",
            fontsize=SMALL_SIZE,
            color=TEXT,
        )
        ax.text(
            greedy + 0.01,
            position + height / 2,
            format_pct(greedy, 1),
            va="center",
            fontsize=SMALL_SIZE,
            color=TEXT_MUTED,
        )
    regions = [
        Region(
            ax,
            0,
            1.15,
            position - 0.48,
            position + 0.48,
            f"Corrida {run.id}: {run.scenario_name}\nCobertura optimizada {format_pct(run.coverage_pct)}\n"
            f"Heurística voraz {format_pct(run.greedy_coverage_pct)}",
            key=run.id,
        )
        for position, run in zip(positions, runs, strict=True)
    ]
    ax.set_yticks(positions, [run_chart_label(run) for run in runs])
    for label, run in zip(ax.get_yticklabels(), runs, strict=True):
        if run.id == current_id:
            label.set_fontweight("bold")
    ax.set_ylim(max(len(runs), MIN_ROW_SLOTS) - 0.5, -0.5)
    ax.set_xlim(0, 1.15)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.xaxis.set_major_formatter(pct_formatter())
    ax.set_xlabel("Sesiones cubiertas / requeridas")
    _horizontal_grid(ax)
    handles = [Patch(color=SERIES[0], label="Optimizado"), Patch(color=BASELINE, label="Heurística voraz")]
    _title_with_legend(ax, "Cobertura por corrida", handles)
    return regions


def chart_has_data(figure: Figure, predicate: Callable[[Axes], bool] | None = None) -> bool:
    """Indica si la figura tiene al menos un eje con elementos dibujados (para la autoprueba)."""
    for ax in figure.axes:
        if predicate is not None:
            if predicate(ax):
                return True
        elif ax.patches or ax.lines or ax.collections or ax.images:
            return True
    return False
