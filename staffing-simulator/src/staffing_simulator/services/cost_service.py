"""Casos de uso de costeo: proyección, indicadores, estructura financiera y comparación de escenarios."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol, TypeVar

from staffing_simulator.data.repositories import (
    CatalogRepository,
    ExecutionRepository,
    FinancialRepository,
    ParameterRepository,
    ScenarioRepository,
)
from staffing_simulator.domain.budget import ItemReport, item_report
from staffing_simulator.domain.comparison import ScenarioComparison, compare_projections
from staffing_simulator.domain.models import BudgetItem, Position, PositionDraft, Scenario
from staffing_simulator.domain.projection import Measure, PositionCost, Projection, project_scenario
from staffing_simulator.domain.validation import validate_position_draft
from staffing_simulator.errors import NotFoundError, ValidationError
from staffing_simulator.services.common import Progress, ProgressCallback, ServiceBase


class _Identified(Protocol):
    @property
    def id(self) -> int: ...


ItemT = TypeVar("ItemT", bound=_Identified)


def _find(items: Sequence[ItemT], item_id: int, message: str) -> ItemT:
    """Elemento del catálogo con el identificador pedido o NotFoundError con el mensaje indicado."""
    for item in items:
        if item.id == item_id:
            return item
    raise NotFoundError(message)


@dataclass(frozen=True)
class ScenarioKpis:
    """Totales que la interfaz muestra siempre visibles para el escenario seleccionado.

    - `position_records`: posiciones vigentes en el año, sumando cantidades (incluye tramos).
    - `assigned_total`: suma de los montos asignados de los ítems presupuestarios del año.
    - `human_resources_cost`: costo proyectado de las posiciones (recurso humano).
    - `other_expenses_total`: total planificado de los otros gastos del escenario.
    - `balance_total`: asignado - planificado (recurso humano + otros gastos).
    """

    scenario: Scenario
    total_cost: int
    average_monthly_cost: int
    peak_month: int
    peak_month_cost: int
    headcount: int
    persons: int
    vacancies: int
    position_records: int
    weekly_hours: Decimal
    assigned_total: int
    human_resources_cost: int
    other_expenses_total: int
    balance_total: int
    items_without_assignment: int
    deficit_items: int
    fee_gross_total: int
    retention_total: int
    fee_net_total: int
    employer_contribution_total: int
    absence_discount_total: int


class CostService(ServiceBase):
    """Proyección de costos, estructura financiera y comparación de uno o varios escenarios."""

    def _projection(
        self,
        repo: ScenarioRepository,
        params: ParameterRepository,
        scenario_id: int,
        program_id: int | None = None,
    ) -> Projection:
        scenario = repo.get(scenario_id)
        positions = repo.positions(scenario_id)
        if program_id is not None:
            positions = [position for position in positions if position.program.id == program_id]
        return project_scenario(scenario, positions, params.cost_parameters())

    def project(self, scenario_id: int, program_id: int | None = None) -> Projection:
        """Costo por posición y mes con todos sus agregados. Lanza MissingParameterError si falta una tarifa.

        Con `program_id` solo se proyectan las posiciones imputadas a ese convenio.
        """
        with self._connection() as conn:
            return self._projection(ScenarioRepository(conn), ParameterRepository(conn), scenario_id, program_id)

    def item_report(
        self, scenario_id: int, projection: Projection | None = None, program_id: int | None = None
    ) -> ItemReport:
        """Asignado, planificado y ejecutado por ítem presupuestario de los programas del año del escenario.

        `projection` permite reutilizar una proyección ya calculada del mismo escenario (debe haberse
        calculado con el mismo `program_id`). Con `program_id` solo se incluyen los ítems de ese convenio.
        """
        with self._connection() as conn:
            current = projection
            if current is None:
                current = self._projection(ScenarioRepository(conn), ParameterRepository(conn), scenario_id, program_id)
            year = current.scenario.year
            financial = FinancialRepository(conn)
            items = financial.items(program_id=program_id, year=year)
            expenses = financial.other_expenses(scenario_id)
            if program_id is not None:
                expenses = [expense for expense in expenses if expense.budget_item.program.id == program_id]
            execution = ExecutionRepository(conn).records(year)
        return item_report(items, current, expenses, execution)

    def kpis(
        self, scenario_id: int, projection: Projection | None = None, program_id: int | None = None
    ) -> ScenarioKpis:
        """Indicadores del escenario; `projection` permite reutilizar una proyección del mismo escenario.

        Con `program_id` los indicadores quedan acotados a ese convenio.
        """
        current = self.project(scenario_id, program_id) if projection is None else projection
        report = self.item_report(scenario_id, current, program_id)
        monthly = current.monthly()
        peak_index = max(range(12), key=monthly.__getitem__)
        staffing = current.staffing()
        deficit = sum(1 for line in report.lines if line.balance < 0)
        without_assignment = sum(1 for line in report.lines if line.assigned == 0 and line.planned_total > 0)
        return ScenarioKpis(
            scenario=current.scenario,
            total_cost=current.total_cost,
            average_monthly_cost=current.average_monthly_cost,
            peak_month=peak_index + 1,
            peak_month_cost=monthly[peak_index],
            headcount=staffing.headcount,
            persons=staffing.persons,
            vacancies=staffing.vacancies,
            position_records=staffing.position_records,
            weekly_hours=staffing.weekly_hours,
            assigned_total=report.total_assigned,
            human_resources_cost=report.total_planned_hr,
            other_expenses_total=report.total_planned_other,
            balance_total=report.total_balance,
            items_without_assignment=without_assignment,
            deficit_items=deficit,
            fee_gross_total=current.fee_gross_total,
            retention_total=current.retention_total,
            fee_net_total=current.fee_net_total,
            employer_contribution_total=current.total(Measure.EMPLOYER_CONTRIBUTION),
            absence_discount_total=current.total(Measure.ABSENCE_DISCOUNT),
        )

    def compare(
        self,
        scenario_ids: Sequence[int],
        base_id: int | None = None,
        program_id: int | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> ScenarioComparison:
        """Compara dos o más escenarios contra el base (por defecto, el primero de la lista).

        Con `program_id` la comparación queda acotada a las posiciones de ese convenio.
        """
        ids = list(dict.fromkeys(scenario_ids))
        if len(ids) < 2:
            raise ValidationError("Seleccione al menos dos escenarios para comparar.")
        base = ids[0] if base_id is None else base_id
        if base not in ids:
            raise ValidationError("El escenario base debe estar entre los escenarios seleccionados.")
        tracker = Progress(progress, cancel)
        projections: list[Projection] = []
        with self._connection() as conn:
            repo, params = ScenarioRepository(conn), ParameterRepository(conn)
            for index, scenario_id in enumerate(ids):
                tracker.report(int(index * 90 / len(ids)), f"Proyectando escenario {index + 1} de {len(ids)}")
                projections.append(self._projection(repo, params, scenario_id, program_id))
        tracker.report(95, "Calculando diferencias")
        result = compare_projections(projections, ids.index(base))
        tracker.finish("Comparación lista")
        return result

    def preview_position(self, scenario_id: int, draft: PositionDraft) -> PositionCost:
        """Costo por mes y anual de una posición sin guardarla (vista previa del formulario).

        Valida el borrador con las mismas reglas que al guardar y lanza
        ValidationError o MissingParameterError con el motivo si no se puede costear.
        """
        with self._connection() as conn:
            scenario = ScenarioRepository(conn).get(scenario_id)
            catalog = CatalogRepository(conn).catalog()
            params = ParameterRepository(conn).cost_parameters()
            financial = FinancialRepository(conn)
            budget_item_id = draft.budget_item_id
            if budget_item_id is None:
                default = financial.default_human_resources_item(draft.program_id, scenario.year)
                budget_item_id = None if default is None else default.id
            budget_item: BudgetItem | None = None if budget_item_id is None else financial.item(budget_item_id)

        contract_type = _find(catalog.contract_types, draft.contract_type_id, "El tipo de contrato no existe.")
        job_role = _find(catalog.job_roles, draft.job_role_id, "El cargo seleccionado no existe.")
        site = _find(catalog.sites, draft.site_id, "La sede seleccionada no existe.")
        program = _find(catalog.programs, draft.program_id, "El programa seleccionado no existe.")
        person = None
        if draft.person_id is not None:
            person = _find(catalog.persons, draft.person_id, "La persona seleccionada no existe.")
        valid = validate_position_draft(replace(draft, budget_item_id=budget_item_id), contract_type, scenario.year)
        if budget_item is None:
            raise ValidationError(
                "El programa no tiene ítems de recurso humano en ese año: cárguelos en Estructura del programa."
            )
        position = Position(
            id=0,
            scenario_id=scenario.id,
            job_role=job_role,
            contract_type=contract_type,
            site=site,
            program=program,
            budget_item=budget_item,
            start_date=valid.start_date,
            end_date=valid.end_date,
            person=person,
            weekly_minutes=valid.weekly_minutes,
            monthly_minutes=valid.monthly_minutes,
            quantity=valid.quantity,
            grade=valid.grade,
            note=valid.note,
        )
        return project_scenario(scenario, (position,), params).position_costs()[0]
