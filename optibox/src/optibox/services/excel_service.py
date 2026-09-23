"""Exportación del plan e importación y exportación de datos maestros en Excel."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from optibox.data import db
from optibox.data.excel_master import MASTER_SHEETS, read_master_workbook, write_master_workbook
from optibox.data.excel_plan import write_plan_workbook
from optibox.data.repository import MasterDataRepository
from optibox.services.planning_service import PlanningService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RejectedRow:
    """Fila rechazada: hoja, número de fila en la planilla (0 si aplica a toda la hoja) y motivo."""

    sheet: str
    row: int
    reason: str


@dataclass(frozen=True)
class ImportReport:
    """Resultado de una importación: se aplica completa o no se aplica."""

    applied: bool
    rows_read: dict[str, int] = field(default_factory=dict)
    rejected: tuple[RejectedRow, ...] = ()

    @property
    def summary(self) -> str:
        if self.applied:
            total = sum(self.rows_read.values())
            sheets = len(self.rows_read)
            return (
                f"Se importaron {total} {'fila' if total == 1 else 'filas'} "
                f"en {sheets} {'hoja' if sheets == 1 else 'hojas'}."
            )
        rejected = len(self.rejected)
        return (
            f"No se importó nada: {rejected} {'fila rechazada' if rejected == 1 else 'filas rechazadas'}. "
            "Corrija la planilla y vuelva a intentarlo."
        )


class ExcelService:
    """Casos de uso de Excel. Los archivos se escriben donde indique el usuario."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.planning = PlanningService(db_path)

    def export_run(self, run_id: int, path: Path) -> Path:
        """Exporta el plan de una corrida (resumen, agendas por persona y sala, cobertura, sin cubrir, carga)."""
        detail = self.planning.load_run(run_id)
        saved = write_plan_workbook(detail, Path(path))
        log.info("Plan de la corrida %d exportado a %s", run_id, saved)
        return saved

    def export_master_data(self, path: Path) -> Path:
        """Exporta los datos maestros con el formato de la plantilla de importación."""
        with db.session(self.db_path) as conn:
            master = MasterDataRepository(conn).load()
        saved = write_master_workbook(master, Path(path))
        log.info("Datos maestros exportados a %s", saved)
        return saved

    def import_master_data(self, path: Path) -> ImportReport:
        """Valida la planilla fila por fila y, solo si no hay rechazos, reemplaza los datos maestros.

        Las corridas guardadas no se modifican: cada una conserva la copia de
        la instancia con que se calculó.
        """
        with db.session(self.db_path) as conn:
            current = MasterDataRepository(conn).load()
        master, errors, counts = read_master_workbook(Path(path), current)
        if master is None:
            rejected = tuple(RejectedRow(e.sheet, e.row, e.reason) for e in errors)
            log.warning("Importación rechazada: %d filas con problemas", len(rejected))
            return ImportReport(applied=False, rows_read=counts, rejected=rejected)
        with db.session(self.db_path) as conn, db.transaction(conn):
            MasterDataRepository(conn).replace_all(master)
        log.info("Datos maestros importados desde %s", path)
        return ImportReport(applied=True, rows_read=counts)

    @staticmethod
    def template_sheets() -> tuple[str, ...]:
        return MASTER_SHEETS
