"""Contenido del reporte ejecutivo, armado por el servicio y leído por los escritores Excel y PDF."""

from __future__ import annotations

from dataclasses import dataclass

from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.engine import PeriodEvaluation
from kpi_monitor.domain.models import MonitorSettings, Period
from kpi_monitor.domain.quality import DataQuality


@dataclass(frozen=True)
class ReportData:
    """Todo lo que necesita un reporte: evaluación, catálogo, supuestos y calidad de datos."""

    organization: str
    evaluation: PeriodEvaluation
    quality: DataQuality
    settings: MonitorSettings

    @property
    def period(self) -> Period:
        return self.evaluation.period

    @property
    def catalog(self) -> Catalog:
        return self.evaluation.catalog
