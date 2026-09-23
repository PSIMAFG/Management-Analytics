"""Calendario día a día: horas programadas por mes y reparto de la jornada (BUG-03, BUG-04)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from staffing_simulator.domain.calendar import (
    WeeklySchedule,
    business_days,
    days_in_month,
    intersect,
    month_range,
)
from staffing_simulator.domain.units import hours_to_minutes
from staffing_simulator.errors import ValidationError

# Horario de 44 h: lunes a jueves 9 h y viernes 8 h.
SCHEDULE_44 = WeeklySchedule.from_hours(9, 9, 9, 9, 8)
EXPECTED_2026 = (193, 176, 194, 194, 184, 194, 202, 185, 194, 193, 185, 203)


def test_monthly_hours_2026_match_real_calendar() -> None:
    hours = tuple(minutes // 60 for minutes in SCHEDULE_44.monthly_minutes(2026))
    assert hours == EXPECTED_2026


@pytest.mark.parametrize(
    ("year", "month", "tail_start", "tail_hours"),
    [
        (2026, 3, date(2026, 3, 30), 18),  # marzo 2026 parte en domingo: 30 y 31 son lunes y martes
        (2026, 8, date(2026, 8, 31), 9),  # agosto 2026 parte en sábado: el 31 es lunes
        (2026, 11, date(2026, 11, 30), 9),  # noviembre 2026 parte en domingo: el 30 es lunes
        (2027, 5, date(2027, 5, 31), 9),  # mayo 2027 parte en sábado
        (2027, 8, date(2027, 8, 30), 18),  # agosto 2027 parte en domingo
    ],
)
def test_months_touching_six_weeks_count_their_last_days(
    year: int, month: int, tail_start: date, tail_hours: int
) -> None:
    """Regresión BUG-03: los últimos días de un mes que toca seis semanas también se cuentan."""
    first, last = month_range(year, month)
    tail = SCHEDULE_44.minutes_between(tail_start, last)
    assert tail == tail_hours * 60
    total = SCHEDULE_44.minutes_between(first, last)
    five_calendar_weeks = SCHEDULE_44.minutes_between(first, tail_start.replace(day=tail_start.day - 1))
    assert total == five_calendar_weeks + tail


def test_march_2026_has_194_hours_not_176() -> None:
    first, last = month_range(2026, 3)
    assert SCHEDULE_44.minutes_between(first, last) == 194 * 60


@pytest.mark.parametrize(
    ("weekly_hours", "expected"),
    [
        ("44", ("9", "9", "9", "9", "8")),
        ("22", ("5", "5", "4", "4", "4")),
        ("7.5", ("2", "2", "1.5", "1", "1")),
        ("6", ("2", "1", "1", "1", "1")),
        ("0.5", ("0.5", "0", "0", "0", "0")),
    ],
)
def test_spread_over_weekdays(weekly_hours: str, expected: tuple[str, ...]) -> None:
    schedule = WeeklySchedule.spread_over_weekdays(hours_to_minutes(weekly_hours))
    assert schedule.minutes_by_weekday[:5] == tuple(hours_to_minutes(value) for value in expected)
    assert schedule.minutes_by_weekday[5:] == (0, 0)


@pytest.mark.parametrize(
    "weekly_hours", ["44", "41", "37", "35", "33", "32", "30", "28", "22", "18", "15", "11", "7.5", "6"]
)
def test_schedule_always_adds_up_to_weekly_hours(weekly_hours: str) -> None:
    """Regresión BUG-04: la jornada y el horario no pueden quedar inconsistentes."""
    minutes = hours_to_minutes(weekly_hours)
    assert WeeklySchedule.spread_over_weekdays(minutes).total_minutes == minutes


def test_schedule_validation() -> None:
    with pytest.raises(ValidationError):
        WeeklySchedule.spread_over_weekdays(0)
    with pytest.raises(ValidationError):
        WeeklySchedule((60, 60, 60, 60, 60, 0))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        WeeklySchedule((-60, 0, 0, 0, 0, 0, 0))
    with pytest.raises(ValidationError):
        WeeklySchedule.from_hours(25)
    with pytest.raises(ValidationError):
        WeeklySchedule.from_hours(Decimal("1.001"))


def test_empty_range_has_no_minutes() -> None:
    assert SCHEDULE_44.minutes_between(date(2026, 3, 10), date(2026, 3, 9)) == 0


def test_holidays_are_not_deducted_from_scheduled_hours() -> None:
    """Regresión AMB-10: un feriado no descuenta horas programadas en honorarios con jornada semanal."""
    holiday = date(2026, 9, 18)  # feriado nacional chileno, cae viernes en 2026
    ordinary_friday = date(2026, 9, 11)
    assert holiday.weekday() == ordinary_friday.weekday() == 4
    assert SCHEDULE_44.minutes_on(holiday) == SCHEDULE_44.minutes_on(ordinary_friday) == 8 * 60
    september = SCHEDULE_44.minutes_between(*month_range(2026, 9))
    assert september == 194 * 60  # el feriado del 18 no se resta del total del mes


def test_business_days_and_helpers() -> None:
    assert business_days(*month_range(2026, 3)) == 22
    assert business_days(date(2026, 3, 16), date(2026, 3, 31)) == 12
    assert business_days(date(2026, 3, 7), date(2026, 3, 8)) == 0
    assert days_in_month(2028, 2) == 29
    assert intersect((date(2026, 1, 1), date(2026, 3, 31)), (date(2026, 3, 1), date(2026, 3, 31))) == (
        date(2026, 3, 1),
        date(2026, 3, 31),
    )
    assert intersect((date(2026, 1, 1), date(2026, 2, 28)), (date(2026, 3, 1), date(2026, 3, 31))) is None
