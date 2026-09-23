"""Casos de uso de Excel: plantillas, importación de posiciones y otros gastos, y exportación del informe."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from staffing_simulator.data.db import transaction
from staffing_simulator.data.excel_export import ReportContent, write_report
from staffing_simulator.data.excel_import import (
    OTHER_EXPENSE_REQUIRED,
    OTHER_EXPENSE_SHEET,
    POSITION_REQUIRED,
    POSITION_SHEET,
    NewPerson,
    OtherExpenseParseResult,
    PositionParseResult,
    parse_other_expense_rows,
    parse_position_rows,
    read_rows,
    write_other_expenses_template,
    write_positions_template,
)
from staffing_simulator.data.repositories import (
    CatalogRepository,
    ExecutionRepository,
    FinancialRepository,
    ParameterRepository,
    ScenarioRepository,
)
from staffing_simulator.domain.budget import item_report
from staffing_simulator.domain.comparison import MAX_COMPARED, compare_projections
from staffing_simulator.domain.imports import ImportReport
from staffing_simulator.domain.projection import Projection, project_scenario
from staffing_simulator.domain.validation import scenario_workload_warnings, validate_position_draft, validate_year
from staffing_simulator.errors import ValidationError
from staffing_simulator.services.common import Progress, ProgressCallback, ServiceBase

log = logging.getLogger(__name__)


class ExcelService(ServiceBase):
    """Intercambio con planillas Excel (openpyxl)."""

    def write_positions_template(self, path: Path, year: int) -> Path:
        """Plantilla de posiciones con listas de cargos, contratos, sedes, programas e ítems válidos."""
        validate_year(year)
        with self._connection() as conn:
            catalog = CatalogRepository(conn).catalog()
            items = FinancialRepository(conn).items(year=year)
        return write_positions_template(Path(path), catalog, items, year)

    def write_other_expenses_template(self, path: Path, year: int) -> Path:
        validate_year(year)
        with self._connection() as conn:
            items = FinancialRepository(conn).items(year=year)
        return write_other_expenses_template(Path(path), items, year)

    def _parse(self, scenario_id: int, path: Path) -> PositionParseResult:
        rows = read_rows(Path(path), POSITION_SHEET, POSITION_REQUIRED)
        with self._connection() as conn:
            scenarios = ScenarioRepository(conn)
            scenario = scenarios.get(scenario_id)
            existing = scenarios.positions(scenario_id)
            catalog = CatalogRepository(conn).catalog()
            items = FinancialRepository(conn).items(year=scenario.year)
            params = ParameterRepository(conn).cost_parameters()
        return parse_position_rows(rows, catalog, items, scenario.year, params, existing)

    def preview_positions(self, scenario_id: int, path: Path) -> ImportReport:
        """Valida la planilla sin guardar nada; informa filas válidas y rechazadas."""
        parsed = self._parse(scenario_id, path)
        return ImportReport(
            total_rows=parsed.total_rows,
            accepted=len(parsed.positions),
            rejected=parsed.rejected,
            applied=False,
            created_persons=len(parsed.new_persons),
        )

    def import_positions(
        self,
        scenario_id: int,
        path: Path,
        only_valid: bool = False,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> ImportReport:
        """Agrega las posiciones de la planilla al escenario.

        Si alguna fila tiene errores no se guarda nada, salvo que se pida
        importar solo las filas válidas (`only_valid=True`). Las personas nuevas
        y las posiciones se guardan en una sola transacción: ante cualquier
        falla o cancelación no queda nada a medias.
        """
        tracker = Progress(progress, cancel)
        tracker.report(5, "Leyendo la planilla")
        parsed = self._parse(scenario_id, path)
        tracker.report(35, "Validando filas")
        apply = bool(parsed.positions) and (only_valid or not parsed.rejected)
        created = 0
        if apply:
            with self._connection() as conn, transaction(conn):
                catalog = CatalogRepository(conn)
                scenarios = ScenarioRepository(conn)
                scenario = scenarios.get(scenario_id)
                contract_types = {item.id: item for item in catalog.contract_types()}
                person_ids: dict[NewPerson, int] = {}
                for new_person in parsed.new_persons:
                    if new_person.rut is not None and catalog.find_person_by_rut(new_person.rut) is not None:
                        raise ValidationError(f"El RUT de {new_person.full_name} ya fue registrado. Vuelva a intentar.")
                    person_ids[new_person] = catalog.add_person(new_person.full_name, new_person.rut).id
                    created += 1
                total = len(parsed.positions)
                for index, item in enumerate(parsed.positions, start=1):
                    if index % 10 == 0:
                        tracker.report(35 + int(index * 60 / total), f"Guardando posición {index} de {total}")
                    draft = item.draft
                    if item.new_person is not None:
                        draft = replace(draft, person_id=person_ids[item.new_person])
                    valid = validate_position_draft(draft, contract_types[draft.contract_type_id], scenario.year)
                    scenarios.insert_position(scenario_id, valid)
                scenarios.touch(scenario_id, self._now())
                # Último punto en que cancelar todavía revierte todo: después se confirma la transacción.
                tracker.check()
        tracker.finish("Importación terminada")
        report = ImportReport(
            total_rows=parsed.total_rows,
            accepted=len(parsed.positions),
            rejected=parsed.rejected,
            applied=apply,
            created_persons=created,
        )
        log.info("Importación de posiciones: %s", report.summary)
        return report

    def _parse_expenses(self, scenario_id: int, path: Path) -> OtherExpenseParseResult:
        rows = read_rows(Path(path), OTHER_EXPENSE_SHEET, OTHER_EXPENSE_REQUIRED)
        with self._connection() as conn:
            scenario = ScenarioRepository(conn).get(scenario_id)
            items = FinancialRepository(conn).items(year=scenario.year)
        return parse_other_expense_rows(rows, scenario_id, items, scenario.year)

    def preview_other_expenses(self, scenario_id: int, path: Path) -> ImportReport:
        parsed = self._parse_expenses(scenario_id, path)
        return ImportReport(
            total_rows=parsed.total_rows, accepted=len(parsed.expenses), rejected=parsed.rejected, applied=False
        )

    def import_other_expenses(
        self,
        scenario_id: int,
        path: Path,
        only_valid: bool = False,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> ImportReport:
        """Agrega los otros gastos de la planilla al escenario, en una sola transacción."""
        tracker = Progress(progress, cancel)
        tracker.report(5, "Leyendo la planilla")
        parsed = self._parse_expenses(scenario_id, path)
        tracker.report(35, "Validando filas")
        apply = bool(parsed.expenses) and (only_valid or not parsed.rejected)
        if apply:
            with self._connection() as conn, transaction(conn):
                repo = FinancialRepository(conn)
                total = len(parsed.expenses)
                for index, item in enumerate(parsed.expenses, start=1):
                    if index % 10 == 0:
                        tracker.report(35 + int(index * 60 / total), f"Guardando gasto {index} de {total}")
                    repo.add_other_expense(item.draft)
                tracker.check()
        tracker.finish("Importación terminada")
        report = ImportReport(
            total_rows=parsed.total_rows, accepted=len(parsed.expenses), rejected=parsed.rejected, applied=apply
        )
        log.info("Importación de otros gastos: %s", report.summary)
        return report

    def export_report(
        self,
        scenario_id: int,
        path: Path,
        compare_ids: Sequence[int] = (),
        base_id: int | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """Informe completo del escenario; si se indican otros escenarios, agrega la comparación.

        La comparación incluye el escenario exportado y los de `compare_ids`
        (como máximo MAX_COMPARED en total) contra `base_id`, el mismo base que
        muestra la pestaña de comparación; si no se indica, el base es el
        escenario exportado.
        """
        tracker = Progress(progress, cancel)
        tracker.report(2, "Calculando la proyección")
        others = [item for item in dict.fromkeys(compare_ids) if item != scenario_id]
        if others and len(others) + 1 > MAX_COMPARED:
            raise ValidationError(
                f"El informe puede comparar como máximo {MAX_COMPARED} escenarios. "
                "Desmarque alguno en el panel lateral e intente nuevamente."
            )
        with self._connection() as conn:
            repo = ScenarioRepository(conn)
            params = ParameterRepository(conn).cost_parameters()
            catalog = CatalogRepository(conn).catalog()
            scenario = repo.get(scenario_id)
            positions = repo.positions(scenario_id)
            projection = project_scenario(scenario, positions, params)
            financial = FinancialRepository(conn)
            items = financial.items(year=scenario.year)
            other_expenses = financial.other_expenses(scenario_id)
            execution = ExecutionRepository(conn).records(scenario.year)
            compared: list[Projection] = [projection]
            for other_id in others:
                tracker.check()
                compared.append(project_scenario(repo.get(other_id), repo.positions(other_id), params))
        tracker.report(10, "Preparando el informe")
        compared_ids = [item.scenario.id for item in compared]
        base_index = compared_ids.index(base_id) if base_id in compared_ids else 0
        content = ReportContent(
            projection=projection,
            params=params,
            catalog=catalog,
            items=item_report(items, projection, other_expenses, execution),
            other_expenses=tuple(other_expenses),
            workload=scenario_workload_warnings(positions, params, scenario.year),
            generated_at=self._now(),
            comparison=compare_projections(compared, base_index) if len(compared) > 1 else None,
        )

        def on_step(percent: int, message: str) -> None:
            tracker.report(10 + int(percent * 0.9), message)

        result = write_report(Path(path), content, on_step)
        # El archivo ya quedó escrito: desde aquí cancelar no puede deshacer nada.
        tracker.finish("Informe guardado")
        log.info("Informe exportado: %s", result)
        return result
