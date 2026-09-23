"""Textos, filas y series que muestra la interfaz, preparados a partir de los resultados de los servicios.

Funciones puras (sin Qt) para poder probarlas de forma aislada. Todos los
montos salen de los agregados de la proyección, que suman exactamente el total.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from staffing_simulator.domain.budget import ItemLine, ItemReport, ProgramLine
from staffing_simulator.domain.comparison import ComparisonRow, ScenarioComparison
from staffing_simulator.domain.models import MONTH_LABELS, MONTHS, OtherExpense, Position
from staffing_simulator.domain.parameters import DEFAULT_WEEKS_PER_MONTH
from staffing_simulator.domain.projection import Dimension, PositionCost, Projection
from staffing_simulator.domain.text import plural
from staffing_simulator.domain.units import MINUTES_PER_HOUR, safe_ratio
from staffing_simulator.services import ScenarioKpis
from staffing_simulator.ui.charts import BudgetBar
from staffing_simulator.ui.formatting import (
    format_clp,
    format_date,
    format_hours,
    format_millions,
    format_signed_clp,
    format_signed_pct,
    month_name,
)

Row = dict[str, Any]


@dataclass(frozen=True)
class KpiText:
    """Contenido de una tarjeta de totales."""

    value: str
    caption: str = ""
    tone: str = "neutral"
    tooltip: str = ""


KPI_CARDS: tuple[tuple[str, str], ...] = (
    ("assigned", "Presupuesto asignado"),
    ("planned", "Planificado total"),
    ("balance", "Saldo"),
    ("hr_cost", "Costo de recurso humano"),
    ("other_cost", "Otros gastos"),
    ("staff", "Dotación"),
)


NBSP = chr(0xA0)


def keep_together(text: str) -> str:
    """Evita que un salto de línea separe el signo peso, la cifra y su unidad."""
    return text.replace("$ ", f"${NBSP}").replace(" millones", f"{NBSP}millones").replace(" h", f"{NBSP}h")


def kpi_texts(kpis: ScenarioKpis, *, base: ScenarioKpis | None = None) -> dict[str, KpiText]:
    """Textos de las tarjetas de totales del escenario seleccionado."""
    texts: dict[str, KpiText] = {}
    planned_total = kpis.human_resources_cost + kpis.other_expenses_total

    if kpis.assigned_total == 0:
        texts["assigned"] = KpiText("Sin ítems", "Cargue la estructura en Estructura del programa", "warning")
    else:
        texts["assigned"] = KpiText(
            format_clp(kpis.assigned_total),
            "Suma de los ítems presupuestarios del año",
            tooltip="Suma de los montos asignados a los ítems presupuestarios de los programas del año del escenario.",
        )

    if base is None:
        texts["planned"] = KpiText(format_clp(planned_total), "Recurso humano + otros gastos")
    else:
        base_planned = base.human_resources_cost + base.other_expenses_total
        difference = planned_total - base_planned
        share = safe_ratio(difference, base_planned)
        texts["planned"] = KpiText(
            format_clp(planned_total),
            f"{format_signed_pct(share)} frente al escenario base",
            tooltip=(
                f"Diferencia con «{base.scenario.name}»: {format_signed_clp(difference)} ({format_signed_pct(share)})"
            ),
        )

    tone = "bad" if kpis.balance_total < 0 else ("warning" if kpis.items_without_assignment else "good")
    balance_caption = (
        f"{plural(kpis.deficit_items, 'ítem', 'ítems')} en déficit" if kpis.deficit_items else "Sin déficit"
    )
    texts["balance"] = KpiText(
        format_signed_clp(kpis.balance_total),
        balance_caption,
        tone,
        tooltip="Saldo = asignado (suma de los ítems) - planificado (recurso humano + otros gastos).",
    )

    texts["hr_cost"] = KpiText(
        format_clp(kpis.human_resources_cost),
        keep_together(f"Máximo mensual en {month_name(kpis.peak_month)}: {format_millions(kpis.peak_month_cost)}"),
        tooltip=f"Costo proyectado de las posiciones. Mes de mayor costo: {month_name(kpis.peak_month)}.",
    )

    texts["other_cost"] = KpiText(
        format_clp(kpis.other_expenses_total),
        "Arriendos, compras e insumos",
        tooltip=(
            "Total anual de los otros gastos planificados del escenario (arriendos, compras, insumos, "
            "capacitación, movilización, etc.). No incluye el costo de recurso humano."
        ),
    )

    texts["staff"] = KpiText(
        plural(kpis.headcount, "puesto", "puestos"),
        f"{plural(kpis.persons, 'persona', 'personas')} y {plural(kpis.vacancies, 'vacante', 'vacantes')}",
        tooltip=(
            f"Puestos con vigencia en {kpis.scenario.year}: personas distintas más vacantes. Una persona con "
            "varias posiciones o con un cambio de jornada (dos tramos) cuenta una sola vez. Registros de posición "
            f"vigentes, sumando cantidades: {kpis.position_records}. Horas semanales promedio del año: "
            f"{format_hours(kpis.weekly_hours)} h."
        ),
    )
    return texts


def holder_text(position: Position) -> str:
    """Persona asignada o vacante; las vacantes idénticas muestran su cantidad."""
    if position.person is not None:
        return position.person.full_name
    return "Vacante" if position.quantity == 1 else f"{position.quantity} vacantes"


def workload_text(position: Position, weeks_per_month: Decimal = DEFAULT_WEEKS_PER_MONTH) -> tuple[str, float]:
    """Jornada de la posición para la tabla y su equivalente semanal para ordenar."""
    weekly = float(position.weekly_equivalent_minutes(weeks_per_month) / MINUTES_PER_HOUR)
    if position.weekly_hours is not None:
        return f"{format_hours(position.weekly_hours)} h/sem", weekly
    if position.monthly_hours is not None:
        return f"{format_hours(position.monthly_hours)} h/mes", weekly
    return "-", 0.0


def position_rows(
    positions: Sequence[Position],
    costs: Mapping[int, PositionCost] | None = None,
    weeks_per_month: Decimal = DEFAULT_WEEKS_PER_MONTH,
) -> list[Row]:
    """Filas de la tabla de posiciones; el costo queda vacío si no se pudo proyectar."""
    rows = []
    for position in positions:
        cost = None if costs is None else costs.get(position.id)
        workload, workload_sort = workload_text(position, weeks_per_month)
        holder = holder_text(position)
        # La primera línea repite la persona: con la ventana angosta su columna se abrevia.
        details = [f"{position.job_role.name}: {holder}"]
        if cost is not None and cost.active_months == 0:
            details.append("Sin vigencia en el año del escenario: no suma costo.")
        details += [
            f"{position.contract_type.name}: {position.contract_type.cost_method.label.lower()}",
            f"Ítem presupuestario: {position.budget_item.name}",
            f"Vigencia: {format_date(position.start_date)} a "
            + (format_date(position.end_date) if position.end_date else "fin de año"),
        ]
        if position.grade is not None:
            details.append(f"Grado real: {position.grade} (el costo usa el grado 15)")
        if cost is not None and cost.active_months:
            details.append(f"Valor hora: {format_clp(cost.hourly_rate)}; meses con costo: {cost.active_months}")
        if position.note:
            details.append(f"Nota: {position.note}")
        rows.append(
            {
                "id": position.id,
                "role": position.job_role.name,
                "contract": position.contract_type.name,
                "site": position.site.name,
                "program": position.program.name,
                "holder": holder,
                "workload": workload,
                "workload_sort": workload_sort,
                "start": position.start_date,
                "end_text": format_date(position.end_date) if position.end_date else "Sin término",
                "end_sort": position.end_date or date.max,
                "total": None if cost is None else cost.total,
                "tooltip": "\n".join(details),
            }
        )
    return rows


def empty_projection_message(projection: Projection) -> str:
    """Texto de los gráficos cuando el escenario no suma costo, según la causa."""
    year = projection.scenario.year
    count = len(projection.positions)
    if count == 0:
        return "El escenario no tiene posiciones. Agréguelas en la pestaña Posiciones."
    if not projection.active_positions():
        subject = "La posición del escenario no tiene" if count == 1 else f"Ninguna de las {count} posiciones tiene"
        return (
            f"{subject} vigencia en {year}. Revise sus fechas en la pestaña Posiciones o el año del escenario "
            "en el panel lateral."
        )
    return f"Las posiciones del escenario no suman costo en {year}."


def monthly_rows(projection: Projection, budget_total: int = 0) -> list[Row]:
    """Costo por mes, acumulado y avance del acumulado contra el presupuesto anual."""
    monthly = projection.monthly()
    cumulative = projection.cumulative()
    total = projection.total_cost
    rows: list[Row] = [
        {
            "month": month_name(month).capitalize(),
            "cost": monthly[month - 1],
            "cumulative": cumulative[month - 1],
            "budget_share": safe_ratio(cumulative[month - 1], budget_total) if budget_total else None,
        }
        for month in MONTHS
    ]
    rows.append(
        {
            "month": "Total",
            "cost": total,
            "cumulative": total,
            "budget_share": safe_ratio(total, budget_total) if budget_total else None,
            "_total": True,
        }
    )
    return rows


BREAKDOWN_SECTIONS: tuple[tuple[Dimension, str], ...] = (
    (Dimension.CONTRACT_TYPE, "Por tipo de contrato"),
    (Dimension.PROGRAM, "Por programa"),
    (Dimension.JOB_ROLE, "Por cargo"),
    (Dimension.SITE, "Por sede"),
)


def breakdown_table(projection: Projection) -> list[Row]:
    """Total del escenario y, debajo, el detalle de cada dimensión con un título de sección."""
    rows = breakdown_rows(projection, Dimension.CONTRACT_TYPE)
    if not rows:
        return []
    table: list[Row] = [dict(rows[-1], label="Total del escenario")]
    for dimension, title in BREAKDOWN_SECTIONS:
        table.append({"label": title, "_group": True})
        table.extend(row for row in breakdown_rows(projection, dimension) if not row.get("_total"))
    return table


def breakdown_rows(projection: Projection, dimension: Dimension) -> list[Row]:
    """Costo anual por elemento de la dimensión, con las posiciones vigentes (sumando cantidades)."""
    positions_by_key: dict[int, int] = defaultdict(int)
    for position in projection.active_positions():
        key, _label = dimension.key_of(position)
        positions_by_key[key] += position.quantity
    rows: list[Row] = [
        {
            "label": row.label,
            "positions": positions_by_key.get(row.key, 0),
            "total": row.total,
            "share": row.share,
        }
        for row in projection.breakdown(dimension)
    ]
    if rows:
        total = projection.total_cost
        rows.append(
            {
                "label": "Total",
                "positions": sum(row["positions"] for row in rows),
                "total": total,
                "share": safe_ratio(total, total),
                "_total": True,
            }
        )
    return rows


@dataclass(frozen=True)
class ColumnSpec:
    """Columna de una tabla generada: clave, encabezado, tipo de formato y ayuda del encabezado."""

    key: str
    header: str
    kind: str = "text"
    tooltip: str = ""


COMPARISON_SECTIONS: tuple[tuple[str, str], ...] = (
    ("monthly", "Por mes"),
    ("contract", "Por tipo de contrato"),
    ("role", "Por cargo"),
)


def comparison_table(comparison: ScenarioComparison, section: str) -> tuple[list[ColumnSpec], list[Row]]:
    """Tabla de diferencias contra el base: primero el costo total y luego la sección pedida.

    Cada escenario tiene tres columnas (valor, diferencia y variación) y los
    encabezados nombran el escenario, para distinguirlos cuando se comparan
    tres o más.
    """
    base = comparison.base_index
    base_name = comparison.base.name
    columns = [ColumnSpec("label", "Concepto"), ColumnSpec(f"v{base}", f"{base_name} (base)", "money")]
    for index, item in enumerate(comparison.scenarios):
        if index == base:
            continue
        short = shorten(item.name, 22)
        columns += [
            ColumnSpec(f"v{index}", item.name, "money"),
            ColumnSpec(
                f"d{index}",
                f"Diferencia\n{short}",
                "signed_money",
                f"Diferencia de «{item.name}» contra «{base_name}», en pesos",
            ),
            ColumnSpec(
                f"p{index}",
                f"Variación\n{short}",
                "signed_pct",
                f"Diferencia de «{item.name}» contra «{base_name}», como porcentaje del base",
            ),
        ]
    sections: dict[str, Sequence[ComparisonRow]] = {
        "monthly": comparison.monthly,
        "contract": comparison.by_contract_type,
        "role": comparison.by_job_role,
    }
    rows = [_comparison_row(comparison.total, total=True)]
    for item in sections.get(section, comparison.monthly):
        label = item.label
        if section == "monthly" and label in MONTH_LABELS:
            label = month_name(MONTH_LABELS.index(label) + 1).capitalize()
        rows.append(_comparison_row(item, label=label))
    return columns, rows


def _comparison_row(item: ComparisonRow, *, label: str | None = None, total: bool = False) -> Row:
    row: Row = {"label": label or item.label}
    for index, value in enumerate(item.values):
        row[f"v{index}"] = value
        row[f"d{index}"] = item.differences[index]
        row[f"p{index}"] = item.percentages[index]
    if total:
        row["_total"] = True
    return row


def _item_row(line: ItemLine) -> Row:
    return {
        "item_id": line.item.id,
        "item": line.item.label,
        "item_name": line.item.name,
        "program": line.item.program.name,
        "program_id": line.item.program.id,
        "item_type": line.item.item_type.label,
        "assigned": line.assigned,
        "planned_hr": line.planned_hr_total,
        "planned_other": line.planned_other_total,
        "planned": line.planned_total,
        "balance": line.balance,
        "used_share": line.used_share,
        "executed_to_date": line.executed_to_date if line.has_execution else None,
        "execution_share": line.execution_share if line.has_execution else None,
    }


def item_report_rows(report: ItemReport) -> list[Row]:
    """Asignado, planificado, saldo y ejecución a la fecha por ítem presupuestario, con el total."""
    rows = [_item_row(line) for line in report.lines]
    if rows:
        rows.append(
            {
                "item_id": None,
                "item": "Total",
                "program": "",
                "item_type": "",
                "assigned": report.total_assigned,
                "planned_hr": report.total_planned_hr,
                "planned_other": report.total_planned_other,
                "planned": report.total_planned,
                "balance": report.total_balance,
                "used_share": safe_ratio(report.total_planned, report.total_assigned),
                "executed_to_date": report.total_executed_to_date,
                "execution_share": safe_ratio(report.total_executed_to_date, report.total_assigned),
                "_total": True,
            }
        )
    return rows


def _program_row(line: ProgramLine) -> Row:
    return {
        "program_id": line.program.id,
        "program": line.program.name,
        "assigned": line.assigned,
        "planned_hr": line.planned_hr,
        "planned_other": line.planned_other,
        "planned": line.planned_total,
        "balance": line.balance,
        "used_share": line.used_share,
        "executed_to_date": line.executed_to_date if line.has_execution else None,
        "execution_share": safe_ratio(line.executed_to_date, line.assigned)
        if line.has_execution and line.assigned
        else None,
    }


def program_report_rows(report: ItemReport) -> list[Row]:
    """Lo mismo que `item_report_rows`, pero con los ítems sumados por programa."""
    lines = report.by_program()
    rows = [_program_row(line) for line in lines]
    if rows:
        rows.append(
            {
                "program_id": None,
                "program": "Total",
                "assigned": report.total_assigned,
                "planned_hr": report.total_planned_hr,
                "planned_other": report.total_planned_other,
                "planned": report.total_planned,
                "balance": report.total_balance,
                "used_share": safe_ratio(report.total_planned, report.total_assigned),
                "executed_to_date": report.total_executed_to_date,
                "execution_share": safe_ratio(report.total_executed_to_date, report.total_assigned),
                "_total": True,
            }
        )
    return rows


def item_chart_bars(report: ItemReport, *, by_program: bool = False) -> list[BudgetBar]:
    """Barras de asignado contra planificado, por ítem o agrupadas por programa."""
    if by_program:
        return [BudgetBar(line.program.name, line.assigned or None, line.planned_total) for line in report.by_program()]
    return [BudgetBar(shorten(line.item.label, 30), line.assigned or None, line.planned_total) for line in report.lines]


def monthly_series(report: ItemReport, item_id: int | None = None) -> tuple[tuple[int, ...], tuple[int | None, ...]]:
    """Planificado y ejecutado por mes, de un ítem o de todos los ítems combinados."""
    lines = report.lines if item_id is None else [line for line in report.lines if line.item.id == item_id]
    planned = [0] * 12
    executed: list[int | None] = [None] * 12
    for line in lines:
        for index in range(12):
            planned[index] += line.planned_monthly[index]
            value = line.executed[index]
            if value is not None:
                executed[index] = (executed[index] or 0) + value
    return tuple(planned), tuple(executed)


def item_monthly_rows(report: ItemReport, item_id: int | None = None) -> list[Row]:
    """Planificado contra ejecutado por mes, de un ítem o de todos los ítems, con el total a la fecha."""
    planned, executed = monthly_series(report, item_id)
    rows: list[Row] = []
    for month in MONTHS:
        index = month - 1
        difference = None if executed[index] is None else executed[index] - planned[index]
        rows.append(
            {
                "month": month_name(month).capitalize(),
                "planned": planned[index],
                "executed": executed[index],
                "difference": difference,
                "difference_share": None if difference is None else safe_ratio(difference, planned[index]),
            }
        )
    if any(value is not None for value in executed):
        planned_to_date = sum(p for p, e in zip(planned, executed, strict=True) if e is not None)
        executed_to_date = sum(e for e in executed if e is not None)
        difference = executed_to_date - planned_to_date
        rows.append(
            {
                "month": "Total a la fecha",
                "planned": planned_to_date,
                "executed": executed_to_date,
                "difference": difference,
                "difference_share": safe_ratio(difference, planned_to_date),
                "_total": True,
            }
        )
    return rows


def coverage_text(report: ItemReport) -> str:
    """Meses con ejecución registrada de algún ítem."""
    months = sorted(
        {month for line in report.lines for month, value in enumerate(line.executed, start=1) if value is not None}
    )
    if not months:
        return f"Sin ejecución registrada en {report.year}. Impórtela desde el panel."
    names = ", ".join(month_name(month) for month in months)
    return f"Ejecución registrada en {names} de {report.year}."


def other_expense_rows(expenses: Sequence[OtherExpense], year: int) -> list[Row]:
    """Descripción, programa, ítem, tipo, vigencia y monto anual de cada otro gasto del escenario."""
    rows = []
    for expense in expenses:
        rows.append(
            {
                "id": expense.id,
                "description": expense.description,
                "program": expense.budget_item.program.name,
                "item": expense.budget_item.name,
                "item_id": expense.budget_item.id,
                "expense_type": expense.expense_type.label,
                "start": expense.start_date,
                "end_text": format_date(expense.end_date) if expense.end_date else "Sin término",
                "amount": expense.amount,
                "annual_total": sum(expense.monthly_amounts(year)),
            }
        )
    return rows


def identity_colors(ids: Sequence[int], palette: Sequence[str]) -> dict[int, str]:
    """Color por entidad según el orden estable de sus identificadores.

    Se llama con las entidades que muestra el gráfico (por ejemplo, solo los
    escenarios comparados): mientras no superen el largo de la paleta, dos
    entidades de un mismo gráfico nunca comparten color.
    """
    return {item: palette[index % len(palette)] for index, item in enumerate(sorted(set(ids)))}


def shorten(text: str, limit: int = 28) -> str:
    """Acorta un texto largo con puntos suspensivos para rótulos de gráficos."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def tone_for_balance(value: int | Decimal | None) -> str | None:
    """Tono de un saldo: verde si hay holgura, rojo si hay déficit."""
    if value is None or value == 0:
        return None
    return "good" if value > 0 else "bad"
