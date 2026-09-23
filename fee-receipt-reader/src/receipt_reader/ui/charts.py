"""Gráficos de la pestaña Gráficos: preparación de datos y dibujo con matplotlib.

Las funciones de preparación son puras (se prueban sin ventana) y las de
dibujo reciben una `Figure` vacía. Criterios comunes: título y subtítulo a la
izquierda, números con formato chileno, colores de programa fijos según el
orden del catálogo y colores de estado solo cuando el color significa estado.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter, MaxNLocator

from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import PeriodAxis, Program, ReceiptStatus, ReferenceRate
from receipt_reader.domain.records import ReceiptRow
from receipt_reader.domain.retention import reference_year
from receipt_reader.ui.formatting import (
    MONTHS_SHORT,
    format_clp,
    format_decimal,
    format_int,
    format_millions,
    format_pct,
    plural,
)
from receipt_reader.ui.style import (
    GRID,
    OTHER_SERIES,
    SERIES,
    STATUS_COLORS,
    SURFACE,
    TEXT,
    TEXT_MUTED,
    WARNING,
)

HOURLY_OUT_OF_RANGE = "HOURLY_RATE_OUT_OF_RANGE"
STATUS_ORDER = (
    ReceiptStatus.APPROVED,
    ReceiptStatus.CORRECTED,
    ReceiptStatus.PENDING,
    ReceiptStatus.ERROR,
    ReceiptStatus.DISCARDED,
)
REFERENCE_BAND = "#E3E8EF"
MIN_SLOTS = 6
MIN_HOURLY_ROWS = 3
BAR_WIDTH = 0.62
# Hasta este largo el eje mensual se completa con los meses sin boletas; más allá solo se muestran los meses con datos.
MAX_FILLED_MONTHS = 36
# Con más meses que esto se deja de rotular el total sobre cada barra (el valor queda en la pestaña Resumen).
MAX_LABELED_BARS = 18

# Pasos de las marcas del eje de montos: 1, 2 o 5 por potencia de 10, que se rotulan sin redondear.
AMOUNT_STEPS = [1, 2, 5, 10]
MAX_TICK_DECIMALS = 3

EMPTY_MONTHLY = "No hay boletas aprobadas o corregidas con período para los filtros elegidos."
EMPTY_STATUS = "No hay boletas registradas para los filtros elegidos."
EMPTY_HOURLY = "No hay boletas válidas con horas y tipo de jornada para calcular el valor hora."


@dataclass(frozen=True)
class ProgramStyle:
    """Nombre corto y color fijo de un programa en los gráficos."""

    name: str
    short_name: str
    color: str


def program_styles(programs: Sequence[Program]) -> dict[str, ProgramStyle]:
    """Color por programa según su código de carpeta: el color sigue al programa aunque cambien los filtros."""
    styles: dict[str, ProgramStyle] = {}
    for index, program in enumerate(sorted(programs, key=lambda item: item.folder_code)):
        color = SERIES[index] if index < len(SERIES) else OTHER_SERIES
        styles[program.name] = ProgramStyle(program.name, program.short_name, color)
    return styles


def style_for(styles: Mapping[str, ProgramStyle], name: str) -> ProgramStyle:
    return styles.get(name) or ProgramStyle(name, name, OTHER_SERIES)


def ordered_series_names(names: Sequence[str], styles: Mapping[str, ProgramStyle]) -> list[str]:
    """Orden de apilado estable: primero el orden del catálogo y después los desconocidos por nombre."""
    catalog = [name for name in styles if name in names]
    others = sorted(name for name in names if name not in styles)
    return catalog + others


def tick_step(count: int) -> int:
    """Cada cuántos meses se rotula el eje para que los rótulos no se toquen."""
    if count <= MAX_LABELED_BARS:
        return 1
    return 2 if count <= 30 else 3


def period_tick_labels(periods: Sequence[Period], step: int = 1) -> list[str]:
    """Mes abreviado cada `step` meses (vacío en los demás); el año va en el primer rótulo y cada vez que cambia."""
    labels: list[str] = []
    previous_year: int | None = None
    for index, period in enumerate(periods):
        if index % step:
            labels.append("")
            continue
        month = MONTHS_SHORT[period.month - 1]
        labels.append(f"{month}\n{period.year}" if period.year != previous_year else month)
        previous_year = period.year
    return labels


def fill_month_gaps(
    periods: Sequence[Period], series: Mapping[str, Sequence[int]], max_months: int = MAX_FILLED_MONTHS
) -> tuple[list[Period], dict[str, list[int]], bool]:
    """Completa con cero los meses sin boletas para que el eje de tiempo no oculte huecos.

    Devuelve los períodos, las series alineadas con ellos y si el eje quedó
    continuo. Si el rango supera `max_months` (por ejemplo, una boleta antigua
    aislada) se conservan solo los meses con datos y el eje no es continuo.
    """
    values = {name: dict(zip(periods, points, strict=True)) for name, points in series.items()}
    ordered = sorted(set(periods))
    if not ordered:
        return [], {name: [] for name in series}, True
    span = ordered[0].months_until(ordered[-1]) + 1
    if span > max_months:
        return ordered, {name: [by_period.get(p, 0) for p in ordered] for name, by_period in values.items()}, False
    months = [ordered[0].shift(offset) for offset in range(span)]
    return months, {name: [by_period.get(p, 0) for p in months] for name, by_period in values.items()}, True


@dataclass(frozen=True)
class AmountUnit:
    """Unidad del eje de montos: millones de pesos o, si el máximo no llega al millón, miles."""

    divisor: int
    label: str

    def bar_label(self, value: float) -> str:
        """Total sobre una barra, en la unidad del eje."""
        if self.divisor == MILLIONS.divisor:
            return format_millions(value)
        return format_int(value / self.divisor)


MILLIONS = AmountUnit(1_000_000, "Millones de pesos")
THOUSANDS = AmountUnit(1_000, "Miles de pesos")


def amount_unit(top: float) -> AmountUnit:
    return MILLIONS if top >= MILLIONS.divisor else THOUSANDS


def tick_decimals(step: float, max_decimals: int = MAX_TICK_DECIMALS) -> int:
    """Decimales mínimos para rotular marcas separadas por `step` (en la unidad del eje) sin repetirlas.

    0,2 necesita 1 decimal, 0,25 necesita 2 y 5 ninguno.
    """
    for decimals in range(max_decimals + 1):
        scaled = step * 10**decimals
        if abs(scaled - round(scaled)) < 1e-6:
            return decimals
    return max_decimals


def amount_formatter(unit: AmountUnit, step: float) -> FuncFormatter:
    """Rótulos del eje de montos con los decimales que exige la separación real entre marcas."""
    decimals = tick_decimals(step / unit.divisor)

    def label(value: float, _position: int | None = None) -> str:
        scaled = value / unit.divisor
        if abs(scaled) < 1e-9:
            return "0"
        return format_decimal(scaled, decimals) if decimals else format_int(scaled)

    return FuncFormatter(label)


def pesos_tick(value: float, _position: int | None = None) -> str:
    return format_int(value)


def _titles(ax: Axes, title: str, subtitle: str = "") -> None:
    ax.set_title(title, loc="left", pad=19 if subtitle else 8)
    if subtitle:
        ax.annotate(
            subtitle,
            xy=(0, 1),
            xycoords="axes fraction",
            xytext=(0, 5),
            textcoords="offset points",
            ha="left",
            va="bottom",
            fontsize=8.5,
            color=TEXT_MUTED,
        )


def _centered_limits(ax: Axes, count: int) -> None:
    """Deja al menos MIN_SLOTS espacios para que pocas barras no se vuelvan bloques anchos."""
    pad = max(0.0, (MIN_SLOTS - count) / 2)
    ax.set_xlim(-0.5 - pad, count - 0.5 + pad)


def draw_monthly_gross(
    figure: Figure,
    periods: Sequence[Period],
    series: Mapping[str, Sequence[int]],
    styles: Mapping[str, ProgramStyle],
    *,
    axis: PeriodAxis = PeriodAxis.SERVICE,
    without_period: int = 0,
) -> Axes:
    """Barras apiladas del bruto mensual por programa, con el total de cada mes sobre la barra.

    Los meses sin boletas aparecen vacíos (el eje es de tiempo continuo) salvo
    que el rango sea demasiado largo; en ese caso el subtítulo lo advierte.
    """
    ax = figure.add_subplot()
    months, filled, continuous = fill_month_gaps(periods, series)
    names = ordered_series_names(list(filled), styles)
    positions = list(range(len(months)))
    bottoms = [0] * len(months)
    labeled = len(months) <= MAX_LABELED_BARS
    for name in names:
        values = filled[name]
        style = style_for(styles, name)
        ax.bar(
            positions,
            values,
            BAR_WIDTH,
            bottom=bottoms,
            color=style.color,
            label=style.short_name,
            edgecolor=SURFACE,
            linewidth=1.2,
            zorder=2,
        )
        bottoms = [base + value for base, value in zip(bottoms, values, strict=True)]
    top = max(bottoms, default=0)
    unit = amount_unit(top)
    for position, total in zip(positions, bottoms, strict=True):
        if labeled and total > 0:
            ax.annotate(
                unit.bar_label(total),
                xy=(position, total),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
                color=TEXT_MUTED,
            )
    ax.set_ylim(0, top * 1.12 if top else 1)
    locator = MaxNLocator(nbins=6, steps=AMOUNT_STEPS)
    ax.yaxis.set_major_locator(locator)
    ticks = locator.tick_values(0, ax.get_ylim()[1])
    step = float(ticks[1] - ticks[0]) if len(ticks) > 1 else float(unit.divisor)
    ax.yaxis.set_major_formatter(amount_formatter(unit, step))
    ax.set_ylabel(unit.label)
    ax.set_xticks(positions, period_tick_labels(months, tick_step(len(months))))
    _centered_limits(ax, len(months))
    subtitle = f"Boletas aprobadas o corregidas, por {axis.label.lower()}."
    if labeled:
        subtitle += " Sobre cada barra, el total del mes."
    if not continuous:
        subtitle += " Se omiten los meses sin boletas."
    if without_period:
        subtitle += f" No incluye {format_clp(without_period)} sin período."
    # Con una sola serie no hay leyenda: el título nombra el programa.
    title = f"Bruto mensual de {style_for(styles, names[0]).short_name}" if len(names) == 1 else None
    _titles(ax, title or "Bruto mensual por programa", subtitle)
    if len(names) > 1:
        figure.legend(loc="outside lower center", ncol=min(len(names), 4), handlelength=1.1, columnspacing=1.6)
    return ax


def status_counts_in_order(counts: Mapping[ReceiptStatus, int]) -> list[tuple[ReceiptStatus, int]]:
    """Todos los estados en orden fijo (también los que tienen cero boletas)."""
    return [(status, counts.get(status, 0)) for status in STATUS_ORDER]


def draw_status_counts(figure: Figure, counts: Mapping[ReceiptStatus, int]) -> Axes:
    """Barras horizontales con la cantidad de boletas por estado y su porcentaje."""
    ax = figure.add_subplot()
    items = status_counts_in_order(counts)
    total = sum(count for _status, count in items)
    positions = list(range(len(items)))
    values = [count for _status, count in items]
    ax.barh(
        positions,
        values,
        height=0.46,
        color=[STATUS_COLORS[status.value] for status, _count in items],
        zorder=2,
    )
    for position, (_status, count) in zip(positions, items, strict=True):
        share = format_pct(count / total, 0) if total else "-"
        ax.annotate(
            f"{format_int(count)}  ({share})",
            xy=(count, position),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            fontsize=8.5,
            color=TEXT,
        )
    ax.set_yticks(positions, [status.label for status, _count in items])
    ax.set_ylim(len(items) - 0.5, -0.5)
    ax.set_xlim(0, max(values, default=0) * 1.35 or 1)
    ax.xaxis.set_visible(False)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="y", colors=TEXT, pad=4)
    _titles(ax, "Boletas por estado", f"{plural(total, 'boleta registrada', 'boletas registradas')}")
    return ax


@dataclass(frozen=True)
class HourlyGroup:
    """Valores hora de las boletas válidas de un programa y su rango de referencia."""

    program_id: int
    label: str
    color: str
    rates: tuple[int, ...]
    flagged: tuple[bool, ...]
    reference: tuple[int, int] | None

    @property
    def median(self) -> float:
        return float(median(self.rates))

    @property
    def flagged_count(self) -> int:
        return sum(self.flagged)


def hourly_groups(
    rows: Sequence[ReceiptRow],
    programs: Sequence[Program],
    reference_rates: Sequence[ReferenceRate],
) -> list[HourlyGroup]:
    """Agrupa por programa el valor hora implícito de las boletas válidas.

    El rango de referencia de cada programa cubre los mismos años con que valida
    la regla del valor hora: el del servicio o, si falta, el de la emisión (si
    hay dos años, va del menor mínimo al mayor máximo). Una boleta queda marcada
    si la validación le dejó la advertencia de valor hora fuera de referencia.
    """
    styles = program_styles(programs)
    by_program: dict[int, list[ReceiptRow]] = {}
    for row in rows:
        if row.status.is_valid and row.hourly_rate is not None and row.program_id is not None:
            by_program.setdefault(row.program_id, []).append(row)
    ordered = sorted(programs, key=lambda item: item.folder_code)
    known = {program.id for program in ordered}
    groups: list[HourlyGroup] = []
    for program_id in [p.id for p in ordered] + sorted(set(by_program) - known):
        members = by_program.get(program_id)
        if not members:
            continue
        program = next((p for p in ordered if p.id == program_id), None)
        style = style_for(styles, program.name) if program else ProgramStyle("", str(program_id), OTHER_SERIES)
        years = {reference_year(row.service_period, row.issue_date) for row in members}
        ranges = [rate for rate in reference_rates if rate.program_id == program_id and rate.year in years]
        reference = (min(r.min_hourly for r in ranges), max(r.max_hourly for r in ranges)) if ranges else None
        groups.append(
            HourlyGroup(
                program_id=program_id,
                label=style.short_name,
                color=style.color,
                rates=tuple(row.hourly_rate or 0 for row in members),
                flagged=tuple(HOURLY_OUT_OF_RANGE in row.issue_codes for row in members),
                reference=reference,
            )
        )
    return groups


def jitter_offsets(count: int, spread: float = 0.44) -> list[float]:
    """Desplazamientos verticales deterministas (secuencia áurea) para que los puntos no se tapen."""
    return [((index * 0.618_034) % 1.0 - 0.5) * spread for index in range(count)]


def draw_hourly_rates(figure: Figure, groups: Sequence[HourlyGroup]) -> Axes:
    """Puntos por boleta sobre la banda de referencia de cada programa, con la mediana marcada."""
    ax = figure.add_subplot()
    any_flagged = False
    for row_index, group in enumerate(groups):
        if group.reference is not None:
            low, high = group.reference
            ax.add_patch(
                Rectangle((low, row_index - 0.34), high - low, 0.68, facecolor=REFERENCE_BAND, linewidth=0, zorder=0)
            )
        offsets = jitter_offsets(len(group.rates))
        normal = [
            (rate, row_index + offset)
            for rate, offset, flag in zip(group.rates, offsets, group.flagged, strict=True)
            if not flag
        ]
        flagged = [
            (rate, row_index + offset)
            for rate, offset, flag in zip(group.rates, offsets, group.flagged, strict=True)
            if flag
        ]
        if normal:
            ax.scatter(
                [x for x, _y in normal],
                [y for _x, y in normal],
                s=30,
                color=group.color,
                edgecolors=SURFACE,
                linewidths=1.0,
                alpha=0.9,
                zorder=3,
            )
        if flagged:
            any_flagged = True
            ax.scatter(
                [x for x, _y in flagged],
                [y for _x, y in flagged],
                s=42,
                marker="D",
                color=WARNING,
                edgecolors=SURFACE,
                linewidths=1.0,
                zorder=4,
            )
        middle = group.median
        ax.plot([middle, middle], [row_index - 0.3, row_index + 0.3], color=TEXT, linewidth=2, zorder=5)
    ax.set_yticks(range(len(groups)), [group.label for group in groups])
    # Con uno o dos programas las filas no ocupan todo el alto: quedan centradas y con el alto de siempre.
    pad = max(0.0, (MIN_HOURLY_ROWS - len(groups)) / 2)
    ax.set_ylim(len(groups) - 0.5 + pad, -0.5 - pad)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True, color=GRID)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    ax.xaxis.set_major_formatter(FuncFormatter(pesos_tick))
    ax.set_xlabel("Pesos por hora")
    ax.tick_params(axis="y", colors=TEXT, pad=4)
    ax.autoscale_view(scalex=True, scaley=False)
    left, right = ax.get_xlim()
    margin = (right - left) * 0.04
    ax.set_xlim(left - margin, right + margin)
    total = sum(len(group.rates) for group in groups)
    _titles(ax, "Valor hora implícito por programa", f"Cada punto es una boleta válida ({format_int(total)} en total)")
    handles: list[Patch | Line2D] = [
        Patch(facecolor=REFERENCE_BAND, label="Rango de referencia"),
        Line2D([0], [0], color=TEXT, linewidth=2, label="Mediana"),
    ]
    if any_flagged:
        handles.append(
            Line2D([0], [0], marker="D", linestyle="none", color=WARNING, markersize=6, label="Fuera de referencia")
        )
    figure.legend(handles=handles, loc="outside lower center", ncol=len(handles), handlelength=1.4, columnspacing=1.6)
    return ax
