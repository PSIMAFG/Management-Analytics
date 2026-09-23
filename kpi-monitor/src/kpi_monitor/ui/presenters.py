"""Lógica de presentación: textos, filas de tablas y totales que muestra la interfaz.

Son funciones puras sobre los modelos de dominio. No recalculan indicadores (eso
lo hace el motor): solo eligen qué mostrar y con qué formato. No dependen de Qt,
de modo que se prueban sin abrir ventanas.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from kpi_monitor.domain.alerts import Alert
from kpi_monitor.domain.compliance import classify
from kpi_monitor.domain.enums import (
    AlertKind,
    GoalRuleType,
    ProjectionMethod,
    Scale,
    Severity,
    Status,
)
from kpi_monitor.domain.importing import input_spec
from kpi_monitor.domain.index import WeightedIndex
from kpi_monitor.domain.models import Indicator, Period, Program, Thresholds
from kpi_monitor.domain.quality import DataQuality
from kpi_monitor.domain.results import IndicatorResult, MonthlyPoint, Projection
from kpi_monitor.domain.summary import IndicatorSheetView, MonitorSummary, StatusMatrix
from kpi_monitor.palette import STATUS_FILLS
from kpi_monitor.services import ObservationRow
from kpi_monitor.ui.formatting import (
    MONTHS,
    format_decimal,
    format_int,
    format_pct,
    format_value,
    month_name,
    period_label,
    plural,
)

NETWORK_KEY = "__red__"
# Valor de orden de las celdas sin número: quedan juntas al inicio (o al final, en orden descendente).
MISSING_SORT = -1.0e18

STATUS_SHORT = {
    Status.GREEN: "Verde",
    Status.YELLOW: "Amarillo",
    Status.RED: "Rojo",
    Status.NO_DATA: "Sin datos",
    Status.UNDETERMINED: "No determinable",
    Status.BASELINE: "Línea base",
    Status.NOT_APPLICABLE: "No aplica",
    Status.PENDING: "Pendiente",
}
SEVERITY_FILLS = {
    Severity.CRITICAL: STATUS_FILLS[Status.RED],
    Severity.WARNING: STATUS_FILLS[Status.YELLOW],
    Severity.INFO: STATUS_FILLS[Status.NO_DATA],
}
_TONES = {Status.GREEN: "good", Status.YELLOW: "warning", Status.RED: "bad"}


@dataclass(frozen=True)
class Cell:
    """Celda de tabla: texto visible, valor para ordenar, color de fondo y ayuda emergente."""

    text: str
    sort: float | str | None = None
    fill: str | None = None
    tooltip: str = ""

    @property
    def sort_value(self) -> float | str:
        if self.sort is None:
            return self.text
        return self.sort


def status_cell(status: Status, text: str | None = None, tooltip: str = "", sort: float | None = None) -> Cell:
    """Celda coloreada con el fondo suave del estado (siempre con texto, nunca solo color)."""
    return Cell(text or status.label, sort, STATUS_FILLS[status], tooltip)


def number_cell(value: float | None, text: str, fill: str | None = None, tooltip: str = "") -> Cell:
    return Cell(text, MISSING_SORT if value is None else value, fill, tooltip)


def level_key(site_code: str | None) -> str:
    """Clave de columna de un nivel: la red no tiene código de sede."""
    return site_code or NETWORK_KEY


def format_number(value: float | None) -> str:
    """Conteos sin decimales y valores fraccionarios con un decimal, en formato chileno."""
    if value is None or math.isnan(value):
        return "-"
    if abs(value - round(value)) < 1e-9:
        return format_int(value)
    return format_decimal(value, 1)


def edit_text(value: float | None) -> str:
    """Número para un campo editable, sin perder decimales: 1234.5 -> '1.234,5'."""
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return format_int(value)
    return format_decimal(value, 4).rstrip("0").rstrip(",")


def axis_value(value: float, scale: Scale) -> str:
    """Etiqueta de eje en la escala del indicador (sin la unidad, que va en el rótulo del eje)."""
    if scale is Scale.PROPORTION:
        return format_pct(value, 0)
    if scale is Scale.DAYS:
        return format_decimal(value, 0)
    return format_decimal(value, 1)


def axis_label(scale: Scale) -> str:
    return {
        Scale.PROPORTION: "Valor acumulado (%)",
        Scale.RATE: "Valor acumulado (por persona)",
        Scale.DAYS: "Promedio acumulado (días)",
    }[scale]


def compliance_text(result: IndicatorResult, decimals: int = 1) -> str:
    """Cumplimiento a la fecha, o el nombre del estado cuando no hay semáforo."""
    if result.compliance is not None:
        return format_pct(result.compliance, decimals)
    return STATUS_SHORT[result.status]


def cell_text(result: IndicatorResult) -> str:
    """Texto corto para la matriz de semáforo y el mapa de calor.

    Lleva un decimal: sin él, un 99,6 % en amarillo se leería como "100 %".
    """
    if result.compliance is not None:
        return format_pct(result.compliance, 1)
    return STATUS_SHORT[result.status]


def index_status(value: float | None, thresholds: Thresholds) -> Status:
    """Color de un índice ponderado con los mismos umbrales del semáforo."""
    status = classify(value, thresholds)
    return status if status is not None else Status.NO_DATA


def tone(status: Status | None) -> str:
    return _TONES.get(status, "muted") if status is not None else "muted"


def goal_text(result: IndicatorResult) -> str:
    """Meta anual efectiva del nivel con el contexto de su regla."""
    goal = result.goal
    scale = result.indicator.scale
    if goal.is_baseline:
        return "Línea base (sin meta)"
    if not goal.determinable or goal.value is None:
        return "No determinable"
    text = format_value(goal.value, scale)
    if goal.rule_type is GoalRuleType.BANDS:
        return f"{text} o menos (tramos)"
    if goal.rule_type is not None and goal.rule_type.needs_reference and goal.reference is not None:
        return f"{text} (año anterior {format_value(goal.reference, scale)})"
    return text


def projection_text(projection: Projection, scale: Scale) -> str:
    """Valor proyectado al cierre o el motivo por el que no hay proyección."""
    if projection.value is None:
        if projection.method is ProjectionMethod.INSUFFICIENT:
            return "Datos insuficientes"
        return "Sin proyección"
    return format_value(projection.value, scale)


def band_text(projection: Projection, scale: Scale) -> str:
    if projection.p10 is None or projection.p90 is None:
        return "-"
    return f"{format_value(projection.p10, scale)} a {format_value(projection.p90, scale)}"


def rhythm_text(projection: Projection) -> str:
    """Ritmo mensual del numerador: el observado y el necesario para cumplir."""
    current = projection.current_rhythm
    required = projection.required_rhythm
    if current is None:
        return "-"
    text = f"{format_number(round(current, 1))} por mes"
    if required is not None:
        text += f"; se necesitan {format_number(round(required, 1))}"
    return text


def change_text(result: IndicatorResult) -> str:
    """Valor del mismo período del año anterior y la variación relativa."""
    previous = result.previous_same_period
    if previous is None:
        return "Sin dato"
    text = f"{format_value(previous, result.indicator.scale)} en el mismo período"
    change = result.change_vs_previous_year
    if change is not None:
        sign = "+" if change > 0 else ""
        text += f" (variación {sign}{format_pct(change)})"
    return text


def cuts_text(result: IndicatorResult) -> str:
    """Cortes de evaluación con su estado: los futuros quedan pendientes."""
    parts = []
    for cut in result.cuts:
        state = STATUS_SHORT[cut.status]
        if cut.compliance is not None:
            state += f" ({format_pct(cut.compliance, 1)})"
        parts.append(f"{month_name(cut.month)}: {state}")
    return "; ".join(parts)


def expected_months(result: IndicatorResult) -> int:
    return sum(1 for p in result.monthly[: result.period.month] if p.expected)


def stock_text(result: IndicatorResult) -> str:
    if result.stock_month is None:
        return ""
    year = result.period.year - 1 if result.stock_carried else result.period.year
    text = f"corte de {month_name(result.stock_month)} {year}"
    return text + " (arrastrado del año anterior)" if result.stock_carried else text


def result_details(result: IndicatorResult) -> list[tuple[str, str]]:
    """Pares campo-valor del panel lateral del avance mensual."""
    ind = result.indicator
    scale = ind.scale
    projection = result.projection
    pairs = [
        ("Nivel", result.site_name),
        ("Valor acumulado", format_value(result.value, scale)),
        ("Numerador y denominador", f"{format_number(result.numerator)} / {format_number(result.denominator)}"),
        ("Meta anual", goal_text(result)),
        ("Meta a la fecha", format_value(result.goal_to_date, scale)),
        ("Cumplimiento a la fecha", compliance_text(result)),
        ("Proyección al cierre", projection_text(projection, scale)),
    ]
    if projection.p10 is not None:
        pairs.append(("Rango p10 a p90", band_text(projection, scale)))
    if projection.compliance is not None:
        pairs.append(("Cumplimiento al cierre", format_pct(projection.compliance)))
    if projection.probability is not None:
        pairs.append(("Probabilidad de cumplir", format_pct(projection.probability, 0)))
    if projection.current_rhythm is not None:
        pairs.append(("Ritmo del numerador", rhythm_text(projection)))
    method = projection.method.label
    if projection.draws:
        method += f" ({format_int(projection.draws)} simulaciones)"
    pairs.append(("Método", method))
    pairs.append(("Meses informados", f"{result.months_reported} de {expected_months(result)} esperados"))
    pairs.append(("Año anterior", change_text(result)))
    if result.stock_month is not None:
        pairs.append(("Stock usado", stock_text(result)))
    if result.cuts:
        pairs.append(("Cortes", cuts_text(result)))
    return pairs


@dataclass(frozen=True)
class KpiValue:
    """Contenido de una tarjeta de la franja de totales."""

    key: str
    value: str
    caption: str
    tone: str = "neutral"
    tooltip: str = ""


def _points(value: float) -> str:
    """Diferencia en puntos porcentuales, sin el "-0,0" que deja un signo negativo de ruido de punto flotante."""
    text = format_decimal(value * 100, 1)
    if text in ("0,0", "-0,0"):
        return "0,0 pp"
    sign = "+" if value > 0 else ""
    return f"{sign}{text} pp"


def kpi_values(summary: MonitorSummary, thresholds: Thresholds, program_names: Mapping[str, str]) -> list[KpiValue]:
    """Tarjetas de la franja superior a partir del resumen del monitor."""
    index = summary.index
    counts = summary.counts
    program = program_names.get(index.program_code, index.program_code) if index else "Sin índice"
    index_value = summary.index_value
    projected = summary.projected_index
    if index_value is not None and projected is not None:
        projected_caption = f"{_points(projected - index_value)} frente a hoy"
    else:
        projected_caption = "sin proyección"
    evaluated = counts.green + counts.yellow + counts.red
    undetermined = counts.get(Status.UNDETERMINED)
    other = (
        f"Sin datos: {counts.get(Status.NO_DATA)}. Meta no determinable: {undetermined}. "
        f"Línea base: {counts.get(Status.BASELINE)}. No aplica: {counts.get(Status.NOT_APPLICABLE)}."
    )
    weight = summary.pct_weight_without_data
    critical = summary.critical_alerts
    alerts_tone = "bad" if critical else ("warning" if summary.alerts else "good")
    counted = index.indicators_counted if index else 0
    return [
        KpiValue(
            "index",
            format_pct(index_value),
            program,
            tone(index_status(index_value, thresholds)) if index_value is not None else "muted",
            f"Índice ponderado a la fecha de {program}: suma de peso por cumplimiento (con tope de 100 %) "
            f"sobre el peso de los indicadores con dato. Indicadores con dato: {counted}.",
        ),
        KpiValue(
            "projected",
            format_pct(projected),
            projected_caption,
            tone(index_status(projected, thresholds)) if projected is not None else "muted",
            "Índice ponderado que se obtendría al cierre del año si cada indicador sigue su proyección central.",
        ),
        KpiValue(
            "green",
            format_int(counts.green),
            f"desde {format_pct(thresholds.green, 0)}",
            "good",
            f"Indicadores con cumplimiento a la fecha de al menos {format_pct(thresholds.green, 0)}.",
        ),
        KpiValue(
            "yellow",
            format_int(counts.yellow),
            f"desde {format_pct(thresholds.yellow, 0)}",
            "warning",
            f"Indicadores entre {format_pct(thresholds.yellow, 0)} y {format_pct(thresholds.green, 0)}.",
        ),
        KpiValue(
            "red",
            format_int(counts.red),
            f"bajo {format_pct(thresholds.yellow, 0)}",
            "bad",
            f"Indicadores con cumplimiento a la fecha bajo {format_pct(thresholds.yellow, 0)}.",
        ),
        KpiValue(
            "no_data",
            format_int(counts.no_data),
            "o sin meta",
            "neutral" if counts.no_data else "muted",
            f"Indicadores sin color por falta de datos o de meta. {other} Con semáforo: {evaluated}.",
        ),
        KpiValue(
            "weight",
            format_pct(weight),
            "del peso evaluable",
            "warning" if weight else "neutral",
            "Parte del peso del programa que no se pudo medir (sin datos o meta no determinable). "
            "No entra al índice, pero se informa para no ocultarlo.",
        ),
        KpiValue(
            "alerts",
            format_int(summary.alerts),
            f"{plural(critical, 'crítica', 'críticas')}, {plural(summary.new_alerts, 'nueva', 'nuevas')}",
            alerts_tone,
            "Alertas del período para el nivel elegido (en la red se incluyen las de todas las sedes). "
            "Una alerta es nueva si no existía en el mes anterior.",
        ),
    ]


def scope_title(level_name: str, period: Period) -> str:
    return f"{level_name}, {period.label}"


def scope_detail(site_code: str | None, program: Program | None, index_program: Program | None) -> str:
    """Explica el alcance de los totales: nivel y programa del índice ponderado."""
    level = "Red: suma de todas las sedes." if site_code is None else "Resultados de la sede."
    if program is not None:
        return f"{level} Filtro de programa: {program.name}."
    if index_program is None:
        return f"{level} Todos los programas."
    return f"{level} Todos los programas; el índice ponderado corresponde a {index_program.name}."


def last_data_text(period: Period, last_month: int | None) -> str:
    """Aviso del panel lateral sobre el último mes con datos del año elegido."""
    if last_month is None:
        return f"Sin datos informados en {period.year}; todos los meses figuran como no informados."
    text = f"Último mes con datos: {period_label(period.year, last_month)}."
    if last_month < period.month:
        text += " Los meses siguientes hasta el corte figuran como no informados."
    return text


def quality_text(quality: DataQuality) -> str:
    completeness = quality.completeness
    if completeness is None:
        return "Sin datos esperados a la fecha"
    return (
        f"Datos recibidos: {format_pct(completeness)} de lo esperado "
        f"({format_int(quality.reported)} de {format_int(quality.expected)})"
    )


def indicator_label(indicator: Indicator) -> str:
    return f"{indicator.code}  {indicator.short_name}"


def result_row(result: IndicatorResult, order: int = 0) -> dict[str, object]:
    """Fila de la tabla de resultados por indicador (o por nivel en la comparación entre sedes)."""
    ind = result.indicator
    scale = ind.scale
    projection = result.projection
    note = result.note or ""
    return {
        "code": ind.code,
        "site_code": result.site_code,
        "indicator": Cell(indicator_label(ind), order, None, ind.name),
        "level": Cell(result.site_name, order),
        "value": number_cell(result.value, format_value(result.value, scale)),
        "goal_to_date": number_cell(result.goal_to_date, format_value(result.goal_to_date, scale)),
        "annual_goal": Cell(goal_text(result), result.goal.value if result.goal.value is not None else MISSING_SORT),
        "compliance": number_cell(result.compliance, compliance_text(result), tooltip=note),
        "status": status_cell(result.status, tooltip=note, sort=_status_rank(result.status)),
        "projection": number_cell(projection.value, projection_text(projection, scale)),
        "projected_compliance": number_cell(projection.compliance, format_pct(projection.compliance)),
        "probability": number_cell(projection.probability, format_pct(projection.probability, 0)),
        "weight": number_cell(result.weight, format_pct(result.weight)),
        "months": number_cell(result.months_reported, f"{result.months_reported} de {expected_months(result)}"),
    }


_STATUS_ORDER = (
    Status.RED,
    Status.YELLOW,
    Status.GREEN,
    Status.UNDETERMINED,
    Status.NO_DATA,
    Status.BASELINE,
    Status.NOT_APPLICABLE,
    Status.PENDING,
)


def _status_rank(status: Status) -> float:
    return float(_STATUS_ORDER.index(status))


def reported_text(point: MonthlyPoint, cut_month: int) -> str:
    if point.month > cut_month:
        return "Pendiente"
    if not point.expected:
        return "No corresponde"
    if point.reported is True:
        return "Sí"
    if point.reported is False:
        return "No informado"
    return "Sin registro"


def monthly_rows(result: IndicatorResult) -> list[dict[str, object]]:
    """Filas de la tabla mes a mes del indicador seleccionado."""
    scale = result.indicator.scale
    cut = result.period.month
    rows = []
    for p in result.monthly:
        reported = reported_text(p, cut)
        missing = p.month <= cut and p.expected and p.reported is not True
        rows.append(
            {
                "month": Cell(MONTHS[p.month - 1].capitalize(), p.month),
                "reported": Cell(reported, None, STATUS_FILLS[Status.NO_DATA] if missing else None),
                "numerator": number_cell(p.numerator, format_number(p.numerator)),
                "denominator": number_cell(p.denominator, format_number(p.denominator)),
                "monthly_value": number_cell(p.monthly_value, format_value(p.monthly_value, scale)),
                "cumulative": number_cell(p.cumulative_value, format_value(p.cumulative_value, scale)),
                "goal_to_date": number_cell(p.goal_to_date, format_value(p.goal_to_date, scale)),
                "compliance": number_cell(
                    p.compliance, format_pct(p.compliance) if p.compliance is not None else STATUS_SHORT[p.status]
                ),
                "status": status_cell(p.status, sort=_status_rank(p.status)),
            }
        )
    return rows


def matrix_tooltip(result: IndicatorResult) -> str:
    scale = result.indicator.scale
    lines = [
        f"{result.indicator.code} {result.indicator.short_name}, {result.site_name}",
        f"Estado: {result.status.label}",
        f"Valor: {format_value(result.value, scale)}; meta a la fecha: {format_value(result.goal_to_date, scale)}",
        f"Proyección al cierre: {projection_text(result.projection, scale)}",
    ]
    if result.note:
        lines.append(result.note)
    return "\n".join(lines)


def matrix_rows(
    matrix: StatusMatrix,
    indexes: Sequence[WeightedIndex],
    programs: Sequence[Program],
    thresholds: Thresholds,
) -> list[dict[str, object]]:
    """Matriz indicador × nivel con una fila final de índice ponderado por programa."""
    rows: list[dict[str, object]] = []
    for order, ind in enumerate(matrix.indicators):
        weight = matrix.cell(ind.code, None).weight
        row: dict[str, object] = {
            "code": ind.code,
            "bold": False,
            "indicator": Cell(indicator_label(ind), order, None, ind.name),
            "weight": number_cell(weight, format_pct(weight)),
        }
        for site_code, _name in matrix.levels:
            result = matrix.cell(ind.code, site_code)
            row[level_key(site_code)] = status_cell(
                result.status, cell_text(result), matrix_tooltip(result), _status_rank(result.status)
            )
        rows.append(row)
    program_codes = {ind.program_code for ind in matrix.indicators}
    for position, program in enumerate(p for p in programs if p.code in program_codes):
        row = {
            "code": None,
            "bold": True,
            "indicator": Cell(f"{program.name} (índice ponderado)", len(rows) + position),
            "weight": Cell("100,0 %", 1.0),
        }
        for site_code, _name in matrix.levels:
            index = next((i for i in indexes if i.program_code == program.code and i.site_code == site_code), None)
            value = index.value if index else None
            tip = ""
            if index is not None and index.pct_weight_without_data:
                tip = f"Peso sin datos: {format_pct(index.pct_weight_without_data)}"
            row[level_key(site_code)] = status_cell(
                index_status(value, thresholds), format_pct(value), tip, MISSING_SORT if value is None else value
            )
        rows.append(row)
    return rows


def filter_alerts(
    alerts: Iterable[Alert],
    severity: Severity | None = None,
    kind: AlertKind | None = None,
    only_new: bool = False,
    indicator_code: str | None = None,
) -> list[Alert]:
    return [
        a
        for a in alerts
        if (severity is None or a.severity is severity)
        and (kind is None or a.kind is kind)
        and (not only_new or a.is_new)
        and (indicator_code is None or a.indicator_code == indicator_code)
    ]


def alert_rows(alerts: Iterable[Alert]) -> list[dict[str, object]]:
    rows = []
    for order, alert in enumerate(alerts):
        label = (
            alert.indicator_name if alert.indicator_code is None else f"{alert.indicator_code} {alert.indicator_name}"
        )
        rows.append(
            {
                "alert": alert,
                "severity": Cell(
                    alert.severity.label, alert.severity.rank * 1000 + order, SEVERITY_FILLS[alert.severity]
                ),
                "kind": Cell(alert.kind.label),
                "level": Cell(alert.site_name, "" if alert.site_code is None else alert.site_name),
                "indicator": Cell(label, alert.indicator_code or ""),
                "message": Cell(alert.message, None, None, alert.message),
                "new": Cell("Sí" if alert.is_new else "No"),
            }
        )
    return rows


def alerts_summary(alerts: Sequence[Alert]) -> str:
    """Resumen de una lista de alertas por gravedad."""
    if not alerts:
        return "Sin alertas para los filtros elegidos."
    by_severity = Counter(a.severity for a in alerts)
    parts = [
        plural(by_severity[Severity.CRITICAL], "crítica", "críticas"),
        plural(by_severity[Severity.WARNING], "advertencia", "advertencias"),
        f"{format_int(by_severity[Severity.INFO])} de información",
    ]
    new = sum(1 for a in alerts if a.is_new)
    return f"{plural(len(alerts), 'alerta', 'alertas')}: {', '.join(parts)}. Nuevas respecto del mes anterior: {new}."


@dataclass(frozen=True)
class ObservationFilter:
    """Filtros propios de la pestaña de datos."""

    month: int | None = None
    indicator_code: str | None = None
    only_missing: bool = False
    up_to_month: int | None = None


def filter_observations(rows: Iterable[ObservationRow], flt: ObservationFilter) -> list[ObservationRow]:
    """Aplica los filtros de mes, indicador y meses no informados."""
    chosen = []
    for row in rows:
        if flt.month is not None and row.month != flt.month:
            continue
        if flt.month is None and flt.up_to_month is not None and row.month > flt.up_to_month:
            continue
        if flt.indicator_code is not None and row.indicator_code != flt.indicator_code:
            continue
        if flt.only_missing and row.reported:
            continue
        chosen.append(row)
    return chosen


def missing_rows(
    existing: Iterable[ObservationRow], quality: DataQuality, year: int, program_by_indicator: Mapping[str, str]
) -> list[ObservationRow]:
    """Filas de faltante para los meses esperados sin ningún registro (ni siquiera marcado como no informado).

    Se agregan a las observaciones ya guardadas para que la tabla de datos y el filtro
    de faltantes cuadren con `quality_text` y con `DataQuality.missing`.
    """
    present = {(row.indicator_code, row.site_code, row.month) for row in existing}
    rows = []
    for report in quality.missing:
        key = (report.indicator_code, report.site_code, report.month)
        if key in present:
            continue
        present.add(key)
        rows.append(
            ObservationRow(
                indicator_code=report.indicator_code,
                indicator_name=report.indicator_name,
                program_code=program_by_indicator.get(report.indicator_code, ""),
                site_code=report.site_code,
                site_name=report.site_name,
                year=year,
                month=report.month,
                period=period_label(year, report.month),
                numerator=None,
                denominator=None,
                reported=False,
                origin="Sin registro",
            )
        )
    return rows


def order_observations(
    rows: Iterable[ObservationRow], indicator_order: Mapping[str, int], site_order: Mapping[str, int]
) -> list[ObservationRow]:
    """Ordena las observaciones por mes y luego en el orden del catálogo (indicadores y sedes)."""
    last = len(indicator_order) + len(site_order)
    return sorted(
        rows,
        key=lambda r: (
            r.year,
            r.month,
            indicator_order.get(r.indicator_code, last),
            site_order.get(r.site_code, last),
        ),
    )


def observation_rows(rows: Iterable[ObservationRow]) -> list[dict[str, object]]:
    """Filas de la tabla de datos a partir de las observaciones del servicio."""
    return [
        {
            "record": row,
            "site": Cell(row.site_name),
            "indicator": Cell(f"{row.indicator_code}  {row.indicator_name}", row.indicator_code),
            "period": Cell(row.period.capitalize(), row.year * 100 + row.month),
            "numerator": number_cell(row.numerator, format_number(row.numerator)),
            "denominator": number_cell(row.denominator, format_number(row.denominator)),
            "reported": Cell(
                "Sí" if row.reported else "No informado",
                float(row.reported),
                None if row.reported else STATUS_FILLS[Status.NO_DATA],
            ),
            "origin": Cell(row.origin),
        }
        for row in rows
    ]


@dataclass(frozen=True)
class DenominatorField:
    """Cómo se captura el denominador de un indicador en el diálogo de edición."""

    enabled: bool
    required: bool
    label: str
    hint: str


def denominator_field(indicator: Indicator) -> DenominatorField:
    """Reglas de captura del denominador según su tipo (la misma definición que usa la importación)."""
    spec = input_spec(indicator)
    return DenominatorField(
        spec.denominator_enabled, spec.denominator_required, spec.denominator_label, spec.denominator_hint
    )


def numerator_hint(indicator: Indicator) -> str:
    """Ayuda de captura del numerador (la misma definición que usa la importación)."""
    return input_spec(indicator).numerator_hint


def sheet_html(views: Sequence[IndicatorSheetView], current_year: int) -> str:
    """Ficha del indicador en HTML simple: definición, reglas del año elegido e historial de metas."""
    current = next((v for v in views if v.year == current_year), views[-1])
    ind = current.indicator
    rows = "".join(
        f"<tr><td class='k'>{_escape(key)}</td><td>{_escape(value)}</td></tr>" for key, value in current.rows()
    )
    history = "".join(
        "<tr>"
        f"<td class='k'>{view.year}</td>"
        f"<td>{_escape(view.rule.rule_type.label if view.rule else 'Sin regla')}</td>"
        f"<td>{_escape(view.rule_description)}</td>"
        f"<td class='n'>{'-' if view.weight is None else format_pct(view.weight, 2)}</td>"
        "</tr>"
        for view in views
    )
    light = "con semáforo" if current.has_traffic_light else "sin semáforo"
    return (
        "<html><head><style>"
        "body { font-family: 'Segoe UI'; font-size: 10pt; color: #1F2933; }"
        "h2 { color: #1F4E79; font-size: 14pt; margin: 0 0 2px 0; }"
        "h3 { color: #1F4E79; font-size: 11pt; margin: 16px 0 6px 0; }"
        "p.sub { color: #5F6B7A; margin: 0 0 10px 0; }"
        "table { border-collapse: collapse; width: 100%; }"
        "td, th { padding: 5px 8px; border-bottom: 1px solid #E6E9EE; vertical-align: top; }"
        "th { text-align: left; background: #EEF1F5; }"
        "td.k { color: #5F6B7A; width: 220px; }"
        "td.n { text-align: right; }"
        "</style></head><body>"
        f"<h2>{_escape(ind.code)} {_escape(ind.name)}</h2>"
        f"<p class='sub'>{_escape(current.program.name)}. Ficha vigente en {current.year} ({light}).</p>"
        f"<table>{rows}</table>"
        "<h3>Meta y peso por año</h3>"
        "<table><tr><th>Año</th><th>Tipo de regla</th><th>Meta</th><th>Peso</th></tr>"
        f"{history}</table>"
        "</body></html>"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
