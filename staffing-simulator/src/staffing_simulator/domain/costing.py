"""Motor de costeo mensual por posición.

Reglas (ver README):

- Honorarios con jornada semanal: bruto = tarifa x horas semanales x semanas por
  mes (4 por convención). Meses parciales en modo proporcional: el bruto se
  multiplica por la fracción de horas programadas del mes que caen dentro de la
  vigencia, con el calendario real día a día. La fracción es un cociente entre
  horas de calendario, de modo que nunca se mezclan con la convención de
  semanas: un tramo con horas vigentes nunca cuesta 0 y dividir una posición
  en tramos consecutivos no cambia su costo.
- Honorarios por horas: bruto = tarifa x horas mensuales estimadas; en meses
  parciales se multiplica por la fracción de días hábiles vigentes.
- Plazo fijo y planta (dependientes): el programa paga solo el sueldo del
  grado 15 de la categoría de la persona (parámetro con vigencia y reajustes
  del sector público), prorrateado por horas semanales / jornada completa; en
  meses parciales, por días corridos vigentes / días del mes. Se suma el
  aporte del empleador y el ausentismo no reduce el costo. Si la persona tiene
  otro grado, la diferencia la asume el municipio y no se imputa al programa.
- Ausentismo en honorarios: las horas no trabajadas son el porcentaje de
  ausentismo aplicado a las horas contratadas del mes (horas semanales x
  semanas por mes, prorrateadas, o las horas mensuales estimadas), y
  pago = min(bruto, max(bruto - tarifa x horas no trabajadas, 0)). Así el
  porcentaje descuenta lo mismo en los dos tipos de honorarios.
- Cada monto por posición y mes se redondea a peso entero antes de multiplicar
  por la cantidad y de agregar.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from staffing_simulator.domain.calendar import (
    WeeklySchedule,
    business_days,
    days_in_month,
    intersect,
    month_range,
)
from staffing_simulator.domain.models import MONTHS, CostMethod, PartialMonthMethod, Position, Scenario
from staffing_simulator.domain.parameters import CostParameters
from staffing_simulator.domain.units import MINUTES_PER_HOUR, ZERO, minutes_to_hours, round_pesos
from staffing_simulator.errors import ValidationError

HOURS_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class CostLine:
    """Costo de una posición en un mes, con los montos ya multiplicados por la cantidad.

    - `base`: bruto programado del mes después del prorrateo.
    - `absence_discount`: descuento por ausentismo esperado (solo honorarios).
    - `gross`: bruto a pagar (`base - absence_discount`).
    - `employer_contribution`: aporte del empleador (solo contratos dependientes).
    - `cost`: costo para la organización (`gross + employer_contribution`).
    - `retention`: retención de honorarios, informativa: no cambia el costo.
    """

    position_id: int
    month: int
    active: bool
    hourly_rate: Decimal
    contract_hours: Decimal
    base: int
    absence_discount: int
    gross: int
    employer_contribution: int
    cost: int
    retention: int
    applies_retention: bool

    @property
    def net(self) -> int:
        """Líquido estimado del prestador (bruto menos retención)."""
        return self.gross - self.retention


def fee_after_absence(gross: Decimal, hourly_rate: Decimal, absent_hours: Decimal) -> Decimal:
    """Honorario después de descontar horas no trabajadas, acotado entre 0 y el bruto."""
    if absent_hours < 0:
        raise ValidationError("Las horas no trabajadas no pueden ser negativas.")
    return min(gross, max(gross - hourly_rate * absent_hours, ZERO))


def weekly_gross(hourly_rate: Decimal, weekly_hours: Decimal, weeks_per_month: Decimal) -> Decimal:
    """Bruto mensual de una jornada semanal (convención de semanas por mes)."""
    return hourly_rate * weekly_hours * weeks_per_month


def retention_amount(gross: int, rate: Decimal) -> int:
    """Retención de honorarios redondeada a peso entero."""
    return round_pesos(Decimal(gross) * rate)


def scheduled_fraction(
    schedule: WeeklySchedule, month_span: tuple[date, date], worked_span: tuple[date, date]
) -> Decimal:
    """Fracción de las horas programadas del mes que caen en el tramo vigente (entre 0 y 1)."""
    month_minutes = schedule.minutes_between(*month_span)
    if month_minutes == 0:
        return ZERO
    return Decimal(schedule.minutes_between(*worked_span)) / Decimal(month_minutes)


def _inactive_line(position_id: int, month: int, applies_retention: bool) -> CostLine:
    return CostLine(
        position_id=position_id,
        month=month,
        active=False,
        hourly_rate=ZERO,
        contract_hours=ZERO,
        base=0,
        absence_discount=0,
        gross=0,
        employer_contribution=0,
        cost=0,
        retention=0,
        applies_retention=applies_retention,
    )


@dataclass(frozen=True)
class _MonthAmounts:
    """Montos e indicadores unitarios de un mes antes de redondear."""

    base: Decimal
    gross: Decimal
    contract_hours: Decimal
    hourly_rate: Decimal


def _weekly_fee_amounts(
    position: Position,
    rate: Decimal,
    month_span: tuple[date, date],
    worked_span: tuple[date, date],
    scenario: Scenario,
    params: CostParameters,
) -> _MonthAmounts:
    weekly_minutes = _required_weekly_minutes(position)
    schedule = WeeklySchedule.spread_over_weekdays(weekly_minutes)
    fraction = scheduled_fraction(schedule, month_span, worked_span)
    contract_hours = minutes_to_hours(weekly_minutes) * params.settings.weeks_per_month * fraction
    base = rate * contract_hours
    absent_hours = scenario.expected_absence * contract_hours
    return _MonthAmounts(
        base=base, gross=fee_after_absence(base, rate, absent_hours), contract_hours=contract_hours, hourly_rate=rate
    )


def _hourly_fee_amounts(
    position: Position,
    rate: Decimal,
    month_span: tuple[date, date],
    worked_span: tuple[date, date],
    scenario: Scenario,
) -> _MonthAmounts:
    if position.monthly_minutes is None:
        raise ValidationError(
            f"La posición {position.id} es un contrato por horas y no tiene horas mensuales estimadas."
        )
    monthly_hours = minutes_to_hours(position.monthly_minutes)
    fraction = Decimal(business_days(*worked_span)) / Decimal(business_days(*month_span))
    hours = monthly_hours * fraction
    base = rate * hours
    absent_hours = scenario.expected_absence * hours
    return _MonthAmounts(
        base=base, gross=fee_after_absence(base, rate, absent_hours), contract_hours=hours, hourly_rate=rate
    )


def _salaried_amounts(
    position: Position,
    month_span: tuple[date, date],
    worked_span: tuple[date, date],
    params: CostParameters,
) -> _MonthAmounts:
    weekly_minutes = _required_weekly_minutes(position)
    full_time_minutes = Decimal(params.settings.full_time_weekly_minutes)
    monthly_salary = params.salary.amount_for(position.job_role.category, month_span[0])
    full = monthly_salary * Decimal(weekly_minutes) / full_time_minutes
    worked_days = (worked_span[1] - worked_span[0]).days + 1
    month_days = days_in_month(month_span[0].year, month_span[0].month)
    base = full * Decimal(worked_days) / Decimal(month_days)
    contract_hours = (
        minutes_to_hours(weekly_minutes) * params.settings.weeks_per_month * Decimal(worked_days) / Decimal(month_days)
    )
    full_time_monthly_hours = (full_time_minutes / Decimal(MINUTES_PER_HOUR)) * params.settings.weeks_per_month
    hourly_rate = monthly_salary / full_time_monthly_hours if full_time_monthly_hours else ZERO
    return _MonthAmounts(base=base, gross=base, contract_hours=contract_hours, hourly_rate=hourly_rate)


def _required_weekly_minutes(position: Position) -> int:
    if position.weekly_minutes is None:
        raise ValidationError(f"La posición {position.id} requiere horas semanales para su tipo de contrato.")
    return position.weekly_minutes


def cost_position(position: Position, scenario: Scenario, params: CostParameters) -> tuple[CostLine, ...]:
    """Doce líneas de costo (una por mes del año del escenario) para una posición."""
    contract_type = position.contract_type
    applies_retention = contract_type.applies_retention
    period = position.period_in_year(scenario.year)
    if period is None:
        return tuple(_inactive_line(position.id, month, applies_retention) for month in MONTHS)

    contribution_rate = params.contributions.rate_for(contract_type, scenario.year)
    method = contract_type.cost_method
    lines: list[CostLine] = []
    for month in MONTHS:
        month_span = month_range(scenario.year, month)
        active_span = intersect(period, month_span)
        if active_span is None:
            lines.append(_inactive_line(position.id, month, applies_retention))
            continue
        worked_span = month_span if scenario.partial_month_method is PartialMonthMethod.FULL_MONTH else active_span
        if method is CostMethod.WEEKLY_FEE:
            rate = params.rates.hourly_rate(position.job_role, scenario.year)
            amounts = _weekly_fee_amounts(position, rate, month_span, worked_span, scenario, params)
        elif method is CostMethod.HOURLY_FEE:
            rate = params.rates.hourly_rate(position.job_role, scenario.year)
            amounts = _hourly_fee_amounts(position, rate, month_span, worked_span, scenario)
        else:
            amounts = _salaried_amounts(position, month_span, worked_span, params)

        unit_base = round_pesos(amounts.base)
        unit_gross = round_pesos(amounts.gross)
        unit_contribution = round_pesos(Decimal(unit_gross) * contribution_rate)
        unit_retention = 0
        if applies_retention:
            retention_year = params.settings.retention_year(scenario.year, month)
            unit_retention = retention_amount(unit_gross, params.retention.rate_for(retention_year))
        quantity = position.quantity
        lines.append(
            CostLine(
                position_id=position.id,
                month=month,
                active=True,
                hourly_rate=amounts.hourly_rate,
                contract_hours=(amounts.contract_hours * quantity).quantize(HOURS_QUANTUM),
                base=unit_base * quantity,
                absence_discount=(unit_base - unit_gross) * quantity,
                gross=unit_gross * quantity,
                employer_contribution=unit_contribution * quantity,
                cost=(unit_gross + unit_contribution) * quantity,
                retention=unit_retention * quantity,
                applies_retention=applies_retention,
            )
        )
    return tuple(lines)


def cost_positions(positions: Iterable[Position], scenario: Scenario, params: CostParameters) -> tuple[CostLine, ...]:
    """Líneas de costo de todas las posiciones de un escenario."""
    lines: list[CostLine] = []
    for position in positions:
        lines.extend(cost_position(position, scenario, params))
    return tuple(lines)
