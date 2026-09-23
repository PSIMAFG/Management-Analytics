"""Punto de acceso único de la interfaz a los casos de uso.

La ventana recibe un `AppServices` ya armado y solo habla con estos servicios
(y con los modelos del dominio que ellos devuelven), nunca con la capa de datos.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from receipt_reader.data.ocr import OcrEngine
from receipt_reader.paths import AppPaths
from receipt_reader.services.catalog import CatalogService
from receipt_reader.services.context import Clock, system_clock
from receipt_reader.services.processing import ProcessingService
from receipt_reader.services.reports import ReportService
from receipt_reader.services.review import ReviewService


@dataclass(frozen=True)
class AppServices:
    """Servicios de la aplicación sobre una misma base de datos."""

    paths: AppPaths
    processing: ProcessingService
    review: ReviewService
    reports: ReportService
    catalog: CatalogService

    @classmethod
    def create(cls, paths: AppPaths, *, ocr: OcrEngine | None = None, clock: Clock = system_clock) -> AppServices:
        """Arma los servicios. `ocr` permite inyectar un motor distinto de RapidOCR (pruebas)."""
        db_path = paths.db_path
        return cls(
            paths=paths,
            processing=ProcessingService(db_path, ocr=ocr, clock=clock),
            review=ReviewService(db_path, clock=clock),
            reports=ReportService(db_path, clock=clock),
            catalog=CatalogService(db_path, clock=clock),
        )

    @property
    def samples_dir(self) -> Path:
        """Carpeta de entrada por defecto (archivos de muestra para procesar con OCR)."""
        return self.paths.samples_dir

    @property
    def export_dir(self) -> Path:
        """Carpeta por defecto de los informes Excel."""
        return self.paths.export_dir
