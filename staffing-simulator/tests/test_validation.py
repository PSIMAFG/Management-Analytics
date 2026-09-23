"""Validación de escenarios y posiciones, advertencias de jornada, RUT y unidades."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from helpers import BUDGET_ITEM, HOURLY_FEE, SALARIED, WEEKLY_FEE, make_position
from staffing_simulator.domain.models import PartialMonthMethod, Person, PositionDraft, ScenarioDraft
from staffing_simulator.domain.rut import check_digit, format_rut, normalize_rut
from staffing_simulator.domain.units import (
    bp_to_fraction,
    fraction_to_bp,
    hours_to_minutes,
    minutes_to_hours,
    round_pesos,
    safe_ratio,
    to_decimal,
)
from staffing_simulator.domain.validation import (
    PositionIdentity,
    find_identical,
    identical_position_message,
    validate_grade,
    validate_position_draft,
    validate_scenario_draft,
    workload_warnings,
)
from staffing_simulator.errors import ValidationError

FULL_TIME_MINUTES = 44 * 60


def _draft(**changes: object) -> PositionDraft:
    values: dict[str, object] = {
        "job_role_id": 1,
        "contract_type_id": 1,
        "site_id": 1,
        "program_id": 1,
        "budget_item_id": BUDGET_ITEM.id,
        "start_date": date(2026, 1, 1),
        "weekly_hours": Decimal(44),
    }
    values.update(changes)
    return PositionDraft(**values)  # type: ignore[arg-type]


def test_valid_weekly_position_is_normalized_to_minutes() -> None:
    valid = validate_position_draft(_draft(weekly_hours=Decimal("7.5"), note="  nota  "), WEEKLY_FEE, 2026)
    assert valid.weekly_minutes == 450
    assert valid.monthly_minutes is None
    assert valid.note == "nota"
    assert valid.budget_item_id == BUDGET_ITEM.id


def test_missing_budget_item_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ítem presupuestario"):
        validate_position_draft(_draft(budget_item_id=None), WEEKLY_FEE, 2026)


def test_end_before_start_is_rejected() -> None:
    """Regresión BUG-01 y MB-02: término anterior al inicio."""
    with pytest.raises(ValidationError, match="anterior a la de inicio"):
        validate_position_draft(_draft(start_date=date(2026, 3, 18), end_date=date(2025, 7, 31)), WEEKLY_FEE, 2026)


def test_position_must_touch_scenario_year() -> None:
    with pytest.raises(ValidationError, match="no tiene vigencia en 2026"):
        validate_position_draft(_draft(start_date=date(2025, 4, 1), end_date=date(2025, 12, 31)), WEEKLY_FEE, 2026)
    with pytest.raises(ValidationError):
        validate_position_draft(_draft(start_date=date(2027, 1, 1)), WEEKLY_FEE, 2026)
    carried = validate_position_draft(_draft(start_date=date(2025, 4, 1)), WEEKLY_FEE, 2026)
    assert carried.start_date == date(2025, 4, 1)


def test_hourly_contract_requires_monthly_hours() -> None:
    """Regresión BUG-10 y BUG-20: las horas mensuales son obligatorias y no se mezclan con las semanales."""
    with pytest.raises(ValidationError, match="horas mensuales"):
        validate_position_draft(_draft(weekly_hours=None), HOURLY_FEE, 2026)
    with pytest.raises(ValidationError, match="horas semanales"):
        validate_position_draft(_draft(weekly_hours=Decimal(10), monthly_hours=Decimal(38)), HOURLY_FEE, 2026)
    valid = validate_position_draft(_draft(weekly_hours=None, monthly_hours=Decimal(38)), HOURLY_FEE, 2026)
    assert valid.monthly_minutes == 38 * 60


def test_weekly_contract_rejects_monthly_hours() -> None:
    with pytest.raises(ValidationError):
        validate_position_draft(_draft(monthly_hours=Decimal(38)), SALARIED, 2026)
    with pytest.raises(ValidationError):
        validate_position_draft(_draft(weekly_hours=None), SALARIED, 2026)


@pytest.mark.parametrize("hours", [Decimal(0), Decimal(-4), Decimal(49), Decimal("7.123")])
def test_invalid_weekly_hours(hours: Decimal) -> None:
    with pytest.raises(ValidationError):
        validate_position_draft(_draft(weekly_hours=hours), WEEKLY_FEE, 2026)


def test_quantity_rules() -> None:
    with pytest.raises(ValidationError):
        validate_position_draft(_draft(quantity=0), WEEKLY_FEE, 2026)
    with pytest.raises(ValidationError):
        validate_position_draft(_draft(quantity=2, person_id=5), WEEKLY_FEE, 2026)
    assert validate_position_draft(_draft(quantity=3), WEEKLY_FEE, 2026).quantity == 3


def test_grade_only_applies_to_salaried_contracts() -> None:
    assert validate_grade(None, WEEKLY_FEE) is None
    with pytest.raises(ValidationError, match="contratos dependientes"):
        validate_grade(15, WEEKLY_FEE)
    assert validate_grade(12, SALARIED) == 12
    with pytest.raises(ValidationError, match="entre 1 y 30"):
        validate_grade(0, SALARIED)
    valid = validate_position_draft(_draft(contract_type_id=SALARIED.id, grade=12), SALARIED, 2026)
    assert valid.grade == 12


def test_scenario_draft_validation() -> None:
    clean = validate_scenario_draft(ScenarioDraft(name="  Plan   2026 ", year=2026, expected_absence=Decimal("0.03")))
    assert clean.name == "Plan 2026"
    assert clean.partial_month_method is PartialMonthMethod.PROPORTIONAL
    for bad in (
        ScenarioDraft(name=" ", year=2026),
        ScenarioDraft(name="X", year=1999),
        ScenarioDraft(name="X", year=2026, expected_absence=Decimal("0.6")),
        ScenarioDraft(name="X", year=2026, expected_absence=Decimal("-0.01")),
        ScenarioDraft(name="X", year=2026, partial_month_method="otro"),  # type: ignore[arg-type]
        ScenarioDraft(name="X" * 81, year=2026),
    ):
        with pytest.raises(ValidationError):
            validate_scenario_draft(bad)


PERSON = Person(7, "Persona con dos posiciones")


def test_workload_warning_for_concurrent_positions() -> None:
    """Jornada completa (44 h): 44 h + 7,5 h supera el límite; se advierte sin bloquear, sin importar el contrato."""
    positions = [make_position(person=PERSON), make_position(person=PERSON, weekly_hours="7.5")]
    warnings = workload_warnings(positions, 2026, full_time_minutes=FULL_TIME_MINUTES)
    assert [(item.weekly_hours, item.limit_hours) for item in warnings] == [(Decimal("51.5"), Decimal(44))]
    assert (warnings[0].start, warnings[0].end) == (date(2026, 1, 1), date(2026, 12, 31))
    assert warnings[0].concurrent == 2
    assert "51,5 h semanales en 2 posiciones simultáneas" in warnings[0].message
    assert "la jornada completa es 44 h" in warnings[0].message


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (("44",), ()),
        (("22", "22"), ()),
        (("45",), (Decimal(45),)),
        (("22.5", "22.5"), (Decimal(45),)),
    ],
)
def test_same_load_is_judged_the_same_in_one_or_several_records(
    hours: tuple[str, ...], expected: tuple[Decimal, ...]
) -> None:
    """La misma carga da el mismo aviso, esté en una posición o repartida en varias, sin importar el contrato."""
    positions = [make_position(person=PERSON, weekly_hours=value) for value in hours]
    warnings = workload_warnings(positions, 2026, full_time_minutes=FULL_TIME_MINUTES)
    assert tuple(item.weekly_hours for item in warnings) == expected


def test_full_time_threshold_is_configurable() -> None:
    positions = [make_position(person=PERSON, weekly_hours=22), make_position(person=PERSON, weekly_hours=22)]
    assert workload_warnings(positions, 2026, full_time_minutes=40 * 60)[0].limit_hours == Decimal(40)
    assert workload_warnings(positions, 2026, full_time_minutes=44 * 60) == ()
    hourly = make_position(contract=HOURLY_FEE, monthly_hours=180, person=PERSON)  # 45 h a la semana
    assert [item.weekly_hours for item in workload_warnings([hourly], 2026, full_time_minutes=44 * 60)] == [Decimal(45)]
    assert workload_warnings([hourly], 2026, full_time_minutes=48 * 60) == ()


def test_salaried_and_fee_positions_are_summed_together() -> None:
    """Una posición dependiente de 44 h más honorarios de 7,5 h suman 51,5 h todo el año: un solo aviso."""
    positions = [
        make_position(person=PERSON, contract=SALARIED),
        make_position(person=PERSON, weekly_hours="7.5", contract=WEEKLY_FEE),
    ]
    warnings = workload_warnings(positions, 2026, full_time_minutes=FULL_TIME_MINUTES)
    assert len(warnings) == 1
    assert (warnings[0].start, warnings[0].end, warnings[0].weekly_hours, warnings[0].concurrent) == (
        date(2026, 1, 1),
        date(2026, 12, 31),
        Decimal("51.5"),
        2,
    )
    # Dos posiciones dependientes que suman exactamente 44 h no superan la jornada completa.
    two_salaried = [make_position(person=PERSON, contract=SALARIED, weekly_hours=22) for _ in range(2)]
    assert workload_warnings(two_salaried, 2026, full_time_minutes=FULL_TIME_MINUTES) == ()


def test_hourly_contract_adds_its_weekly_equivalent() -> None:
    """AMB-06: 44 h semanales más 38 h mensuales (9,5 h a la semana) suman 53,5 h todo el año."""
    positions = [
        make_position(person=PERSON),
        make_position(contract=HOURLY_FEE, monthly_hours=38, person=PERSON),
    ]
    warnings = workload_warnings(positions, 2026, full_time_minutes=FULL_TIME_MINUTES)
    assert [(item.start, item.end, item.weekly_hours) for item in warnings] == [
        (date(2026, 1, 1), date(2026, 12, 31), Decimal("53.5")),
    ]
    assert all(item.includes_hourly for item in warnings)
    assert "53,5 h" in warnings[0].message
    assert "equivalente semanal de contratos por horas" in warnings[0].message
    five_weeks = workload_warnings(positions, 2026, Decimal(5), FULL_TIME_MINUTES)
    assert five_weeks[0].weekly_hours == Decimal("51.6")


def test_no_warning_for_vacancies_or_light_loads() -> None:
    positions = [
        make_position(),
        make_position(person=Person(8, "Jornada parcial"), weekly_hours=30),
        make_position(person=Person(8, "Jornada parcial"), contract=HOURLY_FEE, monthly_hours=40),
    ]
    assert workload_warnings(positions, 2026, full_time_minutes=FULL_TIME_MINUTES) == ()


def test_warning_limited_to_overlap_period() -> None:
    positions = [
        make_position(person=PERSON, weekly_hours=30),
        make_position(person=PERSON, weekly_hours=15, start=date(2026, 9, 1), end=date(2026, 10, 15)),
    ]
    warnings = workload_warnings(positions, 2026, full_time_minutes=FULL_TIME_MINUTES)
    assert [(item.start, item.end) for item in warnings] == [(date(2026, 9, 1), date(2026, 10, 15))]


@pytest.mark.parametrize(
    ("body", "digit"), [(41234567, "3"), (43210987, "9"), (42000001, "4"), (42000003, "0"), (41000013, "K")]
)
def test_rut_check_digit(body: int, digit: str) -> None:
    assert check_digit(body) == digit


def test_rut_normalization() -> None:
    assert normalize_rut("41.234.567-3") == "41234567-3"
    assert normalize_rut(" 412345673 ") == "41234567-3"
    assert normalize_rut("41000013-k") == "41000013-K"
    assert format_rut("41234567-3") == "41.234.567-3"
    with pytest.raises(ValidationError, match="verificador"):
        normalize_rut("41.234.567-4")
    with pytest.raises(ValidationError, match="formato"):
        normalize_rut("abc")


def test_units() -> None:
    assert round_pesos(Decimal("0.5")) == 1
    assert round_pesos(Decimal("2.5")) == 3
    assert round_pesos(Decimal("2.4999")) == 2
    assert hours_to_minutes("7,5") == 450
    assert minutes_to_hours(450) == Decimal("7.5")
    assert fraction_to_bp(Decimal("0.1525")) == 1525
    assert bp_to_fraction(1525) == Decimal("0.1525")
    assert safe_ratio(1, 0) is None
    assert to_decimal(0.1) == Decimal("0.1")
    with pytest.raises(ValidationError):
        fraction_to_bp(Decimal("0.00001"))
    with pytest.raises(ValidationError):
        to_decimal("NaN")
    with pytest.raises(ValidationError):
        to_decimal("doce")


def test_identical_positions_are_found_but_concurrent_ones_are_not() -> None:
    """Regresión BUG-11: se permiten posiciones concurrentes, pero un registro idéntico se detecta."""
    existing = [
        make_position(person=PERSON, position_id=1),
        make_position(weekly_hours=22, position_id=2),
    ]
    same_person = validate_position_draft(_draft(person_id=PERSON.id), WEEKLY_FEE, 2026)
    assert find_identical(same_person, existing) is existing[0]
    assert find_identical(same_person, existing, exclude_id=1) is None
    concurrent = validate_position_draft(_draft(person_id=PERSON.id, program_id=2), WEEKLY_FEE, 2026)
    assert find_identical(concurrent, existing) is None
    vacancy = validate_position_draft(_draft(weekly_hours=Decimal(22), quantity=3), WEEKLY_FEE, 2026)
    assert find_identical(vacancy, existing) is existing[1]
    message = identical_position_message(existing[1])
    assert message.startswith("Es idéntica a una posición que ya está en el escenario (Psicólogo, Vacante")
    assert "aumente la cantidad" in message
    assert PositionIdentity.of_valid(vacancy) == PositionIdentity.of_position(existing[1])
