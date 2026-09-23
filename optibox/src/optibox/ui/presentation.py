"""Lógica de presentación sin Qt ni matplotlib: arma los datos que muestran los gráficos y las tablas.

Se mantiene separada de los widgets para poder probarla directamente: grillas
de cobertura y demanda por día y bloque, faltantes agrupados por causa,
distribución de la agenda en carriles, textos de resumen y tonos de estado.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from optibox.domain.agenda import AgendaEntry
from optibox.domain.diagnosis import CAUSE_LABELS, UnmetCause, UnmetDemand
from optibox.domain.instance import PlanningDay
from optibox.domain.metrics import StaffHours, StaffLoad, ratio
from optibox.domain.run import RunDetail, RunSummary
from optibox.domain.timegrid import BLOCK_MINUTES, WEEKDAY_SHORT, format_range
from optibox.ui.formatting import format_count, format_date, format_decimal, format_int, format_pct, format_seconds

COVERAGE_GOOD = 0.95
COVERAGE_WARNING = 0.85

STATUS_LABELS = {"óptimo": "Óptimo", "factible": "Factible", "heurística": "Heurística"}

# Partes de la carga semanal de una persona, en el orden en que se apilan.
LOAD_PARTS: tuple[tuple[str, str], ...] = (
    ("session_min", "Atención"),
    ("admin_min", "Administrativo"),
    ("meeting_min", "Reuniones y bloqueos"),
    ("non_service_min", "Tareas no asistenciales"),
    ("free_min", "Libre"),
)

# Partes del rendimiento de horas semanal de una persona, en el orden en que se apilan.
HOURS_PARTS: tuple[tuple[str, str], ...] = (
    ("session_min", "Atención"),
    ("admin_min", "Administrativo"),
    ("meeting_min", "Reuniones y bloqueos"),
    ("leave_min", "Permisos"),
    ("idle_min", "Ociosas"),
)

CAUSE_ORDER: tuple[UnmetCause, ...] = tuple(UnmetCause)


def day_label(index: int, day: date | None = None) -> str:
    """(0, 28-09-2026) -> 'Lun 28-09'; sin fecha solo el día abreviado."""
    short = WEEKDAY_SHORT[index]
    return f"{short} {day:%d-%m}" if day is not None else short


def week_range_text(monday: date, days: Sequence[PlanningDay] | None = None) -> str:
    """Texto de la semana planificada: 'lunes 28-09-2026 a viernes 02-10-2026'."""
    friday = days[-1].date if days else date.fromordinal(monday.toordinal() + 4)
    return f"lunes {format_date(monday)} a viernes {format_date(friday)}"


def coverage_tone(pct: float | None) -> str:
    """Tono de semáforo para un porcentaje de cobertura."""
    if pct is None:
        return "muted"
    if pct >= COVERAGE_GOOD:
        return "good"
    if pct >= COVERAGE_WARNING:
        return "warning"
    return "bad"


def status_label(status: str | None) -> str:
    if not status:
        return "-"
    return STATUS_LABELS.get(status, status.capitalize())


# Enteros sueltos de cuatro o más cifras; se excluyen los que forman parte de decimales, fechas u horas.
_LONG_INTEGER = re.compile(r"(?<![\d:/-])(?<!\d[.,])\d{4,}(?![\d:/-])(?![.,]\d)")


def progress_text(message: str) -> str:
    """Mensaje de avance con los enteros largos en formato chileno: 'valor 186700' -> 'valor 186.700'."""
    return _LONG_INTEGER.sub(lambda match: format_int(int(match.group())), message)


def shorten(text: str, limit: int) -> str:
    """Recorta un texto largo con puntos suspensivos para etiquetas de ejes."""
    return text if len(text) <= limit else text[: max(1, limit - 3)].rstrip() + "..."


def staff_label(code: str, name: str, limit: int = 30) -> str:
    """'TO-01' y 'Catalina Vergara Valenzuela' -> 'TO-01 Catalina Vergara V.'."""
    parts = name.split()
    compact = name
    if len(parts) >= 3:
        compact = f"{parts[0]} {parts[1]} {parts[2][0]}."
    return shorten(f"{code} {compact}", limit)


def run_list_text(run: RunSummary) -> str:
    """Texto de dos líneas para el historial del panel lateral."""
    return (
        f"Corrida {run.id}: {run.scenario_name}\n"
        f"Semana {format_date(run.week_start)}, cobertura {format_pct(run.coverage_pct)}"
    )


def run_chart_label(run: RunSummary) -> str:
    return f"{run.id}. {shorten(run.scenario_name, 22)}\n{format_date(run.week_start)}"


def run_info_text(detail: RunDetail) -> str:
    """Línea de contexto de la corrida mostrada: escenario, semana, estado y tiempos."""
    summary = detail.summary
    instance = detail.instance
    parts = [
        f"Corrida {summary.id}, escenario {summary.scenario_name}, "
        f"semana del {week_range_text(instance.week_start, instance.days)}.",
        f"Estado {status_label(summary.status).lower()}; heurística {format_pct(summary.greedy_coverage_pct)} "
        f"frente a {format_pct(summary.coverage_pct)} optimizado.",
        f"Tiempo total {format_seconds(summary.total_seconds)} con límite de búsqueda de "
        f"{format_seconds(summary.time_limit_s)}.",
    ]
    holidays = [day for day in instance.days if not day.is_open]
    if holidays:
        names = ", ".join(f"{day_label(day.index, day.date)} ({day.holiday})" for day in holidays)
        parts.append(f"Feriado: {names}.")
    if instance.holiday_demand_dropped:
        dropped = format_count(instance.holiday_demand_dropped, "sesión de demanda cae", "sesiones de demanda caen")
        parts.append(f"{dropped} en feriado.")
    return " ".join(parts)


@dataclass(frozen=True)
class GridCell:
    """Celda de una grilla día por bloque: requerido, cubierto y detalle por tipo de atención."""

    day: int
    block: int
    is_open: bool
    required: int = 0
    covered: int = 0
    details: tuple[tuple[str, int, int], ...] = ()

    @property
    def pct(self) -> float | None:
        if not self.is_open:
            return None
        return ratio(min(self.covered, self.required), self.required)

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.covered)

    @property
    def time_label(self) -> str:
        return format_range(self.block, self.block + BLOCK_MINUTES)


@dataclass(frozen=True)
class BlockGrid:
    """Grilla de bloques horarios (filas) por día (columnas)."""

    days: tuple[int, ...]
    day_labels: tuple[str, ...]
    blocks: tuple[int, ...]
    cells: tuple[tuple[GridCell, ...], ...]

    def cell(self, day: int, block: int) -> GridCell | None:
        if day not in self.days or block not in self.blocks:
            return None
        return self.cells[self.blocks.index(block)][self.days.index(day)]

    def iter_cells(self) -> Iterable[GridCell]:
        for row in self.cells:
            yield from row

    @property
    def max_required(self) -> int:
        return max((cell.required for cell in self.iter_cells()), default=0)

    @property
    def total_required(self) -> int:
        return sum(cell.required for cell in self.iter_cells())


def _grid(
    days: Sequence[tuple[int, str, frozenset[int]]],
    values: dict[tuple[int, int], list[tuple[str, int, int]]],
) -> BlockGrid:
    """Arma la grilla desde (día, etiqueta, bloques abiertos) y el detalle por (día, bloque)."""
    blocks = sorted({block for _, _, open_blocks in days for block in open_blocks})
    rows: list[tuple[GridCell, ...]] = []
    for block in blocks:
        row: list[GridCell] = []
        for index, _, open_blocks in days:
            details = tuple(sorted(values.get((index, block), [])))
            row.append(
                GridCell(
                    day=index,
                    block=block,
                    is_open=block in open_blocks,
                    required=sum(required for _, required, _ in details),
                    covered=sum(covered for _, _, covered in details),
                    details=details,
                )
            )
        rows.append(tuple(row))
    return BlockGrid(
        days=tuple(index for index, _, _ in days),
        day_labels=tuple(label for _, label, _ in days),
        blocks=tuple(blocks),
        cells=tuple(rows),
    )


def coverage_grid(detail: RunDetail, service: str | None = None) -> BlockGrid:
    """Cobertura por día y bloque de la corrida, de todos los tipos o de uno solo."""
    days = [
        (day.index, day_label(day.index, day.date), frozenset(day.hours.blocks()) if day.is_open else frozenset())
        for day in detail.instance.days
    ]
    values: dict[tuple[int, int], list[tuple[str, int, int]]] = defaultdict(list)
    for row in detail.metrics.coverage_by_block_service:
        if row.service is None or (service is not None and row.service != service):
            continue
        values[(row.day, row.block)].append((row.service, row.required, row.covered))
    return _grid(days, values)


def demand_grid(
    rows: Iterable[tuple[int, int, str, int]],
    open_blocks: dict[int, Sequence[int]],
    service: str | None = None,
) -> BlockGrid:
    """Demanda registrada por día de la semana y bloque: filas (día, bloque, tipo, sesiones)."""
    days = [(weekday, day_label(weekday), frozenset(blocks)) for weekday, blocks in sorted(open_blocks.items())]
    values: dict[tuple[int, int], list[tuple[str, int, int]]] = defaultdict(list)
    for weekday, block, code, sessions in rows:
        if (service is None or code == service) and sessions:
            values[(weekday, block)].append((code, sessions, 0))
    return _grid(days, values)


@dataclass(frozen=True)
class UnmetBar:
    """Faltante de un (día, bloque) sumando todos los tipos, separado por causa."""

    day: int
    block: int
    by_cause: tuple[tuple[UnmetCause, int], ...]
    items: tuple[UnmetDemand, ...]

    @property
    def total(self) -> int:
        return sum(amount for _, amount in self.by_cause)


def unmet_by_block(unmet: Iterable[UnmetDemand]) -> tuple[UnmetBar, ...]:
    """Agrupa los turnos sin cubrir por (día, bloque), con el faltante por causa en orden fijo."""
    grouped: dict[tuple[int, int], list[UnmetDemand]] = defaultdict(list)
    for item in unmet:
        if item.shortfall > 0:
            grouped[(item.day, item.block)].append(item)
    bars: list[UnmetBar] = []
    for (day, block), items in sorted(grouped.items()):
        totals: dict[UnmetCause, int] = defaultdict(int)
        for item in items:
            totals[item.cause] += item.shortfall
        by_cause = tuple((cause, totals[cause]) for cause in CAUSE_ORDER if totals.get(cause))
        bars.append(UnmetBar(day, block, by_cause, tuple(sorted(items, key=lambda i: i.service))))
    return tuple(bars)


def causes_present(bars: Iterable[UnmetBar]) -> tuple[UnmetCause, ...]:
    present = {cause for bar in bars for cause, _ in bar.by_cause}
    return tuple(cause for cause in CAUSE_ORDER if cause in present)


def cause_label(cause: UnmetCause) -> str:
    return CAUSE_LABELS[cause]


def load_parts(load: StaffLoad) -> tuple[tuple[str, int], ...]:
    """Minutos de cada parte de la carga semanal, en el orden de apilado."""
    return tuple((label, int(getattr(load, key))) for key, label in LOAD_PARTS)


def hours_parts(hours: StaffHours) -> tuple[tuple[str, int], ...]:
    """Minutos de cada parte del rendimiento de horas semanal, en el orden de apilado."""
    return tuple((label, int(getattr(hours, key))) for key, label in HOURS_PARTS)


@dataclass(frozen=True)
class AgendaBox:
    """Posición horizontal de una entrada de la agenda dentro de la columna de su día.

    `x` y `width` están en unidades de día (la columna del día d va de d a d + 1).
    Las entradas que se solapan comparten la columna en carriles del mismo ancho.
    """

    entry: AgendaEntry
    x: float
    width: float
    lanes: int


def layout_agenda(entries: Iterable[AgendaEntry], gap: float = 0.06) -> tuple[AgendaBox, ...]:
    """Distribuye las entradas en su columna: cada grupo de entradas solapadas se divide en carriles."""
    by_day: dict[int, list[AgendaEntry]] = defaultdict(list)
    for entry in entries:
        by_day[entry.day].append(entry)
    boxes: list[AgendaBox] = []
    usable = 1.0 - 2 * gap
    for day, day_entries in sorted(by_day.items()):
        ordered = sorted(day_entries, key=lambda e: (e.start, e.lane, e.end))
        clusters: list[list[AgendaEntry]] = []
        cluster_end = -1
        for entry in ordered:
            if clusters and entry.start < cluster_end:
                clusters[-1].append(entry)
                cluster_end = max(cluster_end, entry.end)
            else:
                clusters.append([entry])
                cluster_end = entry.end
        for cluster in clusters:
            lanes_in_cluster = sorted({entry.lane for entry in cluster})
            position = {lane: index for index, lane in enumerate(lanes_in_cluster)}
            lanes = len(lanes_in_cluster)
            width = usable / lanes
            for entry in cluster:
                boxes.append(AgendaBox(entry, day + gap + position[entry.lane] * width, width, lanes))
    return tuple(boxes)


@dataclass(frozen=True)
class OccupancyBand:
    """Rango de personas simultáneas en la sala administrativa; `over` marca los que superan la recomendada."""

    low: int
    high: int
    over: bool = False

    def contains(self, people: int) -> bool:
        return self.low <= people <= self.high

    @property
    def label(self) -> str:
        if self.low == self.high:
            text = format_count(self.low, "persona", "personas")
        else:
            text = f"{self.low} a {self.high} personas"
        return f"{text} (sobre la recomendada)" if self.over else text


def occupancy_bands(soft: int, hard: int) -> tuple[OccupancyBand, ...]:
    """Hasta tres rangos parejos entre 1 y la capacidad recomendada, más uno hasta la capacidad máxima.

    (8, 10) -> 1 a 3, 4 a 5, 6 a 8 y 9 a 10 (sobre la recomendada).
    """
    hard = max(1, hard)
    soft = max(1, min(soft, hard))
    bands: list[OccupancyBand] = []
    low = 1
    for high in sorted({max(1, round(soft * step / 3)) for step in (1, 2, 3)}):
        if high >= low:
            bands.append(OccupancyBand(low, high))
            low = high + 1
    if hard > soft:
        bands.append(OccupancyBand(soft + 1, hard, over=True))
    return tuple(bands)


def band_of(bands: Sequence[OccupancyBand], people: int) -> OccupancyBand | None:
    return next((band for band in bands if band.contains(people)), None)


def admin_room_people(detail: RunDetail) -> dict[tuple[int, int], int]:
    """Personas en la sala administrativa por (día, minuto de inicio del slot)."""
    return {(slot.day, slot.start): slot.people for slot in detail.metrics.admin_room}


def _channel(value: int) -> float:
    srgb = value / 255
    return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """Luminancia relativa (WCAG) de un color '#RRGGBB'."""
    text = color.lstrip("#")
    red, green, blue = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(first: str, second: str) -> float:
    high, low = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def text_color_on(fill: str, dark: str = "#1F2933", light: str = "#FFFFFF") -> str:
    """Color de texto (oscuro o blanco) con mejor contraste sobre un relleno."""
    return light if contrast_ratio(fill, light) >= contrast_ratio(fill, dark) else dark


def hours_text(minutes: int) -> str:
    """Minutos como horas sin unidad y con coma decimal: 450 -> '7,5'."""
    return format_decimal(minutes / 60, 1)
