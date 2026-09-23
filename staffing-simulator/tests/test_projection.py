"""Agregados de la proyección: invariantes, posiciones concurrentes, tramos y dotación."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from helpers import (
    HOURLY_FEE,
    OTHER_PROGRAM,
    OTHER_SITE,
    RATE_B,
    SALARIED,
    make_params,
    make_position,
    make_scenario,
)
from staffing_simulator.domain.models import Person
from staffing_simulator.domain.parameters import CostParameters
from staffing_simulator.domain.projection import Dimension, Measure, Projection, project_scenario

PERSON = Person(1, "Persona de prueba", "41234567-3")
OTHER = Person(2, "Otra persona", None)


def _mixed_projection(params: CostParameters) -> Projection:
    positions = [
        make_position(weekly_hours=44, person=PERSON),
        make_position(weekly_hours="7.5", person=PERSON, program=OTHER_PROGRAM, site=OTHER_SITE),
        make_position(weekly_hours=32, person=OTHER, end=date(2026, 3, 31)),
        make_position(weekly_hours=41, person=OTHER, start=date(2026, 4, 1)),
        make_position(contract=HOURLY_FEE, monthly_hours=38, start=date(2026, 3, 16)),
        make_position(contract=SALARIED, weekly_hours=22, start=date(2026, 5, 11), quantity=3),
        make_position(weekly_hours=44, start=date(2026, 10, 5), end=date(2026, 10, 30)),
    ]
    return project_scenario(make_scenario(absence=Decimal("0.03")), positions, params)


@pytest.mark.parametrize("measure", list(Measure))
@pytest.mark.parametrize("dimension", list(Dimension))
def test_sum_of_parts_equals_total(params: CostParameters, dimension: Dimension, measure: Measure) -> None:
    """Regresión BUG-06 y BUG-08: ningún desglose deja fuera una posición ni un mes."""
    projection = _mixed_projection(params)
    rows = projection.breakdown(dimension, measure)
    assert sum(row.total for row in rows) == projection.total(measure)
    for month in range(12):
        assert sum(row.monthly[month] for row in rows) == projection.monthly(measure)[month]


def test_cumulative_and_shares(params: CostParameters) -> None:
    projection = _mixed_projection(params)
    assert projection.cumulative()[-1] == projection.total_cost
    assert sum(projection.monthly()) == projection.total_cost
    shares = [row.share for row in projection.breakdown(Dimension.PROGRAM)]
    assert sum(share for share in shares if share is not None) == pytest.approx(Decimal(1))
    assert sum(item.total for item in projection.position_costs()) == projection.total_cost


def test_monthly_times_months_equals_annual_without_proration(params: CostParameters) -> None:
    positions = [make_position(weekly_hours=hours) for hours in (44, 22, 15)]
    projection = project_scenario(make_scenario(), positions, params)
    assert len(set(projection.monthly())) == 1
    assert projection.monthly()[0] * 12 == projection.total_cost


def test_cost_components_are_consistent(params: CostParameters) -> None:
    projection = _mixed_projection(params)
    assert projection.total(Measure.COST) == projection.total(Measure.GROSS) + projection.total(
        Measure.EMPLOYER_CONTRIBUTION
    )
    assert projection.total(Measure.GROSS) == projection.total(Measure.BASE) - projection.total(
        Measure.ABSENCE_DISCOUNT
    )
    assert projection.fee_net_total == projection.fee_gross_total - projection.retention_total
    assert abs(projection.average_monthly_cost * 12 - projection.total_cost) <= 6


def test_concurrent_positions_of_one_person_all_count(params: CostParameters) -> None:
    """Regresión BUG-11 y BUG-12: dos posiciones simultáneas de una persona se suman."""
    main = make_position(weekly_hours=33, person=PERSON)
    second = make_position(weekly_hours="7.5", person=PERSON, program=OTHER_PROGRAM)
    projection = project_scenario(make_scenario(), [main, second], params)
    assert projection.monthly()[0] == (33 * 4 + 30) * RATE_B
    by_program = {row.label: row.total for row in projection.breakdown(Dimension.PROGRAM)}
    assert by_program == {"Programa Base": 33 * 4 * RATE_B * 12, "Programa Comunitario": 30 * RATE_B * 12}
    assert projection.staffing().persons == 1


def test_change_of_hours_mid_year_keeps_both_segments(params: CostParameters) -> None:
    """Regresión BUG-13: el cambio de jornada abre un tramo nuevo sin borrar el anterior."""
    before = make_position(weekly_hours=32, person=PERSON, end=date(2026, 3, 31))
    after = make_position(weekly_hours=41, person=PERSON, start=date(2026, 4, 1))
    projection = project_scenario(make_scenario(), [before, after], params)
    monthly = projection.monthly()
    assert monthly[:3] == (32 * 4 * RATE_B,) * 3
    assert monthly[3:] == (41 * 4 * RATE_B,) * 9
    staffing = projection.staffing()
    assert staffing.persons == 1
    assert staffing.position_records == 2
    assert staffing.headcount == 1


def test_mid_month_change_of_hours_prorates_both_segments(params: CostParameters) -> None:
    """Cambio de 22 h a 44 h el lunes 9 de marzo: cada tramo paga su parte de las horas programadas del mes."""
    before = make_position(weekly_hours=22, person=PERSON, end=date(2026, 3, 8))
    after = make_position(weekly_hours=44, person=PERSON, start=date(2026, 3, 9))
    projection = project_scenario(make_scenario(), [before, after], params)
    march = {line.position_id: line for line in projection.lines if line.month == 3}
    assert all(line.active for line in march.values())
    # 22 h: 880.000 x 22 / 98 h de marzo; 44 h: 1.760.000 x 150 / 194 h de marzo.
    assert march[before.id].base == 197_551
    assert march[after.id].base == 1_360_825
    assert projection.monthly()[2] == 1_558_376
    assert projection.monthly()[1] == 22 * 4 * RATE_B
    assert projection.monthly()[3] == 44 * 4 * RATE_B


def test_staffing_summary(params: CostParameters) -> None:
    positions = [
        make_position(person=PERSON),
        make_position(person=PERSON, weekly_hours="7.5"),
        make_position(quantity=2, start=date(2026, 7, 1)),
        make_position(contract=HOURLY_FEE, monthly_hours=40),
        make_position(start=date(2025, 1, 1), end=date(2025, 12, 31)),
    ]
    projection = project_scenario(make_scenario(), positions, params)
    staffing = projection.staffing()
    assert staffing.persons == 1
    assert staffing.vacancies == 3
    assert staffing.position_records == 5
    assert staffing.headcount == 4
    # 44 + 7,5 + 2 x 44 x (184/365) + 40/4 = 61,5 + 44,36 = 105,86
    assert staffing.weekly_hours == Decimal("105.9")
    assert projection.weekly_hours_on(date(2026, 8, 1)) == Decimal("149.5")


def test_progress_callback_reports_each_position(params: CostParameters) -> None:
    calls: list[tuple[int, int]] = []
    positions = [make_position(), make_position()]
    project_scenario(make_scenario(), positions, params, on_position=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 2), (2, 2)]


def test_breakdown_is_sorted_by_total() -> None:
    params = make_params()
    positions = [make_position(weekly_hours=10), make_position(weekly_hours=44, program=OTHER_PROGRAM)]
    rows = project_scenario(make_scenario(), positions, params).breakdown(Dimension.PROGRAM)
    assert [row.label for row in rows] == ["Programa Comunitario", "Programa Base"]
