"""Gráficos de la interfaz dibujados con matplotlib sobre una figura recibida.

Las funciones no dependen de Qt: reciben la figura y los resultados del motor,
de modo que se pueden probar dibujando en memoria. Convenciones: un solo eje
por gráfico, números en formato chileno, colores de semáforo solo para estados
y leyenda solo cuando hay más de una serie.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm, to_rgb
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator, MultipleLocator

from kpi_monitor.domain.enums import Status
from kpi_monitor.domain.models import Period, Thresholds
from kpi_monitor.domain.results import IndicatorResult
from kpi_monitor.domain.summary import StatusMatrix
from kpi_monitor.palette import (
    ACCENT,
    BORDER,
    PRIMARY,
    SERIES,
    STATUS_COLORS,
    SURFACE,
    TEXT,
    TEXT_MUTED,
)
from kpi_monitor.ui.formatting import MONTHS_SHORT, format_pct, format_value
from kpi_monitor.ui.presenters import STATUS_SHORT, axis_label, axis_value, cell_text, indicator_label

# Tope visual de las barras de cumplimiento: sobre él la barra se corta y la etiqueta muestra el valor real.
DISPLAY_CAP = 1.5
BAR_HEIGHT = 0.62
# Grosor máximo de una barra horizontal: con pocas filas en un gráfico alto, barras finas y no bloques.
MAX_BAR_PX = 24
MONTHLY_BAR = SERIES[4]
BAND_COLOR = ACCENT
# Serie categórica de los programas, validada para visión de colores alterada (todas las parejas).
# El color sigue al programa según su orden en el catálogo, nunca a su posición en un filtro.
PROGRAM_COLORS = ("#2A78D6", "#EB6834", "#1BAF7A")
# Escala divergente del mapa de calor: bajo la meta (cálido), en la meta (gris neutro), sobre la meta (frío).
HEAT_COLORS = ("#B2352F", "#E39A8F", "#F0EFEC", "#9EC5F4", "#256ABF")
HEAT_EMPTY = "#F4F5F7"
PCT = FuncFormatter(lambda value, _pos: format_pct(value, 0))
_EVALUATED = (Status.GREEN, Status.YELLOW, Status.RED)
# Rótulos cortos de los estados sin color dentro de las celdas del mapa de calor.
HEAT_LABELS = {
    Status.NO_DATA: "Sin datos",
    Status.UNDETERMINED: "Sin meta",
    Status.BASELINE: "Línea base",
    Status.NOT_APPLICABLE: "No aplica",
    Status.PENDING: "Pendiente",
}


def program_color(position: int) -> str:
    """Color fijo de un programa según su posición en el catálogo."""
    return PROGRAM_COLORS[position % len(PROGRAM_COLORS)]


@dataclass(frozen=True)
class ProgramSeries:
    """Serie del índice ponderado de un programa para el gráfico de evolución."""

    code: str
    name: str
    color: str
    values: tuple[float | None, ...]
    projected: float | None


@dataclass(frozen=True)
class HeatmapLayout:
    """Correspondencia entre filas y columnas del mapa de calor y los códigos que representan."""

    indicator_codes: tuple[str, ...]
    site_codes: tuple[str | None, ...]

    def cell_at(self, x: float | None, y: float | None) -> tuple[str, str | None] | None:
        if x is None or y is None:
            return None
        col, row = round(x), round(y)
        if 0 <= row < len(self.indicator_codes) and 0 <= col < len(self.site_codes):
            return self.indicator_codes[row], self.site_codes[col]
        return None


def row_at(y: float | None, count: int) -> int | None:
    """Fila de un gráfico de barras horizontales bajo el puntero (None fuera de las barras)."""
    if y is None or math.isnan(y):
        return None
    row = round(y)
    return row if 0 <= row < count and abs(y - row) <= 0.5 else None


def heading(ax: Axes, title: str, subtitle: str = "", top_offset: float = 0) -> None:
    """Título en negrita alineado a la izquierda y, debajo, una línea de contexto atenuada.

    `top_offset` (en puntos) deja lugar a etiquetas de eje ubicadas sobre el gráfico.
    """
    ax.set_title(title, loc="left", pad=(20 if subtitle else 8) + top_offset)
    if subtitle:
        ax.annotate(
            subtitle,
            xy=(0, 1),
            xycoords="axes fraction",
            xytext=(0, 6 + top_offset),
            textcoords="offset points",
            fontsize=8.5,
            color=TEXT_MUTED,
            va="bottom",
            ha="left",
        )


def _bottom_legend(ax: Axes, handles: Sequence[object], columns: int | None = None) -> None:
    """Leyenda bajo el gráfico, centrada en la figura (solo con dos o más series)."""
    if len(handles) < 2:
        return
    ax.figure.legend(
        handles=handles,
        loc="outside lower center",
        ncol=columns or len(handles),
        handlelength=1.6,
        columnspacing=1.4,
        fontsize=8.5,
    )


def _status_handles(statuses: Sequence[Status], thresholds: Thresholds) -> list[Patch]:
    labels = {
        Status.GREEN: f"Verde (desde {format_pct(thresholds.green, 0)})",
        Status.YELLOW: f"Amarillo (desde {format_pct(thresholds.yellow, 0)})",
        Status.RED: f"Rojo (bajo {format_pct(thresholds.yellow, 0)})",
    }
    return [Patch(color=STATUS_COLORS[s], label=labels[s]) for s in _EVALUATED if s in statuses]


def _projection_handle() -> Line2D:
    return Line2D(
        [], [], color=TEXT, marker="|", markersize=11, markeredgewidth=2, linestyle="", label="Proyección al cierre"
    )


def bar_height(count: int, axes_px: float | None = None) -> float:
    """Grosor de barra en unidades de fila: con pocas filas se adelgaza para no pintar bloques.

    Con la altura del eje en píxeles, además, ninguna barra supera `MAX_BAR_PX`.
    """
    height = BAR_HEIGHT if count >= 10 else BAR_HEIGHT * max(count, 3) / 10
    if axes_px:
        height = min(height, MAX_BAR_PX * max(count, 1) / axes_px)
    return height


def axes_height_px(figure: Figure) -> float:
    """Altura aproximada del área de datos: la figura menos el título, el eje x y la leyenda."""
    return max(120.0, figure.get_figheight() * figure.dpi - 170)


def _compliance_bars(
    ax: Axes,
    results: Sequence[IndicatorResult],
    labels: Sequence[str],
    thresholds: Thresholds,
    value_labels: Sequence[str],
    ticks: Sequence[float] = (0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5),
) -> None:
    """Barras horizontales de cumplimiento con la línea de 100 %, la proyección y una columna de valores."""
    count = len(results)
    positions = np.arange(count)
    height = bar_height(count, axes_height_px(ax.figure))
    for i, result in enumerate(results):
        if result.compliance is not None and result.status in _EVALUATED:
            ax.barh(i, min(result.compliance, DISPLAY_CAP), height=height, color=STATUS_COLORS[result.status])
        else:
            ax.text(
                0.012,
                i,
                STATUS_SHORT[result.status],
                va="center",
                ha="left",
                fontsize=8,
                color=TEXT_MUTED,
                style="italic",
            )
        projected = result.projection.compliance
        if projected is not None and result.status in _EVALUATED:
            ax.plot(min(projected, DISPLAY_CAP), i, marker="|", markersize=11, markeredgewidth=2, color=TEXT)
        ax.text(
            1.01,
            i,
            value_labels[i],
            transform=ax.get_yaxis_transform(),
            va="center",
            ha="left",
            fontsize=8,
            color=TEXT,
        )
    ax.axvline(thresholds.green, color=TEXT, linewidth=1.1, zorder=1)
    ax.axvline(thresholds.yellow, color=TEXT_MUTED, linewidth=0.8, linestyle=(0, (2, 2)), zorder=1)
    ax.set_yticks(positions, labels)
    ax.set_ylim(count - 0.4, -0.6)
    ax.set_xlim(0, DISPLAY_CAP + 0.05)
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_major_formatter(PCT)
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=8.5, labelcolor=TEXT)


def draw_compliance(
    figure: Figure, results: Sequence[IndicatorResult], thresholds: Thresholds, scope: str
) -> tuple[str, ...]:
    """Cumplimiento a la fecha por indicador coloreado por semáforo. Devuelve los códigos por fila."""
    ax = figure.add_subplot()
    labels = [indicator_label(r.indicator) for r in results]
    values = [format_pct(r.compliance) if r.compliance is not None else "" for r in results]
    _compliance_bars(ax, results, labels, thresholds, values)
    for i in range(1, len(results)):
        if results[i].program_code != results[i - 1].program_code:
            ax.axhline(i - 0.5, color=BORDER, linewidth=1)
    ax.set_xlabel("Cumplimiento respecto de la meta a la fecha")
    heading(ax, "Cumplimiento a la fecha por indicador", f"{scope}. Marca negra: cumplimiento proyectado.")
    statuses = [r.status for r in results]
    handles: list[object] = [*_status_handles(statuses, thresholds), _projection_handle()]
    _bottom_legend(ax, handles)
    return tuple(r.indicator_code for r in results)


def draw_site_comparison(
    figure: Figure, results: Sequence[IndicatorResult], thresholds: Thresholds, selected_site: str | None
) -> tuple[str | None, ...]:
    """Cumplimiento del indicador en la red y en cada sede, con la misma regla y escala."""
    ax = figure.add_subplot()
    scale = results[0].indicator.scale
    labels = [r.site_name for r in results]
    values = [
        f"{format_pct(r.compliance)}  (valor {format_value(r.value, scale)})" if r.compliance is not None else ""
        for r in results
    ]
    _compliance_bars(ax, results, labels, thresholds, values, ticks=(0, 0.5, 1.0, 1.5))
    ax.axhline(0.5, color=BORDER, linewidth=1)
    for tick, result in zip(ax.get_yticklabels(), results, strict=True):
        if result.site_code == selected_site:
            tick.set_fontweight("bold")
    ax.set_xlabel("Cumplimiento respecto de la meta a la fecha")
    ind = results[0].indicator
    heading(ax, "Cumplimiento por sede", f"{ind.code} {ind.short_name}, {results[0].period.label}")
    handles: list[object] = [*_status_handles([r.status for r in results], thresholds), _projection_handle()]
    _bottom_legend(ax, handles, columns=2)
    return tuple(r.site_code for r in results)


def spread_labels(values: Sequence[float], min_gap: float) -> list[float]:
    """Separa verticalmente etiquetas que chocan, conservando su orden y su centro.

    Devuelve las posiciones ajustadas en el mismo orden de entrada.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    placed = [values[i] for i in order]
    for k in range(1, len(placed)):
        placed[k] = max(placed[k], placed[k - 1] + min_gap)
    shift = (sum(placed) - sum(values[i] for i in order)) / max(1, len(placed))
    adjusted = [0.0] * len(values)
    for k, i in enumerate(order):
        adjusted[i] = placed[k] - shift
    return adjusted


def _percent_step(span: float) -> float:
    if span <= 0.12:
        return 0.02
    if span <= 0.4:
        return 0.05
    return 0.1


def draw_index_history(figure: Figure, series: Sequence[ProgramSeries], period: Period, thresholds: Thresholds) -> None:
    """Índice ponderado mes a mes por programa, con el cierre proyectado en línea punteada."""
    ax = figure.add_subplot()
    months = np.arange(1, period.month + 1)
    lowest = thresholds.yellow
    labels: list[tuple[float, float, str]] = []
    for item in series:
        values = np.array([np.nan if v is None else v for v in item.values], dtype=float)
        ax.plot(
            months,
            values,
            color=item.color,
            marker="o",
            markersize=5,
            markeredgecolor=SURFACE,
            markeredgewidth=1.2,
            label=item.name,
            zorder=3,
        )
        finite = values[~np.isnan(values)]
        if finite.size:
            lowest = min(lowest, float(finite.min()))
        last = next(((m, v) for m, v in zip(months[::-1], values[::-1], strict=True) if not np.isnan(v)), None)
        if last is None:
            continue
        end_x, end_value = float(last[0]), float(last[1])
        if item.projected is not None and period.month < 12:
            ax.plot([end_x, 12], [end_value, item.projected], color=item.color, linestyle=(0, (1.5, 1.8)), zorder=3)
            ax.plot(
                12,
                item.projected,
                marker="o",
                markersize=6,
                markerfacecolor=SURFACE,
                markeredgecolor=item.color,
                markeredgewidth=1.8,
                zorder=4,
            )
            end_x, end_value = 12.0, item.projected
            lowest = min(lowest, end_value)
        labels.append((end_x, end_value, f"{item.code} {format_pct(end_value)}"))
    bottom = max(0.0, math.floor((lowest - 0.03) * 50) / 50)
    top = 1.04
    ax.axhline(1.0, color=TEXT, linewidth=1, zorder=2)
    ax.set_xlim(0.6, 12.4)
    ax.set_xticks(range(1, 13), MONTHS_SHORT)
    ax.set_ylim(bottom, top)
    ax.yaxis.set_major_locator(MultipleLocator(_percent_step(top - bottom)))
    ax.yaxis.set_major_formatter(PCT)
    ax.set_ylabel("Índice ponderado")
    axes_px = max(120.0, figure.get_figheight() * figure.dpi * 0.62)
    gap = (top - bottom) * 13 / axes_px
    positions = spread_labels([value for _x, value, _text in labels], gap)
    for (x, value, text), y in zip(labels, positions, strict=True):
        ax.annotate(
            text,
            xy=(x, value),
            xytext=(12.55, y),
            textcoords="data",
            va="center",
            ha="left",
            fontsize=8.5,
            color=TEXT,
            annotation_clip=False,
        )
    heading(ax, "Índice ponderado mes a mes", "Continua: a la fecha. Punteada: cierre proyectado.")
    if len(series) > 1:
        handles = [
            Line2D([], [], color=item.color, marker="o", markersize=5, label=f"{item.code}: {item.name}")
            for item in series
        ]
        _bottom_legend(ax, handles, columns=1)


def _progress_top(result: IndicatorResult) -> float:
    """Límite superior del eje con aire sobre la serie, la meta y la banda de proyección."""
    candidates = [0.0]
    for p in result.monthly:
        candidates.extend(v for v in (p.monthly_value, p.cumulative_value, p.goal_to_date) if v is not None)
    projection = result.projection
    candidates.extend(v for v in (projection.value, projection.p90) if v is not None)
    candidates.extend(v for point in projection.path for v in (point.p90, point.central) if v is not None)
    top = max(candidates)
    return top * 1.15 if top > 0 else 1.0


def _goal_series(result: IndicatorResult) -> list[float]:
    return [np.nan if p.goal_to_date is None else p.goal_to_date for p in result.monthly]


def draw_progress(figure: Figure, result: IndicatorResult) -> None:
    """Avance mensual y acumulado del indicador en un nivel, con meta, proyección y banda p10-p90."""
    ax = figure.add_subplot()
    ind = result.indicator
    scale = ind.scale
    cut = result.period.month
    points = result.monthly
    handles: list[object] = []

    for cut_result in result.cuts:
        ax.axvline(cut_result.month, color=BORDER, linewidth=1, zorder=1)
        ax.plot(
            cut_result.month,
            1.0,
            marker="s",
            markersize=7,
            color=STATUS_COLORS[cut_result.status],
            markeredgecolor=SURFACE,
            transform=ax.get_xaxis_transform(),
            clip_on=False,
            zorder=6,
        )
    if result.cuts:
        handles.append(
            Line2D([], [], color=TEXT_MUTED, marker="s", markersize=7, linestyle="", label="Corte (color del estado)")
        )

    bar_months = [p.month for p in points if p.month <= cut and p.monthly_value is not None]
    bar_values = [p.monthly_value for p in points if p.month <= cut and p.monthly_value is not None]
    if bar_months:
        ax.bar(bar_months, bar_values, width=0.55, color=MONTHLY_BAR, zorder=2)
        label = "Aporte del mes" if ind.grows_with_time else "Valor del mes"
        handles.append(Patch(color=MONTHLY_BAR, label=label))

    missing = [p.month for p in points if p.month <= cut and p.expected and p.reported is not True]
    if missing:
        ax.plot(
            missing,
            [0] * len(missing),
            linestyle="",
            marker="x",
            markersize=7,
            markeredgewidth=1.6,
            color=TEXT,
            zorder=4,
            clip_on=False,
        )
        handles.append(
            Line2D(
                [],
                [],
                linestyle="",
                marker="x",
                markersize=7,
                markeredgewidth=1.6,
                color=TEXT,
                label="Mes no informado",
            )
        )

    cumulative = np.array([np.nan if p.cumulative_value is None else p.cumulative_value for p in points[:cut]])
    months = np.arange(1, cut + 1)
    ax.plot(
        months,
        cumulative,
        color=PRIMARY,
        marker="o",
        markersize=5,
        markeredgecolor=SURFACE,
        markeredgewidth=1.2,
        zorder=5,
    )
    handles.append(Line2D([], [], color=PRIMARY, marker="o", markersize=5, label="Acumulado del año"))

    goals = _goal_series(result)
    goal = result.goal
    if goal.determinable and goal.value is not None:
        annual = format_value(goal.value, scale)
        label = f"Meta a la fecha (anual {annual})" if ind.grows_with_time else f"Meta anual ({annual})"
        ax.plot(range(1, 13), goals, color=TEXT_MUTED, linestyle=(0, (4, 3)), linewidth=1.4, zorder=3)
        handles.append(Line2D([], [], color=TEXT_MUTED, linestyle=(0, (4, 3)), linewidth=1.4, label=label))

    projection = result.projection
    last_value = next((v for v in cumulative[::-1] if not np.isnan(v)), None)
    if projection.path and last_value is not None:
        xs = [cut, *(p.month for p in projection.path)]
        central = [last_value, *(np.nan if p.central is None else p.central for p in projection.path)]
        low = [last_value, *(np.nan if p.p10 is None else p.p10 for p in projection.path)]
        high = [last_value, *(np.nan if p.p90 is None else p.p90 for p in projection.path)]
        ax.fill_between(xs, low, high, color=BAND_COLOR, alpha=0.14, linewidth=0, zorder=2)
        ax.plot(xs, central, color=ACCENT, linestyle=(0, (1.5, 1.8)), linewidth=2, zorder=5)
        handles.append(Line2D([], [], color=ACCENT, linestyle=(0, (1.5, 1.8)), linewidth=2, label="Proyección central"))
        handles.append(Patch(color=BAND_COLOR, alpha=0.2, label="Rango p10 a p90"))
    elif projection.value is not None and last_value is not None and cut < 12:
        ax.plot(
            [cut, 12], [last_value, projection.value], color=ACCENT, linestyle=(0, (1.5, 1.8)), linewidth=2, zorder=5
        )
        handles.append(
            Line2D([], [], color=ACCENT, linestyle=(0, (1.5, 1.8)), linewidth=2, label=projection.method.label)
        )
    if projection.value is not None and cut < 12:
        ax.plot(
            12,
            projection.value,
            marker="o",
            markersize=6,
            markerfacecolor=SURFACE,
            markeredgecolor=ACCENT,
            markeredgewidth=1.8,
            zorder=6,
        )
        ax.annotate(
            f"Cierre {format_value(projection.value, scale)}",
            xy=(12, projection.value),
            xytext=(8, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=TEXT,
        )

    ax.set_xlim(0.4, 12.6)
    ax.set_xticks(range(1, 13), MONTHS_SHORT)
    ax.set_ylim(0, _progress_top(result))
    ax.yaxis.set_major_locator(MaxNLocator(6))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: axis_value(value, scale)))
    ax.set_ylabel(axis_label(scale))
    subtitle = result.site_name
    if goal.is_baseline:
        subtitle += ". Año de línea base: se mide sin meta."
    elif not goal.determinable:
        subtitle += ". Meta no determinable."
    heading(ax, f"{ind.code} {ind.name}", subtitle)
    _bottom_legend(ax, handles, columns=min(len(handles), 4))


def _heat_text_color(value: float, cmap: LinearSegmentedColormap, norm: TwoSlopeNorm) -> str:
    red, green, blue = to_rgb(cmap(norm(value)))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return SURFACE if luminance < 0.5 else TEXT


def heatmap_colormap() -> tuple[LinearSegmentedColormap, TwoSlopeNorm]:
    cmap = LinearSegmentedColormap.from_list("cumplimiento", HEAT_COLORS).with_extremes(bad=HEAT_EMPTY)
    return cmap, TwoSlopeNorm(vmin=0.5, vcenter=1.0, vmax=1.5)


def draw_heatmap(
    figure: Figure,
    matrix: StatusMatrix,
    selected_indicator: str | None,
    selected_site: str | None,
) -> HeatmapLayout:
    """Mapa de calor indicador × nivel con el cumplimiento a la fecha centrado en 100 %."""
    ax = figure.add_subplot()
    indicators = matrix.indicators
    levels = matrix.levels
    values = np.full((len(indicators), len(levels)), np.nan)
    for i, ind in enumerate(indicators):
        for j, (site_code, _name) in enumerate(levels):
            compliance = matrix.cell(ind.code, site_code).compliance
            if compliance is not None:
                values[i, j] = compliance
    cmap, norm = heatmap_colormap()
    image = ax.imshow(np.ma.masked_invalid(values), aspect="auto", cmap=cmap, norm=norm, interpolation="nearest")
    if len(indicators) < 12:
        # Con pocas filas se limita la altura de las celdas para no dibujar bloques enormes.
        ax.set_box_aspect(max(len(indicators), 1) * 0.5 / max(len(levels), 1))
        ax.set_anchor("N")
    small = len(indicators) > 16
    for i, ind in enumerate(indicators):
        for j, (site_code, _name) in enumerate(levels):
            result = matrix.cell(ind.code, site_code)
            value = values[i, j]
            if np.isnan(value):
                label = HEAT_LABELS.get(result.status, cell_text(result))
                ax.text(j, i, label, ha="center", va="center", fontsize=7.5, color=TEXT_MUTED, style="italic")
            else:
                ax.text(
                    j,
                    i,
                    cell_text(result),
                    ha="center",
                    va="center",
                    fontsize=7.5 if small else 8.5,
                    color=_heat_text_color(value, cmap, norm),
                )
    ax.set_xticks(range(len(levels)), [name.replace("Sede ", "") for _code, name in levels])
    ax.set_yticks(range(len(indicators)), [indicator_label(ind) for ind in indicators])
    ax.xaxis.tick_top()
    ax.tick_params(axis="both", labelsize=8.5, labelcolor=TEXT)
    ax.set_xticks(np.arange(-0.5, len(levels), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(indicators), 1), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.grid(which="major", visible=False)
    ax.tick_params(which="minor", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for j, tick in enumerate(ax.get_xticklabels()):
        if levels[j][0] == selected_site:
            tick.set_fontweight("bold")
    codes = tuple(ind.code for ind in indicators)
    if selected_indicator in codes:
        row = codes.index(selected_indicator)
        ax.add_patch(
            Rectangle((-0.5, row - 0.5), len(levels), 1, fill=False, edgecolor=TEXT, linewidth=1.4, clip_on=False)
        )
    # La escala va pegada al mapa (eje interior), de modo que lo acompaña aunque el mapa tenga pocas filas.
    scale_axes = ax.inset_axes(
        (1.04, 0.1 if len(indicators) >= 12 else 0.0, 0.035, 0.8 if len(indicators) >= 12 else 1.0)
    )
    colorbar = figure.colorbar(image, cax=scale_axes, format=PCT)
    # Con pocas filas la escala es corta: tres marcas bastan y no se amontonan.
    colorbar.set_ticks([0.5, 0.75, 1.0, 1.25, 1.5] if len(indicators) >= 6 else [0.5, 1.0, 1.5])
    colorbar.ax.tick_params(labelsize=8)
    colorbar.outline.set_visible(False)
    colorbar.set_label("Cumplimiento a la fecha", fontsize=8.5, color=TEXT_MUTED)
    heading(
        ax,
        "Mapa de cumplimiento por indicador y sede",
        "Gris: en la meta. Doble clic en una celda para ver su avance.",
        top_offset=16,
    )
    return HeatmapLayout(codes, tuple(code for code, _name in levels))
