"""Reporte ejecutivo en PDF (4 páginas) generado con matplotlib, sin interfaz gráfica.

1. Portada y resumen: índices por programa, conteo de estados y alertas.
2. Matriz de semáforo indicador × sede.
3. Cumplimiento a la fecha de la red por indicador, con la proyección al cierre.
4. Comparación entre sedes: índice por programa y estados por sede.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter

from kpi_monitor.data.files import replace_on_success
from kpi_monitor.domain.enums import Status
from kpi_monitor.domain.progress import ProgressFn
from kpi_monitor.domain.reporting import ReportData
from kpi_monitor.domain.results import NETWORK_LABEL, IndicatorResult
from kpi_monitor.domain.text import format_int, format_pct
from kpi_monitor.errors import DataError
from kpi_monitor.palette import BORDER, PRIMARY, SERIES, STATUS_COLORS, TEXT, TEXT_MUTED

PAGE_SIZE = (11.69, 8.27)
DISPLAY_CAP = 1.6
MAX_COVER_ALERTS = 12

_STYLE = {
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.edgecolor": BORDER,
    "axes.labelcolor": TEXT_MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": TEXT_MUTED,
    "ytick.color": TEXT_MUTED,
    "legend.frameon": False,
    "pdf.fonttype": 42,
}
_PCT = FuncFormatter(lambda value, _pos: format_pct(value, 0))
_LIGHT_TEXT = {Status.GREEN, Status.RED, Status.YELLOW, Status.UNDETERMINED}


def _header(fig: Figure, report: ReportData, title: str) -> None:
    fig.text(0.04, 0.955, title, fontsize=15, fontweight="bold", color=PRIMARY)
    fig.text(0.04, 0.925, f"{report.organization}. Período de corte: {report.period.label}", color=TEXT_MUTED)


def _cover(report: ReportData) -> Figure:
    evaluation = report.evaluation
    catalog = report.catalog
    fig = Figure(figsize=PAGE_SIZE)
    _header(fig, report, "Reporte ejecutivo de indicadores")
    indexes = evaluation.indexes_for(None)

    lines = ["Índice ponderado de la red por programa"]
    for index in indexes:
        lines.append(
            f"  {catalog.program(index.program_code).name}: {format_pct(index.value)} a la fecha; "
            f"{format_pct(index.projected)} proyectado al cierre; "
            f"peso sin datos {format_pct(index.pct_weight_without_data)}"
        )
    counts = evaluation.status_counts(None)
    pending = counts.get(Status.PENDING)
    lines += [
        "",
        "Estado de los indicadores en la red",
        f"  Verdes: {counts.green}   Amarillos: {counts.yellow}   Rojos: {counts.red}   "
        f"Sin datos o sin meta: {counts.no_data}   Línea base: {counts.get(Status.BASELINE)}"
        + (f"   Pendientes del primer corte: {pending}" if pending else ""),
        "",
        "Alertas y calidad de datos",
        f"  Alertas del período: {format_int(len(evaluation.alerts))} "
        f"(nuevas respecto del mes anterior: {format_int(sum(1 for a in evaluation.alerts if a.is_new))})",
        f"  Datos informados: {format_pct(report.quality.completeness)} de lo esperado a la fecha",
    ]
    fig.text(0.04, 0.86, "\n".join(lines), va="top", fontsize=10, color=TEXT, linespacing=1.6)

    ax = fig.add_axes((0.22, 0.14, 0.4, 0.34))
    names = [catalog.program(i.program_code).name for i in indexes]
    positions = np.arange(len(indexes))
    to_date = [i.value or 0.0 for i in indexes]
    projected = [i.projected or 0.0 for i in indexes]
    ax.barh(positions + 0.2, to_date, height=0.38, color=SERIES[0], label="A la fecha")
    ax.barh(positions - 0.2, projected, height=0.38, color=SERIES[2], label="Proyectado al cierre")
    ax.set_yticks(positions, names)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.05)
    ax.xaxis.set_major_formatter(_PCT)
    ax.axvline(1.0, color=TEXT_MUTED, linewidth=0.8, linestyle="--")
    ax.set_title("Índice ponderado por programa")
    ax.set_xlabel("Índice ponderado (%), con tope de 100 % por indicador")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2)

    top = list(evaluation.alerts)[:MAX_COVER_ALERTS]
    alert_lines = ["Alertas principales"]
    alert_lines += [f"  {a.site_name}: {a.indicator_name} ({a.kind.label.lower()})" for a in top]
    fig.text(0.66, 0.5, "\n".join(alert_lines), va="top", fontsize=8.5, color=TEXT, linespacing=1.5)
    return fig


def _cell_text(result: IndicatorResult) -> str:
    """Texto de una celda de la matriz: el cumplimiento con un decimal, como en la interfaz.

    Sin decimales, un 99,6 % en amarillo se leería como "100 %".
    """
    if result.status.is_evaluated:
        return format_pct(result.compliance, 1)
    return {
        Status.NO_DATA: "s/d",
        Status.UNDETERMINED: "n/d",
        Status.BASELINE: "base",
        Status.NOT_APPLICABLE: "",
        Status.PENDING: "pend.",
    }[result.status]


def _status_matrix(report: ReportData) -> Figure:
    evaluation = report.evaluation
    catalog = report.catalog
    fig = Figure(figsize=PAGE_SIZE)
    _header(fig, report, "Semáforo por indicador y sede")
    ax = fig.add_axes((0.3, 0.1, 0.66, 0.78))
    levels: list[tuple[str | None, str]] = [(None, NETWORK_LABEL), *((s.code, s.name) for s in catalog.ordered_sites())]
    rows = evaluation.evaluations
    for y, ind_eval in enumerate(rows):
        for x, (code, _name) in enumerate(levels):
            result = ind_eval.for_level(code)
            color = STATUS_COLORS[result.status]
            ax.add_patch(Rectangle((x, y), 0.96, 0.9, color=color, linewidth=0))
            text_color = "white" if result.status in _LIGHT_TEXT else TEXT
            ax.text(x + 0.48, y + 0.45, _cell_text(result), ha="center", va="center", fontsize=7, color=text_color)
    ax.set_xlim(0, len(levels))
    ax.set_ylim(len(rows), 0)
    ax.set_xticks([i + 0.48 for i in range(len(levels))], [name for _code, name in levels])
    ax.xaxis.tick_top()
    ax.set_yticks(
        [i + 0.45 for i in range(len(rows))],
        [f"{e.indicator.code}  {e.indicator.short_name}" for e in rows],
    )
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    present = sorted({r.status for e in rows for r in e.all_results}, key=list(Status).index)
    fig.legend(
        handles=[Patch(color=STATUS_COLORS[s], label=s.label) for s in present],
        loc="lower center",
        ncol=len(present),
        bbox_to_anchor=(0.5, 0.0),
        fontsize=8,
    )
    fig.text(
        0.04,
        0.055,
        "Celdas: cumplimiento a la fecha. s/d: sin datos; n/d: meta no determinable; pend.: aún sin primer corte.",
        color=TEXT_MUTED,
    )
    return fig


def _compliance_bars(report: ReportData) -> Figure:
    evaluation = report.evaluation
    fig = Figure(figsize=PAGE_SIZE)
    _header(fig, report, "Cumplimiento a la fecha de la red por indicador")
    ax = fig.add_axes((0.3, 0.1, 0.62, 0.78))
    results = [r for r in evaluation.results(None) if r.status.is_evaluated]
    positions = np.arange(len(results))
    values = [min(r.compliance or 0.0, DISPLAY_CAP) for r in results]
    ax.barh(positions, values, color=[STATUS_COLORS[r.status] for r in results], height=0.65)
    projected = [
        (i, min(r.projection.compliance, DISPLAY_CAP))
        for i, r in enumerate(results)
        if r.projection.compliance is not None
    ]
    if projected:
        ax.scatter(
            [v for _i, v in projected],
            [i for i, _v in projected],
            marker="D",
            s=22,
            color=TEXT,
            zorder=3,
            label="Proyección al cierre",
        )
        ax.legend(loc="lower right")
    ax.axvline(1.0, color=TEXT, linewidth=1)
    ax.set_yticks(positions, [f"{r.indicator.code}  {r.indicator.short_name}" for r in results])
    ax.invert_yaxis()
    ax.set_xlim(0, DISPLAY_CAP)
    ax.xaxis.set_major_formatter(_PCT)
    ax.set_xlabel("Cumplimiento respecto de la meta a la fecha (%)")
    ax.grid(axis="x", color="#E6E9EE")
    ax.set_axisbelow(True)
    fig.text(
        0.04,
        0.05,
        f"Línea vertical: 100 % de la meta. Valores sobre {format_pct(DISPLAY_CAP, 0)} se muestran recortados.",
        color=TEXT_MUTED,
    )
    return fig


def _site_comparison(report: ReportData) -> Figure:
    evaluation = report.evaluation
    catalog = report.catalog
    fig = Figure(figsize=PAGE_SIZE)
    _header(fig, report, "Comparación entre sedes")
    sites = catalog.ordered_sites()
    programs = [i.program_code for i in evaluation.indexes_for(None)]

    left = fig.add_axes((0.07, 0.2, 0.43, 0.64))
    width = 0.8 / max(1, len(programs))
    x = np.arange(len(sites))
    for k, program_code in enumerate(programs):
        values = [evaluation.index(program_code, s.code).value or 0.0 for s in sites]
        left.bar(
            x + k * width - 0.4 + width / 2,
            values,
            width=width * 0.95,
            color=SERIES[k % len(SERIES)],
            label=catalog.program(program_code).name,
        )
    left.set_xticks(x, [s.name for s in sites], rotation=0)
    left.set_ylim(0, 1.05)
    left.yaxis.set_major_formatter(_PCT)
    left.axhline(1.0, color=TEXT_MUTED, linewidth=0.8, linestyle="--")
    left.set_title("Índice ponderado a la fecha por sede y programa")
    left.set_ylabel("Índice ponderado (%)")
    left.legend(loc="upper center", bbox_to_anchor=(0.5, -0.07), ncol=len(programs), fontsize=8)

    right = fig.add_axes((0.62, 0.2, 0.34, 0.64))
    order = (Status.GREEN, Status.YELLOW, Status.RED, Status.NO_DATA, Status.UNDETERMINED, Status.PENDING)
    y = np.arange(len(sites))
    base = np.zeros(len(sites))
    for status in order:
        counts = np.array([evaluation.status_counts(s.code).get(status) for s in sites], dtype=float)
        if not counts.any():
            continue
        right.barh(y, counts, left=base, color=STATUS_COLORS[status], label=status.label, height=0.6)
        base += counts
    right.set_yticks(y, [s.name for s in sites])
    right.invert_yaxis()
    right.set_xlabel("Número de indicadores")
    right.set_title("Estado de los indicadores por sede")
    right.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3, fontsize=8)
    return fig


def write_pdf_report(path: Path, report: ReportData, progress: ProgressFn | None = None) -> Path:
    """Genera el PDF de 4 páginas del reporte ejecutivo.

    Se escribe en un archivo temporal que reemplaza al destino solo al terminar. Si
    `progress` lanza una excepción (por ejemplo, porque el usuario canceló), el
    temporal se borra y un archivo anterior con el mismo nombre queda intacto.
    """
    builders = (
        ("Portada y resumen", _cover),
        ("Matriz de semáforo", _status_matrix),
        ("Cumplimiento por indicador", _compliance_bars),
        ("Comparación entre sedes", _site_comparison),
    )
    try:
        with replace_on_success(path) as temporary, mpl.rc_context(_STYLE), PdfPages(temporary) as pdf:
            info = pdf.infodict()
            info["Title"] = f"Reporte ejecutivo de indicadores {report.period.label}"
            info["Author"] = report.organization
            for step, (label, build) in enumerate(builders, start=1):
                pdf.savefig(build(report))
                if progress is not None:
                    progress(step * 100 // len(builders), label)
    except OSError as error:
        raise DataError(
            f"No se pudo guardar {path.name}. Verifique que el archivo no esté abierto en otro programa."
        ) from error
    return path
