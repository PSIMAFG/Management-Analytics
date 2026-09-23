"""Reporte ejecutivo del período en Excel y PDF."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from kpi_monitor import ORGANIZATION_NAME
from kpi_monitor.data.excel_report import write_excel_report
from kpi_monitor.data.pdf_report import write_pdf_report
from kpi_monitor.domain.models import Period
from kpi_monitor.domain.progress import ProgressFn
from kpi_monitor.domain.quality import assess_quality
from kpi_monitor.domain.reporting import ReportData
from kpi_monitor.errors import OperationCancelledError
from kpi_monitor.services.catalog_service import CatalogService
from kpi_monitor.services.monitor_service import MonitorService

log = logging.getLogger(__name__)


class ReportService:
    """Arma el contenido del reporte desde la evaluación y lo escribe en el formato pedido."""

    def __init__(self, monitor: MonitorService, catalog: CatalogService) -> None:
        self.monitor = monitor
        self.catalog_service = catalog

    @staticmethod
    def default_filename(period: Period, extension: str) -> str:
        """Nombre sugerido: reporte_indicadores_2026_08.xlsx."""
        return f"reporte_indicadores_{period.year}_{period.month:02d}.{extension.lstrip('.')}"

    def build(
        self, period: Period, progress: ProgressFn | None = None, cancel: threading.Event | None = None
    ) -> ReportData:
        """Contenido del reporte: evaluación del período, calidad de datos y parámetros vigentes."""
        evaluation = self.monitor.evaluate(period, progress, cancel)
        return ReportData(
            organization=ORGANIZATION_NAME,
            evaluation=evaluation,
            quality=assess_quality(evaluation),
            settings=self.catalog_service.settings(),
        )

    def _prepare(
        self, period: Period, progress: ProgressFn | None, cancel: threading.Event | None
    ) -> tuple[ReportData, ProgressFn | None]:
        def first_half(pct: int, message: str) -> None:
            if progress is not None:
                progress(pct * 40 // 100, message)

        def second_half(pct: int, message: str) -> None:
            if cancel is not None and cancel.is_set():
                raise OperationCancelledError("La exportación fue cancelada.")
            if progress is not None:
                progress(40 + pct * 60 // 100, message)

        report = self.build(period, first_half, cancel)
        if cancel is not None and cancel.is_set():
            raise OperationCancelledError("La exportación fue cancelada.")
        return report, second_half

    def export_excel(
        self, period: Period, path: Path, progress: ProgressFn | None = None, cancel: threading.Event | None = None
    ) -> Path:
        """Libro Excel: Resumen, Indicadores, Mensual, una hoja por sede, Supuestos y Calidad de datos."""
        report, step = self._prepare(period, progress, cancel)
        write_excel_report(path, report, step)
        log.info("Reporte Excel guardado en %s", path)
        return path

    def export_pdf(
        self, period: Period, path: Path, progress: ProgressFn | None = None, cancel: threading.Event | None = None
    ) -> Path:
        """PDF de 4 páginas: resumen, matriz de semáforo, cumplimiento por indicador y comparación entre sedes."""
        report, step = self._prepare(period, progress, cancel)
        write_pdf_report(path, report, step)
        log.info("Reporte PDF guardado en %s", path)
        return path
