"""Calendario real día a día para las horas programadas.

La jornada semanal se reparte en los días de la semana y las horas de un
mes se obtienen recorriendo todos sus días. No hay columnas fijas por
semana, de modo que los meses que tocan seis semanas calendario se cuentan
completos. Los feriados no se descuentan: un feriado no genera descuento en
honorarios con jornada semanal.
"""

from __future__ import annotations

import calendar as std_calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from staffing_simulator.domain.units import MINUTES_PER_HOUR, hours_to_minutes
from staffing_simulator.errors import ValidationError

WORKING_WEEKDAYS = 5
ONE_DAY = timedelta(days=1)


def month_range(year: int, month: int) -> tuple[date, date]:
    """Primer y último día del mes."""
    return date(year, month, 1), date(year, month, std_calendar.monthrange(year, month)[1])


def days_in_month(year: int, month: int) -> int:
    return std_calendar.monthrange(year, month)[1]


def iter_days(start: date, end: date) -> Iterator[date]:
    """Días entre `start` y `end`, ambos incluidos."""
    day = start
    while day <= end:
        yield day
        day += ONE_DAY


def intersect(first: tuple[date, date], second: tuple[date, date]) -> tuple[date, date] | None:
    """Intersección de dos tramos cerrados de fechas, o None si no se tocan."""
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    return (start, end) if start <= end else None


def business_days(start: date, end: date) -> int:
    """Días hábiles de lunes a viernes entre dos fechas incluidas."""
    return sum(1 for day in iter_days(start, end) if day.weekday() < WORKING_WEEKDAYS)


@dataclass(frozen=True)
class WeeklySchedule:
    """Minutos programados por día de la semana (lunes = 0 ... domingo = 6)."""

    minutes_by_weekday: tuple[int, int, int, int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.minutes_by_weekday) != 7:
            raise ValidationError("El horario semanal debe tener los siete días de la semana.")
        if any(minutes < 0 for minutes in self.minutes_by_weekday):
            raise ValidationError("Las horas de un día no pueden ser negativas.")
        if any(minutes > 24 * MINUTES_PER_HOUR for minutes in self.minutes_by_weekday):
            raise ValidationError("Un día no puede tener más de 24 horas programadas.")

    @classmethod
    def from_hours(cls, *hours: Decimal | int | float | str) -> WeeklySchedule:
        """Horario desde horas por día, de lunes en adelante (los días omitidos quedan en 0)."""
        if len(hours) > 7:
            raise ValidationError("El horario semanal tiene como máximo siete días.")
        minutes = [hours_to_minutes(value) for value in hours] + [0] * (7 - len(hours))
        return cls((minutes[0], minutes[1], minutes[2], minutes[3], minutes[4], minutes[5], minutes[6]))

    @classmethod
    def spread_over_weekdays(cls, weekly_minutes: int) -> WeeklySchedule:
        """Reparte la jornada de lunes a viernes.

        Las horas enteras se reparten lo más parejo posible comenzando el
        lunes (44 h queda como 9, 9, 9, 9 y 8) y los minutos sobrantes se
        asignan al primer día con menos carga (7,5 h queda como 2, 2, 1,5, 1 y 1).
        """
        if weekly_minutes <= 0:
            raise ValidationError("La jornada semanal debe ser mayor que cero.")
        whole_hours, extra_minutes = divmod(weekly_minutes, MINUTES_PER_HOUR)
        base, extra_hours = divmod(whole_hours, WORKING_WEEKDAYS)
        days = [(base + (1 if index < extra_hours else 0)) * MINUTES_PER_HOUR for index in range(WORKING_WEEKDAYS)]
        if extra_minutes:
            lightest = days.index(min(days))
            days[lightest] += extra_minutes
        return cls((days[0], days[1], days[2], days[3], days[4], 0, 0))

    @property
    def total_minutes(self) -> int:
        return sum(self.minutes_by_weekday)

    def minutes_on(self, day: date) -> int:
        return self.minutes_by_weekday[day.weekday()]

    def minutes_between(self, start: date, end: date) -> int:
        """Minutos programados entre dos fechas incluidas (0 si el tramo está vacío)."""
        return sum(self.minutes_on(day) for day in iter_days(start, end))

    def monthly_minutes(self, year: int) -> tuple[int, ...]:
        """Minutos programados en cada mes del año según el calendario real."""
        return tuple(self.minutes_between(*month_range(year, month)) for month in range(1, 13))
