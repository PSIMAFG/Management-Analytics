"""Comparación de escenarios y estructura financiera: asignado, planificado y ejecutado por ítem."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from helpers import (
    BUDGET_ITEM,
    GRADE_15_B,
    OTHER_BUDGET_ITEM,
    OTHER_PROGRAM,
    RATE_B,
    SALARIED,
    make_position,
    make_scenario,
)
from staffing_simulator.domain.budget import item_report
from staffing_simulator.domain.comparison import MAX_COMPARED, compare_projections
from staffing_simulator.domain.models import ExecutionRecord
from staffing_simulator.domain.parameters import CostParameters
from staffing_simulator.domain.projection import Projection, project_scenario
from staffing_simulator.errors import ValidationError

MONTHLY_44 = 44 * 4 * RATE_B


def _base(params: CostParameters) -> Projection:
    positions = [
        make_position(weekly_hours=44),
        make_position(weekly_hours=22, program=OTHER_PROGRAM, budget_item=OTHER_BUDGET_ITEM),
    ]
    return project_scenario(make_scenario(scenario_id=1, name="Base"), positions, params)


def _expanded(params: CostParameters) -> Projection:
    positions = [
        make_position(weekly_hours=44),
        make_position(weekly_hours=22, program=OTHER_PROGRAM, budget_item=OTHER_BUDGET_ITEM),
        make_position(weekly_hours=44, start=date(2026, 7, 1), quantity=2),
        make_position(contract=SALARIED, weekly_hours=44),
    ]
    return project_scenario(make_scenario(scenario_id=2, name="Expansión"), positions, params)


def test_comparison_differences_against_base(params: CostParameters) -> None:
    base, expanded = _base(params), _expanded(params)
    comparison = compare_projections([base, expanded], base_index=0)
    assert comparison.base.name == "Base"
    assert comparison.total.values == (base.total_cost, expanded.total_cost)
    difference = expanded.total_cost - base.total_cost
    assert comparison.total.differences == (0, difference)
    assert comparison.total.percentages[1] == Decimal(difference) / Decimal(base.total_cost)
    assert comparison.total.percentages[0] == 0
    assert comparison.monthly[0].label == "Ene"
    salaried_cost = GRADE_15_B + GRADE_15_B * 5 // 100
    assert comparison.monthly[6].differences[1] == 2 * MONTHLY_44 + salaried_cost
    assert comparison.scenarios[1].cumulative[-1] == expanded.total_cost


def test_comparison_by_contract_type_includes_missing_keys(params: CostParameters) -> None:
    comparison = compare_projections([_base(params), _expanded(params)])
    salaried_type = next(row for row in comparison.by_contract_type if row.label == "Plazo fijo")
    assert salaried_type.values[0] == 0
    assert salaried_type.percentages[1] is None  # sin base no hay porcentaje
    assert sum(row.values[1] for row in comparison.by_contract_type) == comparison.total.values[1]
    assert sum(row.values[0] for row in comparison.by_job_role) == comparison.total.values[0]


def test_comparison_with_other_base(params: CostParameters) -> None:
    comparison = compare_projections([_base(params), _expanded(params)], base_index=1)
    assert comparison.total.differences[1] == 0
    assert comparison.total.differences[0] < 0


def test_comparison_validation(params: CostParameters) -> None:
    base = _base(params)
    with pytest.raises(ValidationError):
        compare_projections([base])
    with pytest.raises(ValidationError):
        compare_projections([base, base])
    with pytest.raises(ValidationError):
        compare_projections([base, _expanded(params)], base_index=5)
    many = [
        project_scenario(make_scenario(scenario_id=index, name=f"Escenario {index}"), [make_position()], params)
        for index in range(1, MAX_COMPARED + 2)
    ]
    with pytest.raises(ValidationError, match="como máximo"):
        compare_projections(many)
    assert len(compare_projections(many[:MAX_COMPARED]).scenarios) == MAX_COMPARED


def test_item_report_assigned_planned_and_balance(params: CostParameters) -> None:
    projection = _base(params)
    report = item_report([BUDGET_ITEM, OTHER_BUDGET_ITEM], projection)
    lines = {line.item.code: line for line in report.lines}
    assert lines["P1-RRHH"].planned_hr_total == MONTHLY_44 * 12
    assert lines["P1-RRHH"].planned_other_total == 0
    assert lines["P1-RRHH"].assigned == BUDGET_ITEM.amount
    assert lines["P1-RRHH"].balance == BUDGET_ITEM.amount - MONTHLY_44 * 12
    assert lines["P1-RRHH"].used_share == Decimal(MONTHLY_44 * 12) / Decimal(BUDGET_ITEM.amount)
    assert report.total_planned_hr == projection.total_cost
    assert report.total_assigned == BUDGET_ITEM.amount + OTHER_BUDGET_ITEM.amount
    by_program = {line.program.code: line for line in report.by_program()}
    assert by_program["P1"].planned_hr == lines["P1-RRHH"].planned_hr_total
    assert by_program["P1"].assigned == BUDGET_ITEM.amount


def test_item_report_execution_to_date(params: CostParameters) -> None:
    """Ejecutado y planificado a la fecha comparan solo los meses con ejecución registrada."""
    projection = _base(params)
    records = [
        ExecutionRecord(BUDGET_ITEM.id, 2026, 1, MONTHLY_44 - 100_000),
        ExecutionRecord(BUDGET_ITEM.id, 2026, 2, MONTHLY_44 + 50_000),
        ExecutionRecord(BUDGET_ITEM.id, 2025, 1, 1),  # de otro año: se ignora
    ]
    report = item_report([BUDGET_ITEM, OTHER_BUDGET_ITEM], projection, execution=records)
    line = next(item for item in report.lines if item.item.id == BUDGET_ITEM.id)
    assert line.executed[0] == MONTHLY_44 - 100_000
    assert line.executed[1] == MONTHLY_44 + 50_000
    assert line.executed[2] is None
    assert line.executed_to_date == 2 * MONTHLY_44 - 50_000
    assert line.projected_to_date == 2 * MONTHLY_44
    assert line.execution_difference == -50_000
    other_line = next(item for item in report.lines if item.item.id == OTHER_BUDGET_ITEM.id)
    assert not other_line.has_execution
    assert other_line.executed_to_date == 0


def test_item_report_total_balance_counts_missing_execution_as_zero(params: CostParameters) -> None:
    projection = _base(params)
    report = item_report([BUDGET_ITEM, OTHER_BUDGET_ITEM], projection)
    assert report.total_planned == projection.total_cost
    assert report.total_balance == report.total_assigned - report.total_planned
    assert report.total_executed_to_date == 0
