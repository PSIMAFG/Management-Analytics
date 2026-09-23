"""Base de ejemplo y archivos de muestra para la primera ejecución.

La base se puebla procesando un lote sintético con el mismo flujo de un lote
real (lectura, clasificación de páginas, extracción, validación y registro),
pero sin ejecutar el OCR: para las páginas rasterizadas se usan las cajas de
texto que el generador conoce de antemano. Así la primera ejecución es rápida
y los campos, orígenes, incidencias y estados son los que produciría el
procesamiento real.

Después se aplican unas pocas acciones de revisión de ejemplo (nombres
canónicos confirmados, una copia descartada y un folio corregido) y se
escriben los archivos de `muestras`, pensados para procesarlos con el OCR
real desde la interfaz.

Todo es reproducible: semilla fija y reloj fijo. La base se construye en un
archivo temporal y solo reemplaza al definitivo al terminar, de modo que una
interrupción no deja una base a medio poblar.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from receipt_reader.data import db
from receipt_reader.data.ocr import SyntheticOcrEngine
from receipt_reader.data.seed import seed_catalog
from receipt_reader.data.synthetic import SEED, DemoLot, SyntheticDocument, build_demo_lot, build_sample_set
from receipt_reader.data.synthetic_render import write_documents
from receipt_reader.domain.reporting import ReceiptFilter
from receipt_reader.errors import DataError
from receipt_reader.paths import AppPaths
from receipt_reader.services.catalog import CatalogService
from receipt_reader.services.processing import BatchResult, ProcessingService
from receipt_reader.services.reports import ReportService
from receipt_reader.services.review import ReviewService

log = logging.getLogger(__name__)

# Resolución de los documentos rasterizados del lote (solo se usan para la vista previa).
DEMO_DPI = 150
# Resolución de las muestras, que sí se leen con el OCR real.
SAMPLE_DPI = 200
DEMO_TIMESTAMP = datetime(2026, 7, 20, 9, 30)
DISCARD_REASON = "Copia exacta de un archivo ya registrado (reenvío del prestador)."
SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True)
class DemoResult:
    """Resumen de la base de ejemplo creada."""

    batch: BatchResult
    confirmed_providers: int
    corrected: int
    discarded: int
    sample_files: int


def demo_clock() -> datetime:
    """Reloj fijo: las marcas de tiempo de la base de ejemplo no dependen del día de ejecución."""
    return DEMO_TIMESTAMP


def remove_database(db_path: Path) -> None:
    """Borra el archivo de la base y sus archivos auxiliares de SQLite."""
    for candidate in (db_path, *(db_path.with_name(db_path.name + suffix) for suffix in SQLITE_SIDE_FILES)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            raise DataError(
                f"No se pudo borrar {candidate.name}: está en uso. Cierre otras copias de la aplicación."
            ) from error


def _receipt_id_for(reports: ReportService, relative_path: str) -> int | None:
    for row in reports.rows(ReceiptFilter()):
        if row.relative_path == relative_path:
            return row.id
    return None


def _document_with_tag(lot: DemoLot, tag: str) -> SyntheticDocument | None:
    return next((document for document in lot.documents if tag in document.tags), None)


def _apply_review_examples(db_path: Path, lot: DemoLot) -> tuple[int, int, int]:
    """Acciones de revisión de ejemplo; devuelve (nombres confirmados, corregidas, descartadas)."""
    review = ReviewService(db_path, clock=demo_clock)
    reports = ReportService(db_path, clock=demo_clock)
    for provider in lot.providers:
        review.confirm_provider_name(provider.rut, provider.name)
    corrected = discarded = 0
    copy = _document_with_tag(lot, "copia_exacta")
    copy_id = _receipt_id_for(reports, copy.relative_path) if copy else None
    if copy_id is not None:
        review.discard(copy_id, DISCARD_REASON)
        discarded += 1
    missing_folio = _document_with_tag(lot, "folio_ilegible")
    folio_id = _receipt_id_for(reports, missing_folio.relative_path) if missing_folio else None
    if folio_id is not None:
        detail = review.detail(folio_id)
        suggestion = next((item for item in detail.suggestions if item.field == "folio"), None)
        if suggestion is not None:
            review.save_correction(folio_id, {"folio": suggestion.text})
            corrected += 1
    return len(lot.providers), corrected, discarded


def _finalize(db_path: Path) -> None:
    """Deja la base en un solo archivo (sin diario WAL pendiente) antes de moverla."""
    with db.session(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")


def write_samples(samples_dir: Path) -> int:
    """Escribe los archivos de muestra con la convención de carpetas; devuelve cuántos son."""
    documents = build_sample_set()
    write_documents(documents, samples_dir, dpi=SAMPLE_DPI, seed=SEED + 1)
    return len(documents)


def create_demo_database(db_path: Path, lot_dir: Path, samples_dir: Path) -> DemoResult:
    """Crea la base de ejemplo en `db_path` (que no debe existir) y escribe lote y muestras."""
    staging = db_path.with_name(db_path.name + ".nuevo")
    remove_database(staging)
    with db.session(staging) as conn:
        db.initialize(conn)
        seed_catalog(conn)

    lot = build_demo_lot()
    if lot_dir.exists():
        shutil.rmtree(lot_dir)
    ocr_pages = write_documents(lot.documents, lot_dir, dpi=DEMO_DPI, seed=SEED)
    processing = ProcessingService(staging, ocr=SyntheticOcrEngine(ocr_pages), clock=demo_clock)
    batch = processing.process_folder(lot_dir)
    confirmed, corrected, discarded = _apply_review_examples(staging, lot)
    sample_files = write_samples(samples_dir)

    _finalize(staging)
    remove_database(db_path)
    staging.replace(db_path)
    result = DemoResult(batch, confirmed, corrected, discarded, sample_files)
    log.info(
        "Base de ejemplo creada: %s %d nombres confirmados, %d corregidas, %d descartadas, %d muestras.",
        batch.message(),
        confirmed,
        corrected,
        discarded,
        sample_files,
    )
    return result


def ensure_database(paths: AppPaths, *, reset: bool = False) -> DemoResult | None:
    """Deja lista la base de la aplicación.

    Si no existe (o se pide reiniciarla), la crea con los datos de ejemplo y
    devuelve el resumen; si ya existe, la conserva y devuelve None. Una base de
    una versión anterior se actualiza y sus boletas se revalidan con las reglas
    vigentes.
    """
    if reset:
        remove_database(paths.db_path)
        log.info("Base de datos eliminada para regenerarla")
    if paths.db_path.exists():
        with db.session(paths.db_path) as conn:
            previous = db.schema_version(conn)
            created_now = db.initialize(conn)
        if not created_now:
            if 0 < previous < db.SCHEMA_VERSION:
                changed = CatalogService(paths.db_path).revalidate_all()
                log.info("Boletas revalidadas tras actualizar la base: %d cambiaron de estado", changed)
            return None
        # El archivo existía pero estaba vacío: se reemplaza por la base de ejemplo completa.
        remove_database(paths.db_path)
    return create_demo_database(paths.db_path, paths.demo_lot_dir, paths.samples_dir)
