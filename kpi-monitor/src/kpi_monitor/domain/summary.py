"""Vistas de resumen que consume la interfaz: totales, matriz de semáforo y ficha del indicador."""

from __future__ import annotations

from dataclasses import dataclass

from kpi_monitor.domain.enums import GoalRuleType, Status
from kpi_monitor.domain.goals import describe_rule
from kpi_monitor.domain.index import WeightedIndex
from kpi_monitor.domain.models import GoalRule, Indicator, Period, Program
from kpi_monitor.domain.results import IndicatorResult, StatusCounts
from kpi_monitor.domain.text import format_decimal, format_value, join_months


@dataclass(frozen=True)
class MonitorSummary:
    """Totales de la franja superior para un período, un nivel y un programa."""

    period: Period
    site_code: str | None
    program_code: str | None
    index: WeightedIndex | None
    counts: StatusCounts
    alerts: int
    new_alerts: int
    critical_alerts: int

    @property
    def index_value(self) -> float | None:
        return self.index.value if self.index else None

    @property
    def projected_index(self) -> float | None:
        return self.index.projected if self.index else None

    @property
    def pct_weight_without_data(self) -> float | None:
        return self.index.pct_weight_without_data if self.index else None


@dataclass(frozen=True)
class StatusMatrix:
    """Matriz indicador × nivel (red y sedes) con el resultado de cada celda."""

    indicators: tuple[Indicator, ...]
    levels: tuple[tuple[str | None, str], ...]
    cells: dict[tuple[str, str | None], IndicatorResult]

    def cell(self, indicator_code: str, site_code: str | None) -> IndicatorResult:
        return self.cells[(indicator_code, site_code)]

    def status(self, indicator_code: str, site_code: str | None) -> Status:
        return self.cell(indicator_code, site_code).status


@dataclass(frozen=True)
class IndicatorSheetView:
    """Ficha completa de un indicador para un año, lista para mostrar."""

    indicator: Indicator
    program: Program
    year: int
    rule: GoalRule | None
    site_goals: tuple[tuple[str, float], ...]

    @property
    def rule_description(self) -> str:
        return describe_rule(self.rule, self.indicator)

    @property
    def weight(self) -> float | None:
        return self.rule.weight if self.rule else None

    @property
    def has_traffic_light(self) -> bool:
        return self.rule is not None and self.rule.rule_type is not GoalRuleType.BASELINE

    def rows(self) -> list[tuple[str, str]]:
        """Pares (campo, texto) en el orden en que se presentan en la ficha."""
        ind = self.indicator
        sheet = ind.sheet
        rows = [
            ("Código", ind.code),
            ("Indicador", ind.name),
            ("Programa", self.program.name),
            ("Qué mide", sheet.measures),
            ("Numerador", sheet.numerator),
            ("Denominador", sheet.denominator),
            ("Fuente", sheet.source),
            ("Qué significa un cero", sheet.zero_meaning),
            ("Acumulación del numerador", ind.aggregation.label),
            ("Tipo de denominador", ind.denominator_type.label),
            ("Dirección", ind.direction.label),
            ("Escala", ind.scale.label),
            ("Frecuencia", ind.frequency_label),
        ]
        if ind.multiplier != 1.0:
            rows.append(("Multiplicador k", format_decimal(ind.multiplier, 0)))
        if ind.stock_months:
            rows.append(("Meses de corte del stock", join_months(ind.stock_months)))
        if ind.cut_months:
            rows.append(("Cortes de evaluación", join_months(ind.cut_months)))
        rows.append(
            ("Techo natural", format_value(ind.natural_ceiling, ind.scale) if ind.natural_ceiling else "Sin techo")
        )
        rows.append((f"Meta {self.year}", self.rule_description))
        rows.append((f"Peso {self.year}", "-" if self.weight is None else f"{format_decimal(self.weight * 100, 2)} %"))
        if self.site_goals:
            goals = "; ".join(f"{name}: {format_decimal(target, 0)}" for name, target in self.site_goals)
            rows.append((f"Metas fijas por sede {self.year}", goals))
        if sheet.notes:
            rows.append(("Notas", sheet.notes))
        return rows
