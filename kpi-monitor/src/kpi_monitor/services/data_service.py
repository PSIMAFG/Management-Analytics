"""Datos de entrada: consulta de observaciones, importación validada y exportaciones Excel."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kpi_monitor.data.db import open_db, transaction
from kpi_monitor.data.excel_io import read_observation_file, write_observations, write_template
from kpi_monitor.data.repositories import ObservationRepository
from kpi_monitor.domain.enums import Origin
from kpi_monitor.domain.importing import RawRow, RejectedRow, validate_rows
from kpi_monitor.domain.models import Observation
from kpi_monitor.domain.progress import ProgressFn
from kpi_monitor.domain.text import format_int, period_label, plural
from kpi_monitor.errors import OperationCancelledError, ValidationError
from kpi_monitor.services.catalog_service import CatalogService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObservationRow:
    """Observación con nombres legibles para la tabla de datos."""

    indicator_code: str
    indicator_name: str
    program_code: str
    site_code: str
    site_name: str
    year: int
    month: int
    period: str
    numerator: float | None
    denominator: float | None
    reported: bool
    origin: str


@dataclass(frozen=True)
class ImportReport:
    """Resultado de una importación: filas leídas, aplicadas y rechazadas con su motivo."""

    total_rows: int
    valid_rows: int
    inserted: int
    replaced: int
    rejected: tuple[RejectedRow, ...]
    applied: bool

    @property
    def message(self) -> str:
        """Resumen en una frase para mostrar al usuario."""
        if self.total_rows == 0:
            return "El archivo no tiene filas con datos."
        if self.applied:
            inserted = plural(self.inserted, "nueva", "nuevas")
            replaced = plural(self.replaced, "reemplazada", "reemplazadas")
            text = f"Se importaron {plural(self.inserted + self.replaced, 'fila', 'filas')} ({inserted} y {replaced})."
            if self.rejected:
                text += f" Se omitieron {plural(len(self.rejected), 'fila', 'filas')} con errores."
            return text
        if self.rejected:
            return (
                f"No se importó nada: {format_int(len(self.rejected))} de {format_int(self.total_rows)} filas "
                "tienen errores. Corrija el archivo o importe solo las filas válidas."
            )
        return f"El archivo es válido: {plural(self.valid_rows, 'fila lista', 'filas listas')} para importar."


class DataService:
    """Importa y exporta observaciones. Tras un cambio avisa para recalcular las evaluaciones."""

    def __init__(self, db_path: Path, catalog: CatalogService, on_change: Callable[[], None] | None = None) -> None:
        self.db_path = db_path
        self.catalog_service = catalog
        self.on_change = on_change

    def observations(
        self,
        year: int,
        month: int | None = None,
        site_code: str | None = None,
        indicator_code: str | None = None,
        program_code: str | None = None,
    ) -> list[ObservationRow]:
        """Observaciones de un año (y opcionalmente de un mes, sede, indicador o programa)."""
        catalog = self.catalog_service.catalog()
        codes: list[str] | None = None
        if indicator_code is not None:
            codes = [indicator_code]
        elif program_code is not None:
            codes = [i.code for i in catalog.indicators if i.program_code == program_code]
        with open_db(self.db_path) as conn:
            observations = ObservationRepository(conn).find(
                years=[year], month=month, site_code=site_code, indicator_codes=codes
            )
        rows = []
        for obs in observations:
            indicator = catalog.indicator(obs.indicator_code)
            rows.append(
                ObservationRow(
                    indicator_code=obs.indicator_code,
                    indicator_name=indicator.short_name,
                    program_code=indicator.program_code,
                    site_code=obs.site_code,
                    site_name=catalog.site(obs.site_code).name,
                    year=obs.year,
                    month=obs.month,
                    period=period_label(obs.year, obs.month),
                    numerator=obs.numerator,
                    denominator=obs.denominator,
                    reported=obs.reported,
                    origin=obs.origin.label,
                )
            )
        return rows

    def validate_file(self, path: Path) -> ImportReport:
        """Valida un archivo sin escribir nada en la base."""
        validation = validate_rows(read_observation_file(path), self.catalog_service.catalog())
        return ImportReport(
            total_rows=validation.total_rows,
            valid_rows=len(validation.accepted),
            inserted=0,
            replaced=0,
            rejected=validation.rejected,
            applied=False,
        )

    def import_file(
        self,
        path: Path,
        *,
        accept_valid_only: bool = False,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> ImportReport:
        """Importa observaciones en formato largo.

        Todas las filas se validan antes de escribir. Si hay filas con errores no
        se importa nada, salvo que `accept_valid_only` sea verdadero; en ese caso
        se importan las filas válidas. La escritura es una sola transacción: nunca
        queda una importación a medias.
        """
        if progress is not None:
            progress(10, "Leyendo el archivo")
        rows = read_observation_file(path)
        if cancel is not None and cancel.is_set():
            raise OperationCancelledError("La importación fue cancelada.")
        if progress is not None:
            progress(40, "Validando filas")
        validation = validate_rows(rows, self.catalog_service.catalog(), Origin.IMPORTED)
        if cancel is not None and cancel.is_set():
            raise OperationCancelledError("La importación fue cancelada.")
        can_apply = bool(validation.accepted) and (validation.is_clean or accept_valid_only)
        inserted = replaced = 0
        if can_apply:
            if progress is not None:
                progress(70, "Guardando en la base de datos")
            loaded_at = datetime.now().replace(microsecond=0).isoformat()
            message = (
                "No se pudieron guardar los datos importados; no se aplicó ningún cambio. "
                "Cierre otras ventanas de la aplicación y vuelva a intentarlo."
            )
            with open_db(self.db_path, message) as conn, transaction(conn):
                inserted, replaced = ObservationRepository(conn).upsert_many(validation.accepted, loaded_at)
            log.info("Importación de %s: %d nuevas, %d reemplazadas", path.name, inserted, replaced)
            if self.on_change is not None:
                self.on_change()
        report = ImportReport(
            total_rows=validation.total_rows,
            valid_rows=len(validation.accepted),
            inserted=inserted,
            replaced=replaced,
            rejected=validation.rejected,
            applied=can_apply,
        )
        if progress is not None:
            progress(100, report.message)
        return report

    def save_observation(
        self,
        site_code: str,
        indicator_code: str,
        year: int,
        month: int,
        numerator: object,
        denominator: object,
        reported: bool = True,
    ) -> Observation:
        """Registra o corrige el dato de un mes con las mismas validaciones de la importación.

        Los valores pueden venir como número o como texto con formato chileno ("1.234,5").
        El dato queda con origen "carga manual" y reemplaza al anterior del mismo mes.
        Lanza ValidationError con el motivo si el dato no cumple las reglas.
        """
        raw = RawRow(1, site_code, indicator_code, year, month, numerator, denominator, "Sí" if reported else "No")
        validation = validate_rows([raw], self.catalog_service.catalog(), Origin.MANUAL)
        if validation.rejected:
            raise ValidationError(validation.rejected[0].reason)
        observation = validation.accepted[0]
        loaded_at = datetime.now().replace(microsecond=0).isoformat()
        message = "No se pudo guardar el dato en la base. Cierre otras ventanas de la aplicación y vuelva a intentarlo."
        with open_db(self.db_path, message) as conn, transaction(conn):
            ObservationRepository(conn).upsert_many([observation], loaded_at)
        log.info("Dato registrado a mano: %s %s %d-%02d", indicator_code, site_code, year, month)
        if self.on_change is not None:
            self.on_change()
        return observation

    def export_template(self, path: Path) -> Path:
        """Plantilla Excel de importación con instrucciones y códigos válidos."""
        return write_template(path, self.catalog_service.catalog())

    def export_observations(self, path: Path, year: int | None = None) -> Path:
        """Exporta las observaciones (de un año o de todos) en el formato de importación."""
        with open_db(self.db_path) as conn:
            observations = ObservationRepository(conn).find(years=[year] if year is not None else None)
        return write_observations(path, observations, self.catalog_service.catalog())
