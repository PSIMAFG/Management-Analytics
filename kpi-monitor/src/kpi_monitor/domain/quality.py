"""Calidad de datos del período: meses no informados y anomalías detectadas."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from kpi_monitor.domain.alerts import Alert
from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.engine import PeriodEvaluation
from kpi_monitor.domain.enums import AlertKind, Status
from kpi_monitor.domain.models import Indicator, Site

ANOMALY_KINDS = (AlertKind.UNDERREPORTING, AlertKind.IMPOSSIBLE_VALUE, AlertKind.MISSING_DENOMINATOR)


@dataclass(frozen=True)
class MissingReport:
    """Mes en que una sede debía informar un indicador y no lo hizo."""

    site_code: str
    site_name: str
    indicator_code: str
    indicator_name: str
    month: int


@dataclass(frozen=True)
class DataQuality:
    """Resumen de completitud y anomalías del año hasta el mes de corte."""

    expected: int
    reported: int
    missing: tuple[MissingReport, ...]
    anomalies: tuple[Alert, ...]

    @property
    def completeness(self) -> float | None:
        """Fracción de los datos esperados que fueron informados."""
        return self.reported / self.expected if self.expected else None


def expected_reports(catalog: Catalog, year: int, last_month: int) -> Iterator[tuple[Indicator, Site, int]]:
    """Meses en que cada sede debía informar cada indicador vigente del año, hasta `last_month`.

    Es la misma definición que usa la evaluación: todos los meses en los numeradores
    de flujo, solo los meses de corte en los de stock, y nunca en una sede donde el
    indicador no aplica (meta fija sin asignar).
    """
    for indicator in catalog.active_indicators(year):
        for site in catalog.ordered_sites():
            if not catalog.is_applicable(indicator, year, site.code):
                continue
            for month in range(1, last_month + 1):
                if indicator.expects_data_in(month):
                    yield indicator, site, month


def assess_quality(
    evaluation: PeriodEvaluation, site_code: str | None = None, program_code: str | None = None
) -> DataQuality:
    """Cuenta los datos esperados, informados y faltantes del año hasta el mes de corte.

    Con `site_code` se cuentan solo los datos de esa sede (None = todas las sedes) y
    con `program_code`, solo los indicadores de ese programa; las anomalías siguen
    el mismo alcance, para que los totales cuadren con lo que se está mirando.
    """
    expected = 0
    reported = 0
    missing: list[MissingReport] = []
    for indicator_eval in evaluation.evaluations:
        if program_code is not None and indicator_eval.indicator.program_code != program_code:
            continue
        for result in indicator_eval.sites:
            if result.status is Status.NOT_APPLICABLE or result.site_code is None:
                continue
            if site_code is not None and result.site_code != site_code:
                continue
            for point in result.monthly[: evaluation.period.month]:
                if not point.expected:
                    continue
                expected += 1
                if point.reported is True:
                    reported += 1
                    continue
                missing.append(
                    MissingReport(
                        site_code=result.site_code,
                        site_name=result.site_name,
                        indicator_code=result.indicator.code,
                        indicator_name=result.indicator.short_name,
                        month=point.month,
                    )
                )
    levels = None if site_code is None else [site_code]
    anomalies = tuple(a for a in evaluation.alerts_for(program_code, levels) if a.kind in ANOMALY_KINDS)
    return DataQuality(expected=expected, reported=reported, missing=tuple(missing), anomalies=anomalies)
