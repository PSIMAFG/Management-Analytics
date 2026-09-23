"""Casos de uso de escenarios y posiciones: crear, duplicar, editar y validar."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from staffing_simulator.data.db import transaction
from staffing_simulator.data.repositories import (
    CatalogRepository,
    FinancialRepository,
    ParameterRepository,
    ScenarioRepository,
)
from staffing_simulator.domain.models import BudgetItemType, Person, Position, PositionDraft, Scenario, ScenarioDraft
from staffing_simulator.domain.parameters import ensure_position_costable
from staffing_simulator.domain.rut import normalize_rut
from staffing_simulator.domain.text import plural
from staffing_simulator.domain.validation import (
    ValidPosition,
    WorkloadWarning,
    find_identical,
    identical_position_message,
    scenario_workload_warnings,
    validate_name,
    validate_position_draft,
    validate_scenario_draft,
)
from staffing_simulator.errors import AppError, NotFoundError, ValidationError
from staffing_simulator.services.common import ServiceBase

log = logging.getLogger(__name__)

COPY_PREFIX = "Copia de "


@dataclass(frozen=True)
class PositionResult:
    """Posición guardada y advertencias no bloqueantes (por ejemplo, jornada de la persona)."""

    position: Position
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioUpdateResult:
    """Escenario actualizado y advertencias no bloqueantes (por ejemplo, posiciones sin vigencia en el año)."""

    scenario: Scenario
    warnings: tuple[str, ...] = ()


def _format_date(value: date) -> str:
    return value.strftime("%d-%m-%Y")


def validity_message(position: Position, year: int) -> str:
    """Aviso de una posición que no tiene vigencia en el año del escenario."""
    end = _format_date(position.end_date) if position.end_date else "sin término"
    return (
        f"{position.job_role.name} ({position.holder_label}, {position.program.name}), vigente del "
        f"{_format_date(position.start_date)} al {end}: no tiene vigencia en {year} y no suma costo."
    )


def inactive_summary(inactive: int, total: int, year: int) -> str:
    """Resumen de las posiciones que no tienen vigencia en el año: '9 de 62 posiciones no tienen...'."""
    verb, cost = ("tiene", "suma") if inactive == 1 else ("tienen", "suman")
    return f"{inactive} de {plural(total, 'posición', 'posiciones')} no {verb} vigencia en {year} y no {cost} costo."


class ScenarioService(ServiceBase):
    """CRUD de escenarios y posiciones con validación de reglas de negocio."""

    def list_scenarios(self) -> list[Scenario]:
        with self._connection() as conn:
            return ScenarioRepository(conn).list_all()

    def get_scenario(self, scenario_id: int) -> Scenario:
        with self._connection() as conn:
            return ScenarioRepository(conn).get(scenario_id)

    def _check_name(self, repo: ScenarioRepository, name: str, exclude_id: int | None = None) -> None:
        if repo.name_taken(name, exclude_id):
            raise ValidationError(f"Ya existe un escenario llamado «{name}». Elija otro nombre.")

    def _check_base(self, repo: ScenarioRepository, scenario_id: int | None, base_id: int | None) -> None:
        if base_id is None:
            return
        try:
            repo.get(base_id)
        except NotFoundError as error:
            raise ValidationError("El escenario base seleccionado no existe.") from error
        if scenario_id is not None and scenario_id in repo.base_chain(base_id):
            raise ValidationError("El escenario base no puede ser el mismo escenario ni uno que dependa de él.")

    def create_scenario(self, draft: ScenarioDraft) -> Scenario:
        clean = validate_scenario_draft(draft)
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            self._check_name(repo, clean.name)
            self._check_base(repo, None, clean.base_scenario_id)
            scenario_id = repo.insert(clean, self._now())
            log.info("Escenario creado: %s", clean.name)
            return repo.get(scenario_id)

    def update_scenario(self, scenario_id: int, draft: ScenarioDraft) -> ScenarioUpdateResult:
        """Edita nombre, descripción y supuestos (año, meses parciales, ausentismo y base).

        Si cambia el año, el resultado advierte cuántas posiciones quedan sin
        vigencia en el año nuevo: no suman costo y el usuario debe saberlo
        (nunca un costo 0 silencioso). La interfaz pide confirmación antes de
        aplicar el cambio con `inactive_positions`.
        """
        clean = validate_scenario_draft(draft)
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            current = repo.get(scenario_id)
            self._check_name(repo, clean.name, scenario_id)
            self._check_base(repo, scenario_id, clean.base_scenario_id)
            repo.update(scenario_id, clean, self._now())
            warnings: tuple[str, ...] = ()
            if clean.year != current.year:
                positions = repo.positions(scenario_id)
                inactive = [item for item in positions if item.period_in_year(clean.year) is None]
                if inactive:
                    summary = inactive_summary(len(inactive), len(positions), clean.year)
                    warnings = (f"{summary} Revise sus fechas en la pestaña Posiciones.",)
                    log.info("Cambio de año de %s: %d posiciones sin vigencia", clean.name, len(inactive))
            return ScenarioUpdateResult(repo.get(scenario_id), warnings)

    def rename_scenario(self, scenario_id: int, name: str) -> Scenario:
        current = self.get_scenario(scenario_id)
        draft = current.to_draft()
        return self.update_scenario(
            scenario_id,
            ScenarioDraft(
                name=name,
                year=draft.year,
                description=draft.description,
                partial_month_method=draft.partial_month_method,
                expected_absence=draft.expected_absence,
                base_scenario_id=draft.base_scenario_id,
            ),
        ).scenario

    def suggest_copy_name(self, scenario_id: int) -> str:
        """Nombre libre del tipo «Copia de X», «Copia de X (2)»..."""
        with self._connection() as conn:
            repo = ScenarioRepository(conn)
            source = repo.get(scenario_id)
            return self._free_copy_name(repo, source.name)

    @staticmethod
    def _free_copy_name(repo: ScenarioRepository, name: str) -> str:
        taken = repo.names()
        base = f"{COPY_PREFIX}{name}"[:72]
        candidate, counter = base, 2
        while candidate.casefold() in taken:
            candidate = f"{base} ({counter})"
            counter += 1
        return candidate

    def duplicate_scenario(self, scenario_id: int, name: str | None = None) -> Scenario:
        """Copia profunda: supuestos y todas las posiciones como filas nuevas e independientes."""
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            source = repo.get(scenario_id)
            new_name = self._free_copy_name(repo, source.name) if name is None else name
            draft = validate_scenario_draft(
                ScenarioDraft(
                    name=new_name,
                    year=source.year,
                    description=source.description,
                    partial_month_method=source.partial_month_method,
                    expected_absence=source.expected_absence,
                    base_scenario_id=source.base_scenario_id,
                )
            )
            self._check_name(repo, draft.name)
            new_id = repo.insert(draft, self._now())
            copied = repo.copy_positions(scenario_id, new_id)
            expenses = FinancialRepository(conn).copy_other_expenses(scenario_id, new_id)
            log.info(
                "Escenario duplicado: %s -> %s (%d posiciones, %d otros gastos)",
                source.name,
                draft.name,
                copied,
                expenses,
            )
            return repo.get(new_id)

    def delete_scenario(self, scenario_id: int) -> None:
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            scenario = repo.get(scenario_id)
            repo.delete(scenario_id)
            log.info("Escenario eliminado: %s", scenario.name)

    def list_positions(self, scenario_id: int, program_id: int | None = None) -> list[Position]:
        """Posiciones del escenario; con `program_id` solo las imputadas a ese convenio."""
        with self._connection() as conn:
            repo = ScenarioRepository(conn)
            repo.get(scenario_id)
            positions = repo.positions(scenario_id)
        if program_id is None:
            return positions
        return [position for position in positions if position.program.id == program_id]

    def get_position(self, position_id: int) -> Position:
        with self._connection() as conn:
            return ScenarioRepository(conn).position(position_id)

    def inactive_positions(self, scenario_id: int, year: int | None = None) -> list[Position]:
        """Posiciones sin ningún día de vigencia en `year` (por defecto, el año del escenario)."""
        with self._connection() as conn:
            repo = ScenarioRepository(conn)
            scenario = repo.get(scenario_id)
            positions = repo.positions(scenario_id)
        target = scenario.year if year is None else year
        return [item for item in positions if item.period_in_year(target) is None]

    def _resolve_budget_item(self, conn: sqlite3.Connection, draft: PositionDraft, scenario_year: int) -> PositionDraft:
        """Completa el ítem de recurso humano con el primero del programa si el borrador no trae uno."""
        financial = FinancialRepository(conn)
        if draft.budget_item_id is None:
            default = financial.default_human_resources_item(draft.program_id, scenario_year)
            if default is None:
                raise ValidationError(
                    "El programa no tiene ítems de recurso humano en ese año: cárguelos en Estructura del "
                    "programa antes de agregar posiciones."
                )
            return replace(draft, budget_item_id=default.id)
        item = financial.item(draft.budget_item_id)
        if item.program.id != draft.program_id:
            raise ValidationError("El ítem presupuestario elegido no pertenece al programa de la posición.")
        if item.year != scenario_year:
            raise ValidationError("El ítem presupuestario elegido no corresponde al año del escenario.")
        if item.item_type is not BudgetItemType.HUMAN_RESOURCES:
            raise ValidationError("Las posiciones se imputan a un ítem de recurso humano.")
        return draft

    def _validate(
        self, conn: sqlite3.Connection, scenario: Scenario, draft: PositionDraft, position_id: int | None = None
    ) -> ValidPosition:
        """Valida la posición y rechaza un registro idéntico a otra posición del escenario (BUG-11)."""
        catalog = CatalogRepository(conn)
        contract_type = catalog.contract_type(draft.contract_type_id)
        role = catalog.job_role(draft.job_role_id)
        if not catalog.exists("site", draft.site_id):
            raise NotFoundError("La sede seleccionada no existe.")
        if not catalog.exists("program", draft.program_id):
            raise NotFoundError("El programa seleccionado no existe.")
        if draft.person_id is not None:
            catalog.person(draft.person_id)
        draft = self._resolve_budget_item(conn, draft, scenario.year)
        valid = validate_position_draft(draft, contract_type, scenario.year)
        params = ParameterRepository(conn).cost_parameters()
        ensure_position_costable(params, role, contract_type, scenario.year)
        duplicate = find_identical(valid, ScenarioRepository(conn).positions(scenario.id), exclude_id=position_id)
        if duplicate is not None:
            raise ValidationError(identical_position_message(duplicate))
        return valid

    def _person_warnings(self, conn: sqlite3.Connection, scenario: Scenario, person_id: int | None) -> tuple[str, ...]:
        if person_id is None:
            return ()
        positions = ScenarioRepository(conn).positions(scenario.id)
        params = ParameterRepository(conn).cost_parameters()
        warnings = scenario_workload_warnings(positions, params, scenario.year)
        return tuple(warning.message for warning in warnings if warning.person.id == person_id)

    def add_position(self, scenario_id: int, draft: PositionDraft) -> PositionResult:
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            scenario = repo.get(scenario_id)
            valid = self._validate(conn, scenario, draft)
            position_id = repo.insert_position(scenario_id, valid)
            repo.touch(scenario_id, self._now())
            warnings = self._person_warnings(conn, scenario, valid.person_id)
            return PositionResult(repo.position(position_id), warnings)

    def update_position(self, position_id: int, draft: PositionDraft) -> PositionResult:
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            current = repo.position(position_id)
            scenario = repo.get(current.scenario_id)
            valid = self._validate(conn, scenario, draft, position_id)
            repo.update_position(position_id, valid)
            repo.touch(scenario.id, self._now())
            warnings = self._person_warnings(conn, scenario, valid.person_id)
            return PositionResult(repo.position(position_id), warnings)

    def delete_position(self, position_id: int) -> None:
        self.delete_positions([position_id])

    def delete_positions(self, position_ids: Sequence[int]) -> int:
        """Elimina varias posiciones en una sola transacción: si una no existe, no se elimina ninguna."""
        unique = list(dict.fromkeys(position_ids))
        with self._connection() as conn, transaction(conn):
            repo = ScenarioRepository(conn)
            scenario_ids = {repo.position(position_id).scenario_id for position_id in unique}
            deleted = sum(repo.delete_position(position_id) for position_id in unique)
            now = self._now()
            for scenario_id in scenario_ids:
                repo.touch(scenario_id, now)
        log.info("Posiciones eliminadas: %d", deleted)
        return deleted

    def list_persons(self) -> list[Person]:
        with self._connection() as conn:
            return CatalogRepository(conn).persons()

    def create_person(self, full_name: str, rut: str | None = None) -> Person:
        """Registra una persona; el RUT es opcional pero, si se indica, debe ser válido y único."""
        name = validate_name(full_name, "El nombre de la persona")
        normalized = normalize_rut(rut) if rut and rut.strip() else None
        with self._connection() as conn, transaction(conn):
            catalog = CatalogRepository(conn)
            if normalized is not None and catalog.find_person_by_rut(normalized) is not None:
                raise ValidationError("Ya existe una persona registrada con ese RUT.")
            return catalog.add_person(name, normalized)

    def workload_warnings(self, scenario_id: int) -> list[WorkloadWarning]:
        """Personas que superan la jornada semanal de referencia en algún tramo del año."""
        with self._connection() as conn:
            scenario = ScenarioRepository(conn).get(scenario_id)
            positions = ScenarioRepository(conn).positions(scenario_id)
            params = ParameterRepository(conn).cost_parameters()
        return list(scenario_workload_warnings(positions, params, scenario.year))

    def scenario_warnings(self, scenario_id: int) -> list[str]:
        """Avisos del escenario distintos de la jornada: posiciones sin vigencia en el año y parámetros faltantes.

        Las advertencias de jornada por persona se obtienen con `workload_warnings`.
        """
        with self._connection() as conn:
            repo = ScenarioRepository(conn)
            scenario = repo.get(scenario_id)
            positions = repo.positions(scenario_id)
            params = ParameterRepository(conn).cost_parameters()
        messages: list[str] = []
        for position in positions:
            if position.period_in_year(scenario.year) is None:
                messages.append(validity_message(position, scenario.year))
                continue
            try:
                ensure_position_costable(params, position.job_role, position.contract_type, scenario.year)
            except AppError as error:
                messages.append(error.user_message)
        return list(dict.fromkeys(messages))
