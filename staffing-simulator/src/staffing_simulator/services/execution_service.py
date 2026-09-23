"""Casos de uso de ejecución: registrar montos ejecutados por ítem presupuestario e importarlos desde Excel."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from staffing_simulator.data.db import transaction
from staffing_simulator.data.excel_import import (
    EXECUTION_REQUIRED,
    EXECUTION_SHEET,
    ExecutionParseResult,
    parse_execution_rows,
    read_rows,
    write_execution_template,
)
from staffing_simulator.data.repositories import ExecutionRepository, FinancialRepository
from staffing_simulator.domain.imports import ImportReport
from staffing_simulator.domain.models import ExecutionRecord
from staffing_simulator.domain.validation import validate_year
from staffing_simulator.errors import ValidationError
from staffing_simulator.services.common import Progress, ProgressCallback, ServiceBase

log = logging.getLogger(__name__)


class ExecutionService(ServiceBase):
    """Montos ejecutados por ítem presupuestario y mes, y su importación desde Excel."""

    def records(self, year: int | None = None) -> list[ExecutionRecord]:
        with self._connection() as conn:
            return ExecutionRepository(conn).records(year)

    def years(self) -> list[int]:
        with self._connection() as conn:
            return ExecutionRepository(conn).years()

    def set_amount(self, budget_item_id: int, month: int, amount: int | None) -> None:
        """Registra (o borra, con None) el monto ejecutado de un ítem en un mes."""
        if not 1 <= month <= 12:
            raise ValidationError("El mes debe estar entre 1 y 12.")
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int) or amount < 0):
            raise ValidationError("El monto ejecutado debe ser un número entero de pesos mayor o igual a 0.")
        with self._connection() as conn, transaction(conn):
            FinancialRepository(conn).item(budget_item_id)
            repo = ExecutionRepository(conn)
            if amount is None:
                repo.delete(budget_item_id, month)
            else:
                repo.upsert(budget_item_id, month, amount)

    def write_template(self, path: Path, year: int) -> Path:
        """Plantilla Excel con una fila por ítem presupuestario y mes para completar los montos."""
        validate_year(year)
        with self._connection() as conn:
            items = FinancialRepository(conn).items(year=year)
        return write_execution_template(Path(path), items, year)

    def _parse_file(self, conn: sqlite3.Connection, path: Path) -> tuple[ExecutionParseResult, int]:
        """Filas validadas de la planilla y cuántas reemplazan un monto ya registrado."""
        rows = read_rows(Path(path), EXECUTION_SHEET, EXECUTION_REQUIRED)
        parsed = parse_execution_rows(rows, FinancialRepository(conn).items())
        years = {record.year for _row, record in parsed.records}
        existing = {
            (item.budget_item_id, item.year, item.month)
            for year in years
            for item in ExecutionRepository(conn).records(year)
        }
        replaced = sum(
            1 for _row, record in parsed.records if (record.budget_item_id, record.year, record.month) in existing
        )
        return parsed, replaced

    def preview_file(self, path: Path) -> ImportReport:
        """Valida la planilla sin guardar nada: filas válidas, rechazadas y montos que se reemplazarían."""
        with self._connection() as conn:
            parsed, replaced = self._parse_file(conn, path)
        return ImportReport(
            total_rows=parsed.total_rows,
            accepted=len(parsed.records),
            rejected=parsed.rejected,
            applied=False,
            replaced=replaced,
        )

    def import_file(
        self,
        path: Path,
        only_valid: bool = False,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> ImportReport:
        """Importa montos ejecutados; si hay filas con errores no guarda nada, salvo `only_valid=True`.

        Los montos de un ítem y mes que ya existían se reemplazan. Todo se
        guarda en una sola transacción.
        """
        tracker = Progress(progress, cancel)
        tracker.report(5, "Leyendo la planilla")
        with self._connection() as conn:
            parsed, replaced = self._parse_file(conn, path)
            tracker.report(30, "Validando filas")
            apply = bool(parsed.records) and (only_valid or not parsed.rejected)
            if apply:
                with transaction(conn):
                    repo = ExecutionRepository(conn)
                    for index, (_row, record) in enumerate(parsed.records, start=1):
                        if index % 20 == 0:
                            tracker.report(30 + int(index * 65 / len(parsed.records)), "Guardando montos")
                        repo.upsert(record.budget_item_id, record.month, record.amount)
                    # Último punto en que cancelar todavía revierte todo: después se confirma la transacción.
                    tracker.check()
        tracker.finish("Importación terminada")
        report = ImportReport(
            total_rows=parsed.total_rows,
            accepted=len(parsed.records),
            rejected=parsed.rejected,
            applied=apply,
            replaced=replaced if apply else 0,
        )
        log.info("Importación de ejecución: %s", report.summary)
        return report
