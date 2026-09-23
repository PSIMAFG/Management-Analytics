"""Gráficos de la aplicación dibujados con matplotlib sobre una figura dada.

Criterios comunes: un solo eje vertical por gráfico (nunca dos escalas),
montos en millones de pesos con formato chileno, colores fijos por entidad,
leyenda solo cuando hay más de una serie y rótulos directos selectivos.
Cada función recibe `hover` para registrar el texto que se muestra al pasar
el mouse sobre una barra o un punto.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator

from staffing_simulator.ui.formatting import (
    MILLION,
    format_clp,
    format_decimal,
    format_mm,
    format_pct,
    format_signed_clp,
)
from staffing_simulator.ui.style import (
    AXIS,
    BAD,
    GOOD,
    INK,
    INK_SECONDARY,
    SERIES,
    SURFACE,
    TEXT,
    TEXT_MUTED,
)

AXIS_INK = AXIS

Hover = Callable[[Artist, str | Sequence[str]], None]

BAR_WIDTH = 0.5
BAR_HEIGHT = 0.5
GAP = 1.2
MONEY_AXIS = "Millones de pesos"


def _no_hover(_artist: Artist, _text: str | Sequence[str]) -> None:
    return None


def millions_formatter(max_value: float) -> FuncFormatter:
    """Marcas del eje en millones: sin decimales para montos grandes, uno para montos chicos."""
    decimals = 0 if max_value >= 20 * MILLION else 1

    def label(value: float, _position: int) -> str:
        return format_decimal(value / MILLION, decimals)

    return FuncFormatter(label)


def _money_axis(ax: Axes, max_value: float, axis: str = "y") -> None:
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_locator(MaxNLocator(nbins=6, min_n_ticks=3))
    target.set_major_formatter(millions_formatter(max_value))


def _wrap(label: str, width: int = 22) -> str:
    return "\n".join(textwrap.wrap(label, width)) or label


@dataclass(frozen=True)
class Series:
    """Serie con nombre, color e importes por mes."""

    label: str
    color: str
    values: Sequence[int]


def draw_monthly(
    figure: Figure,
    *,
    months: Sequence[str],
    series: Sequence[Series],
    cumulative: Sequence[int],
    budget_total: int | None,
    hover: Hover = _no_hover,
) -> None:
    """Barras apiladas del costo mensual por tipo de contrato y, debajo, el acumulado del año."""
    grid = figure.add_gridspec(2, 1, height_ratios=(3, 2))
    top = figure.add_subplot(grid[0])
    bottom = figure.add_subplot(grid[1], sharex=top)
    _draw_monthly_bars(top, months, series, hover)
    _draw_cumulative(bottom, months, cumulative, budget_total, hover)


def _draw_monthly_bars(ax: Axes, months: Sequence[str], series: Sequence[Series], hover: Hover) -> None:
    positions = list(range(len(months)))
    totals = [sum(item.values[index] for item in series) for index in positions]
    base = [0] * len(months)
    for item in series:
        bars = ax.bar(
            positions,
            item.values,
            BAR_WIDTH,
            bottom=base,
            color=item.color,
            edgecolor=SURFACE,
            linewidth=GAP,
            label=item.label,
            zorder=2,
        )
        for index, bar in enumerate(bars):
            if item.values[index]:
                hover(
                    bar,
                    f"{months[index]}, {item.label}: {format_clp(item.values[index])}\n"
                    f"Total del mes: {format_clp(totals[index])}",
                )
        base = [value + extra for value, extra in zip(base, item.values, strict=True)]

    peak = max(totals, default=0)
    ax.set_title("Costo mensual por tipo de contrato")
    ax.set_ylabel(MONEY_AXIS)
    ax.set_ylim(0, peak * 1.28 if peak else 1)
    _money_axis(ax, peak)
    if peak:
        ax.annotate(
            f"Máximo: {format_mm(peak)}",
            (totals.index(peak), peak),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=TEXT,
        )
    if len(series) > 1:
        ax.legend(loc="upper left", ncols=min(len(series), 4), bbox_to_anchor=(0, 1.0), borderaxespad=0.2)
    ax.tick_params(axis="x", labelbottom=True)


def _draw_cumulative(
    ax: Axes, months: Sequence[str], cumulative: Sequence[int], budget_total: int | None, hover: Hover
) -> None:
    positions = list(range(len(months)))
    ax.fill_between(positions, cumulative, color=INK, alpha=0.08, linewidth=0, zorder=1)
    (line,) = ax.plot(
        positions,
        cumulative,
        color=INK,
        marker="o",
        markersize=5,
        markeredgecolor=SURFACE,
        markeredgewidth=1.5,
        zorder=3,
        label="Acumulado proyectado",
    )
    hover(line, [f"Acumulado a {month}: {format_clp(value)}" for month, value in zip(months, cumulative, strict=True)])
    upper = max([*cumulative, budget_total or 0, 1])
    if budget_total:
        ax.axhline(budget_total, color=INK_SECONDARY, linewidth=1.2, zorder=2)
        ax.annotate(
            f"Presupuesto anual: {format_mm(budget_total)}",
            (0, budget_total),
            xytext=(0, 4),
            textcoords="offset points",
            ha="left",
            va="bottom",
            fontsize=8.5,
            color=INK_SECONDARY,
        )
    if cumulative:
        ax.annotate(
            format_mm(cumulative[-1]),
            (positions[-1], cumulative[-1]),
            xytext=(0, -12 if budget_total and cumulative[-1] > budget_total * 0.93 else 8),
            textcoords="offset points",
            ha="center",
            va="center",
            fontsize=8.5,
            fontweight="bold",
            color=TEXT,
        )
    ax.set_title("Costo acumulado del año")
    ax.set_ylabel(MONEY_AXIS)
    ax.set_ylim(0, upper * 1.18)
    _money_axis(ax, upper)
    ax.set_xticks(positions, months)
    ax.set_xlim(-0.6, len(months) - 0.4)


@dataclass(frozen=True)
class BarItem:
    """Barra horizontal con su rótulo, monto, participación y color."""

    label: str
    value: int
    share: float | None
    color: str


@dataclass(frozen=True)
class BarPanel:
    title: str
    items: Sequence[BarItem]


def _barh_panel(ax: Axes, panel: BarPanel, max_value: int, hover: Hover, slots: int) -> None:
    items = list(panel.items)
    positions = list(range(len(items)))
    bars = ax.barh(
        positions,
        [item.value for item in items],
        BAR_HEIGHT,
        color=[item.color for item in items],
        edgecolor=SURFACE,
        linewidth=GAP,
        zorder=2,
    )
    for bar, item in zip(bars, items, strict=True):
        share = "" if item.share is None else f" ({format_pct(item.share)})"
        hover(bar, f"{item.label}: {format_clp(item.value)}{share}")
        ax.annotate(
            f"{format_mm(item.value)}{share}",
            (item.value, bar.get_y() + bar.get_height() / 2),
            xytext=(4, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=8,
            color=TEXT,
        )
    ax.set_yticks(positions, [_wrap(item.label, 16) for item in items])
    ax.set_ylim(max(slots, len(items)) - 0.5, -0.5)
    ax.set_xlim(0, max_value * 1.7 if max_value else 1)
    ax.set_title(panel.title)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="x", labelbottom=False)
    ax.tick_params(axis="y", labelsize=8.5)


def draw_breakdown(figure: Figure, panels: Sequence[BarPanel], hover: Hover = _no_hover) -> None:
    """Paneles de barras horizontales (uno por dimensión), con monto en millones y participación."""
    columns = 2
    rows = (len(panels) + 1) // columns
    slots = [
        max((len(panel.items) for panel in panels[row * columns : (row + 1) * columns]), default=1)
        for row in range(rows)
    ]
    grid = figure.add_gridspec(rows, columns, height_ratios=[count + 1.6 for count in slots])
    for index, panel in enumerate(panels):
        ax = figure.add_subplot(grid[index // columns, index % columns])
        max_value = max((item.value for item in panel.items), default=0)
        if not panel.items:
            ax.axis("off")
            ax.set_title(panel.title)
            continue
        _barh_panel(ax, panel, max_value, hover, slots[index // columns])
    figure.suptitle(
        "Costo anual en millones de pesos y participación en el total", x=0.01, ha="left", fontsize=9, color=TEXT_MUTED
    )


@dataclass(frozen=True)
class ScenarioBar:
    name: str
    color: str
    total: int
    difference: int
    share: float | None
    is_base: bool


@dataclass(frozen=True)
class DifferenceGroup:
    """Diferencia anual contra el base de un concepto (por ejemplo, un tipo de contrato), por escenario."""

    label: str
    values: Sequence[int]


def _signed_mm(value: int) -> str:
    text = format_mm(value)
    return text if value <= 0 or text.startswith("-") else f"+{text}"


def draw_comparison(
    figure: Figure,
    *,
    bars: Sequence[ScenarioBar],
    months: Sequence[str],
    lines: Sequence[Series],
    cumulative: bool,
    differences: Sequence[DifferenceGroup] = (),
    difference_series: Sequence[Series] = (),
    hover: Hover = _no_hover,
) -> None:
    """Costo anual por escenario, diferencia contra el base por tipo de contrato y evolución por mes."""
    grid = figure.add_gridspec(2, 2, width_ratios=(2, 3), height_ratios=(2, 3))
    top_left = figure.add_subplot(grid[0, 0])
    bottom_left = figure.add_subplot(grid[1, 0])
    right = figure.add_subplot(grid[:, 1])
    _draw_totals(top_left, bars, hover)
    _draw_differences(bottom_left, differences, difference_series, hover)
    _draw_lines(right, months, lines, cumulative, hover)


def _draw_totals(ax: Axes, bars: Sequence[ScenarioBar], hover: Hover) -> None:
    positions = list(range(len(bars)))
    rects = ax.barh(
        positions,
        [item.total for item in bars],
        BAR_HEIGHT,
        color=[item.color for item in bars],
        edgecolor=SURFACE,
        linewidth=GAP,
        zorder=2,
    )
    max_total = max((item.total for item in bars), default=0)
    for rect, item in zip(rects, bars, strict=True):
        if item.is_base:
            note = "base"
        else:
            share = "-" if item.share is None else format_pct(item.share)
            note = share if share.startswith("-") else f"+{share}"
        hover(
            rect,
            f"{item.name}: {format_clp(item.total)}\nDiferencia con el base: {format_signed_clp(item.difference)}",
        )
        ax.annotate(
            f"{format_mm(item.total)} ({note})",
            (item.total, rect.get_y() + rect.get_height() / 2),
            xytext=(4, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=8.5,
            color=TEXT,
        )
    ax.set_yticks(positions, [_wrap(item.name, 20) for item in bars])
    ax.set_ylim(max(len(bars), 3) - 0.5, -0.5)
    ax.set_xlim(0, max_total * 1.45 if max_total else 1)
    ax.set_title("Costo anual por escenario (millones de pesos)")
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="x", labelbottom=False)


def _draw_differences(ax: Axes, groups: Sequence[DifferenceGroup], series: Sequence[Series], hover: Hover) -> None:
    # Título corto: la columna izquierda es angosta a 1366 px y un título largo invade el gráfico vecino.
    ax.set_title("Diferencia con el base (millones de pesos)")
    visible = [group for group in groups if any(group.values)]
    if not visible or not series:
        ax.axis("off")
        ax.text(0.5, 0.5, "Sin diferencias con el escenario base", ha="center", va="center", color=TEXT_MUTED)
        return
    count = len(series)
    height = min(0.7 / count, 0.36)
    extreme = max(abs(value) for group in visible for value in group.values) or 1
    for index, item in enumerate(series):
        offsets = [row + (index - (count - 1) / 2) * height for row in range(len(visible))]
        values = [group.values[index] for group in visible]
        rects = ax.barh(offsets, values, height, color=item.color, edgecolor=SURFACE, linewidth=GAP, zorder=2)
        for rect, group, value in zip(rects, visible, values, strict=True):
            hover(rect, f"{group.label}, {item.label}: {format_signed_clp(value)}")
            if value:
                ax.annotate(
                    _signed_mm(value),
                    (value, rect.get_y() + rect.get_height() / 2),
                    xytext=(4 if value > 0 else -4, 0),
                    textcoords="offset points",
                    ha="left" if value > 0 else "right",
                    va="center",
                    fontsize=8,
                    color=TEXT,
                )
    ax.axvline(0, color=AXIS_INK, linewidth=1, zorder=3)
    ax.set_yticks(list(range(len(visible))), [_wrap(group.label, 20) for group in visible])
    ax.set_ylim(max(len(visible), 3) - 0.5, -0.5)
    ax.set_xlim(-extreme * 1.6, extreme * 1.6)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="x", labelbottom=False)
    if count > 1:
        ax.legend(
            handles=[Patch(color=item.color, label=item.label) for item in series],
            loc="lower left",
            ncols=1,
            fontsize=8,
        )


def _draw_lines(ax: Axes, months: Sequence[str], lines: Sequence[Series], cumulative: bool, hover: Hover) -> None:
    x = list(range(len(months)))
    peak = 0
    low = None
    for item in lines:
        (line,) = ax.plot(x, item.values, color=item.color, label=item.label, zorder=3)
        ax.plot(
            x[-1:],
            item.values[-1:],
            "o",
            color=item.color,
            markersize=6,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
            zorder=4,
        )
        hover(
            line,
            [f"{item.label}, {month}: {format_clp(value)}" for month, value in zip(months, item.values, strict=True)],
        )
        peak = max(peak, *item.values)
        low = min(item.values) if low is None else min(low, *item.values)
    ax.set_title("Costo acumulado por escenario" if cumulative else "Costo mensual por escenario")
    ax.set_ylabel(MONEY_AXIS)
    ax.set_xticks(x, months)
    if cumulative or not peak:
        ax.set_ylim(0, peak * 1.12 if peak else 1)
    else:
        ax.set_ylim(max(0, (low or 0) * 0.85), peak * 1.08)
    _money_axis(ax, peak)
    if len(lines) > 1:
        ax.legend(loc="upper left", ncols=1)


@dataclass(frozen=True)
class BudgetBar:
    label: str
    budget: int | None
    projected: int


def draw_budget(
    figure: Figure,
    *,
    programs: Sequence[BudgetBar],
    months: Sequence[str],
    projected: Sequence[int],
    executed: Sequence[int | None],
    scope: str,
    comparable: Sequence[int | None] | None = None,
    coverage: Sequence[str | None] | None = None,
    hover: Hover = _no_hover,
) -> None:
    """Proyectado contra presupuesto por programa y proyectado contra ejecutado por mes.

    `comparable` es la proyección de los programas con ejecución en cada mes y
    `coverage` describe los meses con carga parcial (por ejemplo, «1 de 4
    programas»); esos meses se marcan aparte para no leerlos como un gasto bajo.
    """
    grid = figure.add_gridspec(1, 2, width_ratios=(1, 1))
    _draw_budget_bars(figure.add_subplot(grid[0]), programs, hover)
    months_count = len(months)
    _draw_execution_lines(
        figure.add_subplot(grid[1]),
        months,
        projected,
        executed,
        comparable if comparable is not None else [None] * months_count,
        coverage if coverage is not None else [None] * months_count,
        scope,
        hover,
    )


def _draw_budget_bars(ax: Axes, programs: Sequence[BudgetBar], hover: Hover) -> None:
    positions = list(range(len(programs)))
    rects = ax.barh(positions, [item.projected for item in programs], 0.36, color=INK, zorder=2)
    upper = max([item.projected for item in programs] + [item.budget or 0 for item in programs] + [1])
    for position, rect, item in zip(positions, rects, programs, strict=True):
        if item.budget is not None:
            ax.plot(
                [item.budget, item.budget],
                [position - 0.36, position + 0.36],
                color=INK_SECONDARY,
                linewidth=2.5,
                solid_capstyle="butt",
                zorder=3,
            )
            balance = item.budget - item.projected
            word = "Holgura" if balance >= 0 else "Déficit"
            ax.annotate(
                f"{word} {format_mm(abs(balance))}",
                (max(item.budget, item.projected), position),
                xytext=(6, 0),
                textcoords="offset points",
                ha="left",
                va="center",
                fontsize=8.5,
                color=GOOD if balance >= 0 else BAD,
                fontweight="bold",
            )
            hover(
                rect,
                f"{item.label}\nProyectado: {format_clp(item.projected)}\nPresupuesto: {format_clp(item.budget)}\n"
                f"Saldo: {format_clp(balance)}",
            )
        else:
            ax.annotate(
                "Sin presupuesto",
                (item.projected, position),
                xytext=(6, 0),
                textcoords="offset points",
                ha="left",
                va="center",
                fontsize=8.5,
                color=TEXT_MUTED,
            )
            hover(rect, f"{item.label}\nProyectado: {format_clp(item.projected)}\nSin presupuesto cargado")
    ax.set_yticks(positions, [_wrap(item.label, 16) for item in programs])
    ax.set_ylim(max(len(programs), 4) - 0.5, -0.5)
    ax.set_xlim(0, upper * 1.4)
    ax.set_title("Proyectado y presupuesto por programa")
    ax.set_xlabel(MONEY_AXIS)
    ax.grid(True, axis="x")
    ax.grid(False, axis="y")
    _money_axis(ax, upper, axis="x")
    ax.legend(
        handles=[
            Patch(color=INK, label="Costo proyectado"),
            Line2D([], [], color=INK_SECONDARY, linewidth=2.5, label="Presupuesto"),
        ],
        loc="lower right",
        ncols=1,
    )


def _execution_hover(month: str, value: int, reference: int, coverage: str | None) -> str:
    if coverage is None:
        return (
            f"{month}\nEjecutado: {format_clp(value)}\nProyectado: {format_clp(reference)}\n"
            f"Diferencia: {format_signed_clp(value - reference)}"
        )
    return (
        f"{month}: ejecución de {coverage}\nEjecutado: {format_clp(value)}\n"
        f"Proyectado de esos programas: {format_clp(reference)}\nDiferencia: {format_signed_clp(value - reference)}"
    )


def _draw_execution_lines(
    ax: Axes,
    months: Sequence[str],
    projected: Sequence[int],
    executed: Sequence[int | None],
    comparable: Sequence[int | None],
    coverage: Sequence[str | None],
    scope: str,
    hover: Hover,
) -> None:
    x = list(range(len(months)))
    (projected_line,) = ax.plot(x, projected, color=SERIES[0], label="Proyectado", zorder=3)
    hover(
        projected_line,
        [f"{month}, proyectado: {format_clp(value)}" for month, value in zip(months, projected, strict=True)],
    )
    points = [(index, value) for index, value in enumerate(executed) if value is not None]
    complete = [(index, value) for index, value in points if coverage[index] is None]
    partial = [(index, value) for index, value in points if coverage[index] is not None]

    def reference(index: int) -> int:
        value = comparable[index]
        return projected[index] if value is None else value

    if complete:
        (executed_line,) = ax.plot(
            [index for index, _value in complete],
            [value for _index, value in complete],
            color=SERIES[1],
            marker="o",
            markersize=5,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
            label="Ejecutado",
            zorder=4,
        )
        hover(executed_line, [_execution_hover(months[i], v, reference(i), None) for i, v in complete])
    if partial:
        (partial_marks,) = ax.plot(
            [index for index, _value in partial],
            [value for _index, value in partial],
            linestyle="none",
            marker="o",
            markersize=6,
            markerfacecolor=SURFACE,
            markeredgecolor=SERIES[1],
            markeredgewidth=1.5,
            label="Ejecutado (carga parcial)",
            zorder=4,
        )
        hover(partial_marks, [_execution_hover(months[i], v, reference(i), coverage[i]) for i, v in partial])
    if points:
        ax.legend(loc="lower right", ncols=3 if partial else 2)
    else:
        ax.text(
            0.5,
            0.12,
            "Sin ejecución registrada para este año",
            transform=ax.transAxes,
            ha="center",
            color=TEXT_MUTED,
            fontsize=8.5,
        )
    peak = max([*projected, *(value for _index, value in points), 1])
    ax.set_title(f"Proyectado y ejecutado por mes: {scope}")
    ax.set_ylabel(MONEY_AXIS)
    ax.set_xticks(x, months)
    low = min([*projected, *(value for _index, value in points)], default=0)
    ax.set_ylim(max(0, low * 0.85), peak * 1.08)
    _money_axis(ax, peak)
