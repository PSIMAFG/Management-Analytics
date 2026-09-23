"""Capa de servicios: casos de uso que la interfaz consume."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kpi_monitor.services.catalog_service import CatalogService
from kpi_monitor.services.data_service import DataService, ImportReport, ObservationRow
from kpi_monitor.services.monitor_service import MonitorService
from kpi_monitor.services.report_service import ReportService

__all__ = [
    "CatalogService",
    "DataService",
    "ImportReport",
    "MonitorService",
    "ObservationRow",
    "ReportService",
    "Services",
    "build_services",
]


@dataclass(frozen=True)
class Services:
    """Servicios de la aplicación conectados a una misma base."""

    catalog: CatalogService
    monitor: MonitorService
    data: DataService
    reports: ReportService


def build_services(db_path: Path) -> Services:
    """Crea los servicios y enlaza la importación con la invalidación de evaluaciones."""
    catalog = CatalogService(db_path)
    monitor = MonitorService(db_path, catalog)
    data = DataService(db_path, catalog, on_change=monitor.invalidate)
    reports = ReportService(monitor, catalog)
    return Services(catalog=catalog, monitor=monitor, data=data, reports=reports)
