"""Servicios sobre una base sembrada en una carpeta temporal."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from staffing_simulator.domain.models import (
    BudgetItemDraft,
    BudgetItemType,
    PartialMonthMethod,
    PositionDraft,
    RetentionBasis,
    ScenarioDraft,
)
from staffing_simulator.domain.projection import Dimension
from staffing_simulator.errors import (
    MissingParameterError,
    NotFoundError,
    OperationCancelledError,
    ValidationError,
)
from staffing_simulator.services import Services

CURRENT, EXPANSION, RECONVERSION = 1, 2, 3


def _ids(services: Services) -> dict[str, int]:
    catalog = services.parameters.catalog()
    ids = {f"role:{item.code}": item.id for item in catalog.job_roles}
    ids |= {f"contract:{item.code}": item.id for item in catalog.contract_types}
    ids |= {f"site:{item.code}": item.id for item in catalog.sites}
    ids |= {f"program:{item.code}": item.id for item in catalog.programs}
    return ids


def _hr_item_id(services: Services, program_code: str = "P1", year: int = 2026) -> int:
    program_id = _ids(services)[f"program:{program_code}"]
    item = services.financial.default_human_resources_item(program_id, year)
    assert item is not None
    return item.id


def _draft(services: Services, **changes: object) -> PositionDraft:
    ids = _ids(services)
    values: dict[str, object] = {
        "job_role_id": ids["role:PSI"],
        "contract_type_id": ids["contract:HON"],
        "site_id": ids["site:SC"],
        "program_id": ids["program:P1"],
        "budget_item_id": _hr_item_id(services),
        "start_date": date(2026, 7, 1),
        "weekly_hours": Decimal(22),
    }
    values.update(changes)
    return PositionDraft(**values)  # type: ignore[arg-type]


def test_scenario_crud(services: Services) -> None:
    created = services.scenarios.create_scenario(ScenarioDraft(name="Plan invierno", year=2026))
    assert created.created_at == created.updated_at
    assert created.partial_month_method is PartialMonthMethod.PROPORTIONAL
    renamed = services.scenarios.rename_scenario(created.id, "Plan de invierno")
    assert renamed.name == "Plan de invierno"
    result = services.scenarios.update_scenario(
        created.id,
        ScenarioDraft(
            name="Plan de invierno",
            year=2027,
            partial_month_method=PartialMonthMethod.FULL_MONTH,
            expected_absence=Decimal("0.04"),
            base_scenario_id=CURRENT,
        ),
    )
    updated = result.scenario
    assert (updated.year, updated.expected_absence, updated.base_scenario_id) == (2027, Decimal("0.04"), CURRENT)
    assert result.warnings == ()
    services.scenarios.delete_scenario(created.id)
    with pytest.raises(NotFoundError):
        services.scenarios.get_scenario(created.id)


def test_scenario_names_are_unique_ignoring_case(services: Services) -> None:
    with pytest.raises(ValidationError, match="Ya existe"):
        services.scenarios.create_scenario(ScenarioDraft(name="dotación VIGENTE", year=2026))
    with pytest.raises(ValidationError):
        services.scenarios.rename_scenario(EXPANSION, "Dotación vigente")


def test_base_scenario_cannot_form_a_cycle(services: Services) -> None:
    current = services.scenarios.get_scenario(CURRENT)
    draft = current.to_draft()
    with pytest.raises(ValidationError, match="escenario base"):
        services.scenarios.update_scenario(
            CURRENT,
            ScenarioDraft(name=draft.name, year=draft.year, base_scenario_id=EXPANSION),
        )
    with pytest.raises(ValidationError):
        services.scenarios.update_scenario(
            CURRENT, ScenarioDraft(name=draft.name, year=draft.year, base_scenario_id=CURRENT)
        )


def test_duplicate_is_a_deep_copy(services: Services) -> None:
    copy = services.scenarios.duplicate_scenario(CURRENT)
    assert copy.name == "Copia de Dotación vigente"
    assert services.scenarios.suggest_copy_name(CURRENT) == "Copia de Dotación vigente (2)"
    original_positions = services.scenarios.list_positions(CURRENT)
    copied_positions = services.scenarios.list_positions(copy.id)
    assert len(copied_positions) == len(original_positions)
    assert not {item.id for item in original_positions} & {item.id for item in copied_positions}
    assert services.costs.project(copy.id).total_cost == services.costs.project(CURRENT).total_cost
    assert len(services.financial.other_expenses(copy.id)) == len(services.financial.other_expenses(CURRENT))

    services.scenarios.delete_position(copied_positions[0].id)
    services.scenarios.update_position(copied_positions[1].id, replace(copied_positions[1].to_draft(), note="cambio"))
    assert services.scenarios.get_position(copied_positions[1].id).note == "cambio"
    assert len(services.scenarios.list_positions(CURRENT)) == len(original_positions)
    assert services.scenarios.get_position(original_positions[1].id).note == original_positions[1].note


def test_add_update_and_delete_position(services: Services) -> None:
    before = services.costs.project(CURRENT).total_cost
    result = services.scenarios.add_position(CURRENT, _draft(services))
    assert result.position.weekly_hours == Decimal(22)
    assert result.position.budget_item.item_type is BudgetItemType.HUMAN_RESOURCES
    assert result.warnings == ()
    after = services.costs.project(CURRENT).total_cost
    assert after > before
    changed = services.scenarios.update_position(result.position.id, _draft(services, weekly_hours=Decimal(44)))
    assert changed.position.weekly_hours == Decimal(44)
    assert services.costs.project(CURRENT).total_cost > after
    services.scenarios.delete_position(result.position.id)
    assert services.costs.project(CURRENT).total_cost == before
    assert services.scenarios.get_scenario(CURRENT).updated_at.date() == date(2026, 9, 1)


def test_add_position_defaults_to_the_first_human_resources_item(services: Services) -> None:
    """El ítem se completa con el primero de recurso humano del programa si el llamado no lo indica."""
    result = services.scenarios.add_position(CURRENT, _draft(services, budget_item_id=None))
    assert result.position.budget_item.id == _hr_item_id(services)


def test_add_position_rejects_invalid_data(services: Services) -> None:
    with pytest.raises(ValidationError, match="anterior"):
        services.scenarios.add_position(CURRENT, _draft(services, end_date=date(2026, 6, 1)))
    ids = _ids(services)
    with pytest.raises(ValidationError, match="horas mensuales"):
        services.scenarios.add_position(CURRENT, _draft(services, contract_type_id=ids["contract:HPH"]))
    with pytest.raises(NotFoundError):
        services.scenarios.add_position(CURRENT, _draft(services, site_id=999))


def test_add_position_rejects_item_of_another_program(services: Services) -> None:
    other_item = _hr_item_id(services, "P2")
    with pytest.raises(ValidationError, match="no pertenece al programa"):
        services.scenarios.add_position(CURRENT, _draft(services, budget_item_id=other_item))


def test_add_position_without_rate_for_year_fails_clearly(services: Services) -> None:
    future = services.scenarios.create_scenario(ScenarioDraft(name="Plan 2029", year=2029))
    services.financial.add_item(
        BudgetItemDraft(
            _ids(services)["program:P1"], 2029, "P1-RRHH-2029", "Recurso humano", BudgetItemType.HUMAN_RESOURCES, 0
        )
    )
    with pytest.raises(MissingParameterError, match="2029"):
        services.scenarios.add_position(future.id, _draft(services, start_date=date(2029, 1, 1), budget_item_id=None))


def test_adding_a_concurrent_position_returns_workload_warning(services: Services) -> None:
    person = services.scenarios.create_person("Persona Nueva Sintética", "44.444.444-4")
    first = services.scenarios.add_position(CURRENT, _draft(services, person_id=person.id, start_date=date(2026, 1, 1)))
    assert first.warnings == ()
    # 22 h + 22 h suman exactamente la jornada completa (44 h): no hay aviso.
    second = services.scenarios.add_position(
        CURRENT,
        _draft(
            services,
            person_id=person.id,
            start_date=date(2026, 1, 1),
            program_id=_ids(services)["program:P2"],
            budget_item_id=_hr_item_id(services, "P2"),
        ),
    )
    assert second.warnings == ()
    third = services.scenarios.add_position(
        CURRENT,
        _draft(
            services,
            person_id=person.id,
            start_date=date(2026, 1, 1),
            program_id=_ids(services)["program:P3"],
            budget_item_id=_hr_item_id(services, "P3"),
            weekly_hours=Decimal("7.5"),
        ),
    )
    assert third.warnings
    assert "Persona Nueva Sintética suma 51,5 h semanales en 3 posiciones simultáneas" in third.warnings[-1]


def test_identical_positions_are_rejected_but_concurrent_ones_are_allowed(services: Services) -> None:
    """Regresión BUG-11: una persona puede tener posiciones concurrentes, pero no dos registros idénticos."""
    person = services.scenarios.create_person("Persona Duplicada Sintética", None)
    draft = _draft(services, person_id=person.id)
    added = services.scenarios.add_position(CURRENT, draft)
    before = len(services.scenarios.list_positions(CURRENT))
    with pytest.raises(ValidationError, match="Es idéntica a una posición que ya está en el escenario"):
        services.scenarios.add_position(CURRENT, replace(draft, note="Otra nota"))
    services.scenarios.add_position(
        CURRENT,
        replace(draft, program_id=_ids(services)["program:P2"], budget_item_id=_hr_item_id(services, "P2")),
    )
    assert len(services.scenarios.list_positions(CURRENT)) == before + 1
    vacancy = services.scenarios.add_position(CURRENT, _draft(services, weekly_hours=Decimal(11)))
    with pytest.raises(ValidationError, match="aumente la cantidad"):
        services.scenarios.add_position(CURRENT, _draft(services, weekly_hours=Decimal(11), quantity=2))
    # Editar una posición sin cambiar sus datos no choca consigo misma.
    services.scenarios.update_position(added.position.id, replace(draft, note="Nota editada"))
    services.scenarios.update_position(vacancy.position.id, _draft(services, weekly_hours=Decimal(11), quantity=2))
    with pytest.raises(ValidationError, match="idéntica"):
        services.scenarios.update_position(vacancy.position.id, draft)


def test_role_rate_missing_for_the_scenario_year_is_reported(services: Services) -> None:
    """Regresión BUG-09: el cargo médico con tarifa propia en 2025 a 2027 no usa la de su categoría en 2028."""
    params = services.parameters
    for category in "ABCDEF":
        params.set_category_rate(category, 2028, 20_000)
        params.set_salary_scale(category, date(2028, 1, 1), 900_000)
    for code in ("PF", "PLA"):
        params.set_contribution(_ids(services)[f"contract:{code}"], 2028, Decimal(0))
    draft = replace(services.scenarios.get_scenario(CURRENT).to_draft(), year=2028)
    services.scenarios.update_scenario(CURRENT, draft)
    with pytest.raises(MissingParameterError, match="tiene tarifa propia en 2025, 2026, 2027 pero no en 2028"):
        services.costs.project(CURRENT)
    notices = services.scenarios.scenario_warnings(CURRENT)
    assert any(message.startswith("El cargo Médico psiquiatra tiene tarifa propia") for message in notices)
    params.set_role_rate(_ids(services)["role:MED"], 2028, 20_500)
    assert services.costs.project(CURRENT).total_cost > 0


def test_create_person_validates_rut(services: Services) -> None:
    person = services.scenarios.create_person("  Persona   Sintética ", None)
    assert person.full_name == "Persona Sintética"
    with pytest.raises(ValidationError):
        services.scenarios.create_person("Otra", "44.444.444-5")
    services.scenarios.create_person("Tercera", "42000003-0")
    with pytest.raises(ValidationError, match="Ya existe"):
        services.scenarios.create_person("Cuarta", "42.000.003-0")


def test_kpis_match_projection_and_item_report(services: Services) -> None:
    projection = services.costs.project(CURRENT)
    kpis = services.costs.kpis(CURRENT)
    assert kpis.total_cost == projection.total_cost
    assert kpis.human_resources_cost == projection.total_cost
    assert kpis.headcount == kpis.persons + kpis.vacancies
    assert kpis.balance_total == kpis.assigned_total - kpis.human_resources_cost - kpis.other_expenses_total
    assert kpis.fee_net_total == kpis.fee_gross_total - kpis.retention_total
    assert 1 <= kpis.peak_month <= 12
    report = services.costs.item_report(CURRENT)
    assert report.total_planned_hr == projection.total_cost
    assert sum(row.total for row in projection.breakdown(Dimension.SITE)) == projection.total_cost


def test_program_filter_narrows_positions_costs_and_items(services: Services) -> None:
    """El filtro por convenio (program_id) acota posiciones, proyección, informe de ítems y comparación."""
    ids = _ids(services)
    program_id = ids["program:P1"]

    all_positions = services.scenarios.list_positions(CURRENT)
    filtered_positions = services.scenarios.list_positions(CURRENT, program_id)
    assert filtered_positions
    assert len(filtered_positions) < len(all_positions)
    assert all(position.program.id == program_id for position in filtered_positions)

    full_projection = services.costs.project(CURRENT)
    projection = services.costs.project(CURRENT, program_id)
    assert all(position.program.id == program_id for position in projection.positions)
    assert 0 < projection.total_cost < full_projection.total_cost

    report = services.costs.item_report(CURRENT, projection, program_id)
    assert report.lines
    assert all(line.item.program.id == program_id for line in report.lines)

    kpis = services.costs.kpis(CURRENT, projection, program_id)
    assert kpis.human_resources_cost == projection.total_cost
    assert kpis.assigned_total == report.total_assigned

    expenses = services.financial.other_expenses(CURRENT, program_id)
    assert expenses
    assert all(expense.budget_item.program.id == program_id for expense in expenses)

    comparison = services.costs.compare([CURRENT, EXPANSION], program_id=program_id)
    assert comparison.total.values[0] == projection.total_cost


def test_compare_seeded_scenarios(services: Services) -> None:
    progress: list[int] = []
    comparison = services.costs.compare(
        [CURRENT, EXPANSION, RECONVERSION], progress=lambda pct, _msg: progress.append(pct)
    )
    assert progress[-1] == 100
    assert comparison.total.differences[0] == 0
    assert comparison.total.differences[1] > 0
    # La reconversión a plazo fijo paga solo el sueldo del grado 15: puede ser más barata que los honorarios
    # que reemplaza (el municipio asume la diferencia de grado fuera del programa), no siempre más cara.
    assert comparison.total.differences[2] != 0
    salaried_type = next(row for row in comparison.by_contract_type if row.label == "Plazo fijo")
    assert salaried_type.values[2] > salaried_type.values[0]
    with pytest.raises(ValidationError):
        services.costs.compare([CURRENT])
    with pytest.raises(ValidationError):
        services.costs.compare([CURRENT, EXPANSION], base_id=RECONVERSION)


def test_compare_can_be_cancelled(services: Services) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelledError):
        services.costs.compare([CURRENT, EXPANSION], cancel=cancel)


def test_parameter_editing(services: Services) -> None:
    params = services.parameters
    before = services.costs.project(CURRENT).total_cost
    rates = {(row.category, row.year): row.hourly_rate for row in params.rate_rows(2026) if not row.is_role_specific}
    params.set_category_rate("b", 2026, rates[("B", 2026)] + 100)
    assert services.costs.project(CURRENT).total_cost > before
    with pytest.raises(ValidationError):
        params.set_category_rate("Z", 2026, 1000)
    with pytest.raises(ValidationError):
        params.set_category_rate("B", 2026, 0)
    params.delete_category_rate("B", 2026)
    with pytest.raises(MissingParameterError):
        services.costs.project(CURRENT)


def test_contributions_only_for_salaried_contracts(services: Services) -> None:
    ids = _ids(services)
    services.parameters.set_contribution(ids["contract:PF"], 2026, Decimal("0.07"))
    assert any(item.rate == Decimal("0.07") for item in services.parameters.contributions())
    with pytest.raises(ValidationError, match="dependientes"):
        services.parameters.set_contribution(ids["contract:HON"], 2026, Decimal("0.07"))
    with pytest.raises(ValidationError):
        services.parameters.set_contribution(ids["contract:PF"], 2026, Decimal("0.9"))
    with pytest.raises(ValidationError):
        services.parameters.update_contract_type(ids["contract:PF"], "Plazo fijo", True)
    renamed = services.parameters.update_contract_type(ids["contract:HON"], "Honorarios semanales", True)
    assert renamed.name == "Honorarios semanales"


def test_salary_scale_and_adjustments(services: Services) -> None:
    params = services.parameters
    params.set_salary_scale("B", date(2030, 1, 1), 1_500_000)
    assert any(item.category == "B" and item.valid_from == date(2030, 1, 1) for item in params.salary_scales())
    params.set_salary_adjustment(date(2030, 6, 1), Decimal("0.02"), "Reajuste de prueba")
    assert any(item.valid_from == date(2030, 6, 1) for item in params.salary_adjustments())
    params.delete_salary_adjustment(date(2030, 6, 1))
    with pytest.raises(NotFoundError):
        params.delete_salary_adjustment(date(2030, 6, 1))
    with pytest.raises(ValidationError):
        params.set_salary_scale("Z", date(2030, 1, 1), 1_000_000)
    with pytest.raises(ValidationError):
        params.set_salary_scale("B", date(2030, 1, 1), 0)


def test_retention_reference_and_full_time_setting(services: Services) -> None:
    params = services.parameters
    params.set_retention_rate(2029, Decimal("0.17"))
    assert params.retention_rates()[-1].year == 2029
    params.delete_retention_rate(2029)
    with pytest.raises(NotFoundError):
        params.delete_retention_rate(2029)
    assert params.settings().full_time_weekly_hours == 44
    before = services.costs.project(CURRENT)
    settings = params.update_settings(Decimal(5), RetentionBasis.PAYMENT, Decimal(44))
    assert params.settings() == settings
    assert services.costs.project(CURRENT).total_cost > before.total_cost
    with pytest.raises(ValidationError):
        params.update_settings(Decimal(0), RetentionBasis.SERVICE, Decimal(44))
    with pytest.raises(ValidationError):
        params.update_settings(Decimal(4), "otro", Decimal(44))
    with pytest.raises(ValidationError):
        params.update_settings(Decimal(4), RetentionBasis.SERVICE, Decimal(0))


def test_program_structure_and_other_expenses(services: Services) -> None:
    ids = _ids(services)
    program_id = ids["program:P1"]
    services.financial.set_program_budget(program_id, 2026, 999_000_000, reference="Res. 123")
    budget = next(item for item in services.financial.program_budgets(2026) if item.program_id == program_id)
    assert budget.amount == 999_000_000
    with pytest.raises(ValidationError):
        services.financial.set_program_budget(program_id, 2026, -5)
    items_before = services.financial.items(program_id, 2026)
    assert any(item.item_type is BudgetItemType.HUMAN_RESOURCES for item in items_before)
    other_expenses = services.financial.other_expenses(CURRENT)
    assert other_expenses
    expense = other_expenses[0]
    duplicated = services.financial.duplicate_other_expense(expense.id)
    assert duplicated.id != expense.id
    assert duplicated.amount == expense.amount
    services.financial.delete_other_expense(duplicated.id)
    with pytest.raises(NotFoundError):
        services.financial.delete_other_expense(duplicated.id)


def test_item_report_execution_and_manual_amount(services: Services) -> None:
    report = services.costs.item_report(CURRENT)
    assert any(line.has_execution for line in report.lines)
    item_id = _hr_item_id(services)
    services.execution.set_amount(item_id, 9, 123)
    assert services.execution.records(2026)
    services.execution.set_amount(item_id, 9, None)
    with pytest.raises(ValidationError):
        services.execution.set_amount(item_id, 13, 1)
    assert services.execution.years() == [2026]


def test_scenario_warnings_use_full_time_jornada(services: Services) -> None:
    assert services.scenarios.scenario_warnings(CURRENT) == []
    warnings = services.scenarios.workload_warnings(CURRENT)
    assert any(item.weekly_hours > 44 and item.concurrent > 1 for item in warnings)
    # La persona con 44 h semanales y turnos por horas suma el equivalente semanal de esos turnos.
    assert any(item.includes_hourly and item.weekly_hours == Decimal("53.5") for item in warnings)
    assert all(item.limit_hours == 44 for item in warnings)


def test_full_time_setting_affects_workload_warnings(services: Services) -> None:
    params = services.parameters
    before = len(services.scenarios.workload_warnings(CURRENT))
    settings = params.update_settings(Decimal(4), RetentionBasis.SERVICE, Decimal("40.5"))
    assert settings.full_time_weekly_minutes == 40 * 60 + 30
    assert params.settings() == settings
    assert len(services.scenarios.workload_warnings(CURRENT)) > before
    assert params.update_settings(Decimal(4), RetentionBasis.SERVICE, Decimal(44)).full_time_weekly_minutes == 44 * 60


def _year_change(services: Services, year: int) -> tuple[int, int, tuple[str, ...]]:
    total = len(services.scenarios.list_positions(CURRENT))
    inactive = len(services.scenarios.inactive_positions(CURRENT, year))
    draft = replace(services.scenarios.get_scenario(CURRENT).to_draft(), year=year)
    return total, inactive, services.scenarios.update_scenario(CURRENT, draft).warnings


def test_year_change_reports_positions_without_validity(services: Services) -> None:
    """Cambiar el año nunca deja posiciones sin costo en silencio."""
    positions = services.scenarios.list_positions(CURRENT)
    ended_2026 = sum(1 for item in positions if item.end_date is not None and item.end_date.year == 2026)
    total, inactive, warnings = _year_change(services, 2027)
    assert inactive == ended_2026 > 0
    assert warnings == (
        f"{inactive} de {total} posiciones no tienen vigencia en 2027 y no suman costo. "
        "Revise sus fechas en la pestaña Posiciones.",
    )
    assert len(services.scenarios.inactive_positions(CURRENT)) == inactive
    notices = services.scenarios.scenario_warnings(CURRENT)
    assert sum("no tiene vigencia en 2027" in message for message in notices) == inactive

    total, inactive, warnings = _year_change(services, 2025)
    assert inactive == total
    assert warnings[0].startswith(f"{total} de {total} posiciones no tienen vigencia en 2025")
    assert services.costs.project(CURRENT).total_cost == 0
    assert not services.costs.project(CURRENT).active_positions()

    _total, _inactive, warnings = _year_change(services, 2026)
    assert warnings == ()


def test_delete_positions_is_atomic(services: Services) -> None:
    positions = services.scenarios.list_positions(EXPANSION)
    ids = [positions[0].id, positions[1].id]
    with pytest.raises(NotFoundError):
        services.scenarios.delete_positions([*ids, 999_999])
    assert len(services.scenarios.list_positions(EXPANSION)) == len(positions)
    assert services.scenarios.delete_positions(ids) == 2
    assert len(services.scenarios.list_positions(EXPANSION)) == len(positions) - 2
