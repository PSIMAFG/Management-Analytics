"""Motor de costeo: fórmulas por tipo de contrato, ausentismo, vigencia, prorrateo y redondeo."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from helpers import (
    DOCTOR,
    GENERAL_DOCTOR,
    GRADE_15_B,
    HOURLY_FEE,
    RATE_B,
    SALARIED,
    WEEKLY_FEE,
    make_params,
    make_position,
    make_scenario,
)
from staffing_simulator.domain.costing import cost_position, fee_after_absence, retention_amount, weekly_gross
from staffing_simulator.domain.models import PartialMonthMethod, RetentionBasis
from staffing_simulator.domain.parameters import (
    CategoryRate,
    ContributionTable,
    CostParameters,
    CostSettings,
    RateTable,
    RoleRate,
)
from staffing_simulator.errors import MissingParameterError, ValidationError

FULL_44 = 44 * 4 * RATE_B  # 1.760.000


def test_weekly_fee_is_rate_times_hours_times_four(params: CostParameters) -> None:
    lines = cost_position(make_position(weekly_hours=44), make_scenario(), params)
    assert len(lines) == 12
    for line in lines:
        assert line.base == line.gross == line.cost == FULL_44
        assert line.employer_contribution == 0
        assert line.contract_hours == Decimal("176.00")
        assert line.retention == 268_400  # 15,25 % en 2026
        assert line.net == FULL_44 - 268_400


def test_hourly_fee_uses_estimated_monthly_hours(params: CostParameters) -> None:
    """Regresión BUG-10: el contrato por horas no se costea como una sola hora al mes."""
    position = make_position(contract=HOURLY_FEE, monthly_hours=38)
    lines = cost_position(position, make_scenario(), params)
    assert {line.cost for line in lines} == {380_000}
    assert all(line.applies_retention for line in lines)


def test_salaried_adds_employer_contribution_and_has_no_retention(params: CostParameters) -> None:
    """Plazo fijo y planta cuestan el sueldo del grado 15 (no el valor hora de honorarios)."""
    lines = cost_position(make_position(contract=SALARIED), make_scenario(), params)
    line = lines[0]
    assert line.gross == GRADE_15_B
    assert line.employer_contribution == 25_000  # 5 % de 500.000
    assert line.cost == GRADE_15_B + 25_000
    assert line.retention == 0
    assert not line.applies_retention


def test_absence_does_not_reduce_salaried_cost(params: CostParameters) -> None:
    lines = cost_position(make_position(contract=SALARIED), make_scenario(absence=Decimal("0.1")), params)
    assert {line.cost for line in lines} == {GRADE_15_B + 25_000}
    assert {line.absence_discount for line in lines} == {0}


def test_absence_discounts_rate_times_contracted_hours_not_worked(params: CostParameters) -> None:
    """Regresión BUG-02 y AMB-03: cada hora no trabajada descuenta el valor hora, y las horas no trabajadas
    son el porcentaje de las horas contratadas (44 h x 4 = 176 h), sin mezclar horas de calendario."""
    lines = cost_position(make_position(weekly_hours=44), make_scenario(absence=Decimal("0.1")), params)
    for line in lines:
        assert line.absence_discount == 176_000  # 10 % de 176 h x 10.000
        assert line.gross == FULL_44 - line.absence_discount
        assert line.cost == line.gross


def test_absence_percentage_means_the_same_for_weekly_and_hourly_fees(params: CostParameters) -> None:
    """El mismo ausentismo descuenta el mismo porcentaje del bruto en los dos tipos de honorarios."""
    scenario = make_scenario(absence=Decimal("0.03"))
    weekly = cost_position(make_position(weekly_hours=44), scenario, params)
    hourly = cost_position(make_position(contract=HOURLY_FEE, monthly_hours=176), scenario, params)
    for weekly_line, hourly_line in zip(weekly, hourly, strict=True):
        assert weekly_line.base == hourly_line.base == FULL_44
        assert weekly_line.absence_discount == hourly_line.absence_discount == 52_800
    # En un mes parcial el descuento también es el 3 % del bruto prorrateado.
    partial = cost_position(make_position(start=date(2026, 3, 9)), scenario, params)[2]
    assert abs(partial.absence_discount - partial.base * Decimal("0.03")) <= 1  # solo difiere por redondeo


@pytest.mark.parametrize(
    ("weekly_hours", "not_worked", "expected_rate_units"),
    [(33, 24, 108), (44, 44, 132), (44, 26, 150), (22, 13, 75), (44, 35, 141)],
)
def test_worked_examples_of_the_discount_rule(weekly_hours: int, not_worked: int, expected_rate_units: int) -> None:
    rate = Decimal(RATE_B)
    gross = weekly_gross(rate, Decimal(weekly_hours), Decimal(4))
    assert fee_after_absence(gross, rate, Decimal(not_worked)) == expected_rate_units * rate


def test_discount_rule_is_not_the_calendar_proportion() -> None:
    """Enero 2026 con 35 h no trabajadas: HB - 35 VH, no HB x 158 / 193."""
    rate = Decimal(RATE_B)
    gross = Decimal(FULL_44)
    paid = fee_after_absence(gross, rate, Decimal(35))
    assert paid == Decimal(1_410_000)
    assert paid != gross * Decimal(158) / Decimal(193)


def test_absence_discount_is_capped_between_zero_and_gross() -> None:
    """Regresión MB-03: el pago nunca supera el bruto ni queda negativo."""
    rate, gross = Decimal(RATE_B), Decimal(FULL_44)
    assert fee_after_absence(gross, rate, Decimal(200)) == 0
    assert fee_after_absence(gross, rate, Decimal(0)) == gross
    with pytest.raises(ValidationError):
        fee_after_absence(gross, rate, Decimal(-1))


def test_large_expected_absence_never_goes_negative(params: CostParameters) -> None:
    lines = cost_position(make_position(weekly_hours=44), make_scenario(absence=Decimal("0.5")), params)
    assert all(0 <= line.gross <= line.base for line in lines)


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2020, 107_500),
        (2021, 115_000),
        (2022, 122_500),
        (2023, 130_000),
        (2024, 137_500),
        (2025, 145_000),
        (2026, 152_500),
        (2027, 160_000),
        (2028, 170_000),
        (2031, 170_000),
    ],
)
def test_retention_by_year(params: CostParameters, year: int, expected: int) -> None:
    rate = params.retention.rate_for(year)
    assert retention_amount(1_000_000, rate) == expected
    assert 1_000_000 - retention_amount(1_000_000, rate) == 1_000_000 - expected


def test_retention_uses_scenario_year(params: CostParameters) -> None:
    lines = cost_position(make_position(start=date(2025, 1, 1)), make_scenario(year=2025), params)
    assert lines[0].retention == retention_amount(FULL_44, Decimal("0.145"))


def test_retention_basis_payment_moves_december_to_next_year() -> None:
    params = make_params(settings=CostSettings(retention_basis=RetentionBasis.PAYMENT))
    lines = cost_position(make_position(), make_scenario(), params)
    assert lines[10].retention == 268_400  # noviembre: 15,25 %
    assert lines[11].retention == 281_600  # diciembre, pagado en 2027: 16 %


def test_end_date_is_respected(params: CostParameters) -> None:
    """Regresión BUG-01: un contrato vencido no suma costo y el término corta la vigencia."""
    expired = make_position(start=date(2025, 1, 1), end=date(2025, 12, 31))
    assert all(not line.active and line.cost == 0 for line in cost_position(expired, make_scenario(), params))
    until_march = cost_position(make_position(end=date(2026, 3, 31)), make_scenario(), params)
    assert [line.cost for line in until_march[:3]] == [FULL_44] * 3
    assert all(line.cost == 0 for line in until_march[3:])


def test_open_end_runs_until_year_end(params: CostParameters) -> None:
    carried_over = make_position(start=date(2025, 6, 1), end=None)
    lines = cost_position(carried_over, make_scenario(), params)
    assert all(line.active and line.cost == FULL_44 for line in lines)


def test_proportional_start_pays_the_share_of_scheduled_hours(params: CostParameters) -> None:
    """Regresión BUG-05: ingreso el lunes 9 de marzo; se pagan 150 de las 194 h programadas de marzo."""
    position = make_position(start=date(2026, 3, 9))
    proportional = cost_position(position, make_scenario(), params)
    full_month = cost_position(position, make_scenario(method=PartialMonthMethod.FULL_MONTH), params)
    assert proportional[2].base == 1_360_825  # 1.760.000 x 150 / 194
    assert full_month[2].base == FULL_44
    assert proportional[1].cost == full_month[1].cost == 0
    assert proportional[3].cost == full_month[3].cost == FULL_44


def test_proportional_end_mid_month(params: CostParameters) -> None:
    position = make_position(end=date(2026, 3, 8))
    lines = cost_position(position, make_scenario(), params)
    # Marzo 2026 tiene 194 h programadas y hasta el 8 hay 44 h vigentes.
    assert lines[2].base == 399_175  # 1.760.000 x 44 / 194


@pytest.mark.parametrize(
    ("start", "end", "month", "expected"),
    [
        (date(2026, 3, 31), None, 3, 81_649),  # ingreso el martes 31: 9 de 194 h
        (date(2025, 6, 1), date(2026, 1, 2), 1, 155_026),  # egreso el viernes 2: 17 de 193 h
    ],
)
def test_a_single_scheduled_day_is_never_free(
    params: CostParameters, start: date, end: date | None, month: int, expected: int
) -> None:
    """Un tramo con horas programadas vigentes nunca cuesta 0 (antes, HB - VH x horas fuera daba 0)."""
    line = cost_position(make_position(start=start, end=end), make_scenario(), params)[month - 1]
    assert line.active
    assert line.base == expected > 0


@pytest.mark.parametrize(
    ("hours", "start", "end", "month", "expected"),
    [
        (22, date(2026, 10, 5), date(2026, 10, 30), 10, 806_667),  # cuatro semanas: 88 de 96 h de octubre
        (44, date(2026, 5, 4), date(2026, 5, 8), 5, 420_870),  # una semana: 44 de 184 h de mayo
        (44, date(2026, 3, 9), date(2026, 3, 31), 3, 1_360_825),  # reemplazo de tres semanas: 150 de 194 h
    ],
)
def test_short_contracts_pay_their_share_of_the_month(
    params: CostParameters, hours: int, start: date, end: date, month: int, expected: int
) -> None:
    line = cost_position(make_position(weekly_hours=hours, start=start, end=end), make_scenario(), params)[month - 1]
    assert line.base == expected


def test_splitting_a_position_in_two_segments_keeps_its_cost(params: CostParameters) -> None:
    """Regresión BUG-13: dividir una posición en tramos consecutivos (un cambio de jornada) no cambia su costo."""
    whole = cost_position(make_position(), make_scenario(), params)
    first = cost_position(make_position(end=date(2026, 3, 14)), make_scenario(), params)
    second = cost_position(make_position(start=date(2026, 3, 15)), make_scenario(), params)
    assert (first[2].base, second[2].base) == (798_351, 961_649)  # 88 y 106 de las 194 h de marzo
    for index in range(12):
        assert first[index].cost + second[index].cost == whole[index].cost


def test_hourly_fee_prorated_by_business_days(params: CostParameters) -> None:
    position = make_position(contract=HOURLY_FEE, monthly_hours=38, start=date(2026, 3, 16))
    proportional = cost_position(position, make_scenario(), params)
    full_month = cost_position(position, make_scenario(method=PartialMonthMethod.FULL_MONTH), params)
    assert proportional[2].base == 207_273  # 38 h x 12 / 22 días hábiles x 10.000
    assert full_month[2].base == 380_000


def test_salaried_prorated_by_calendar_days(params: CostParameters) -> None:
    lines = cost_position(make_position(contract=SALARIED, start=date(2026, 3, 16)), make_scenario(), params)
    assert lines[2].gross == 258_065  # 500.000 x 16 / 31
    assert lines[2].employer_contribution == 12_903
    assert lines[2].cost == 258_065 + 12_903


def test_rounding_is_half_up_per_position_and_month() -> None:
    """Regresión BUG-17: montos en pesos enteros con ROUND_HALF_UP antes de agregar."""
    base = make_params()
    params = CostParameters(
        rates=RateTable.from_records([CategoryRate("B", 2026, 10_001)]),
        retention=base.retention,
        contributions=ContributionTable(),
    )
    position = make_position(contract=HOURLY_FEE, monthly_hours="38.5")
    line = cost_position(position, make_scenario(), params)[0]
    assert line.gross == 385_039  # 385.038,5 sube
    assert line.retention == 58_718  # 58.718,4475 baja
    assert isinstance(line.gross, int)


def test_quantity_multiplies_rounded_unit_amounts(params: CostParameters) -> None:
    single = cost_position(make_position(start=date(2026, 3, 16)), make_scenario(), params)
    double = cost_position(make_position(start=date(2026, 3, 16), quantity=2), make_scenario(), params)
    for one, two in zip(single, double, strict=True):
        assert two.cost == 2 * one.cost
        assert two.retention == 2 * one.retention


def test_role_specific_rate_prevails_over_category(params: CostParameters) -> None:
    """Regresión BUG-09: el cargo médico se costea con su tarifa, no con la profesional."""
    doctor = cost_position(make_position(role=DOCTOR, weekly_hours=6), make_scenario(), params)
    general = cost_position(make_position(role=GENERAL_DOCTOR, weekly_hours=6), make_scenario(), params)
    assert doctor[0].cost == 23_000 * 6 * 4
    assert general[0].cost == 20_000 * 6 * 4


def test_role_with_own_rate_in_other_years_does_not_fall_back_to_the_category() -> None:
    """Regresión BUG-09: el cargo médico con tarifa propia en 2026 no se costea en 2027 con la de la categoría."""
    base = make_params()
    rates = RateTable.from_records(
        [CategoryRate("A", year, 20_000) for year in (2026, 2027)],
        [RoleRate(DOCTOR.id, 2026, 23_000)],
    )
    params = CostParameters(rates=rates, retention=base.retention, contributions=base.contributions)
    assert rates.hourly_rate(DOCTOR, 2026) == 23_000
    assert rates.hourly_rate(GENERAL_DOCTOR, 2027) == 20_000
    with pytest.raises(MissingParameterError) as info:
        cost_position(
            make_position(role=DOCTOR, weekly_hours=6, start=date(2027, 1, 1)), make_scenario(year=2027), params
        )
    assert info.value.user_message.startswith("El cargo Médico psiquiatra tiene tarifa propia en 2026 pero no en 2027")


def test_missing_rate_raises_clear_error(params: CostParameters) -> None:
    """Regresión BUG-18: sin tarifa para la categoría y el año hay error, nunca costo 0."""
    with pytest.raises(MissingParameterError) as info:
        cost_position(make_position(start=date(2028, 1, 1)), make_scenario(year=2028), params)
    assert "categoría B" in info.value.user_message
    assert "2028" in info.value.user_message


def test_position_without_validity_does_not_need_rates(params: CostParameters) -> None:
    lines = cost_position(
        make_position(start=date(2026, 1, 1), end=date(2026, 6, 30)), make_scenario(year=2028), params
    )
    assert all(line.cost == 0 for line in lines)


def test_missing_employer_contribution_raises(params: CostParameters) -> None:
    empty = CostParameters(rates=params.rates, retention=params.retention, contributions=ContributionTable())
    with pytest.raises(MissingParameterError):
        cost_position(make_position(contract=SALARIED), make_scenario(), empty)
    assert cost_position(make_position(contract=WEEKLY_FEE), make_scenario(), empty)[0].cost == FULL_44


def test_weeks_per_month_is_a_parameter() -> None:
    params = make_params(weeks_per_month=Decimal("4.33"))
    line = cost_position(make_position(weekly_hours=44), make_scenario(), params)[0]
    assert line.gross == 1_905_200


def test_missing_hours_for_contract_type_is_rejected(params: CostParameters) -> None:
    broken = replace(make_position(contract=HOURLY_FEE), monthly_minutes=None)
    with pytest.raises(ValidationError):
        cost_position(broken, make_scenario(), params)


def test_only_two_partial_month_methods_exist() -> None:
    """Regresión BUG-16: solo hay dos métodos de mes parcial; no existe un tercero por día calendario."""
    assert {method.value for method in PartialMonthMethod} == {"proportional", "full_month"}
