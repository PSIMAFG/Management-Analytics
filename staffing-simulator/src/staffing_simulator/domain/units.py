"""Conversión y redondeo de dinero, porcentajes y horas.

Reglas del proyecto:

- El dinero se calcula con `Decimal` y cada monto por posición y mes se
  redondea a peso entero con `ROUND_HALF_UP` antes de agregarlo.
- Los porcentajes se representan como fracciones `Decimal` (0,1525 = 15,25 %)
  y se guardan en la base como puntos básicos enteros (1525).
- Las horas se guardan como minutos enteros para evitar errores de coma
  flotante (7,5 h = 450 minutos).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from staffing_simulator.errors import ValidationError

ZERO = Decimal(0)
ONE = Decimal(1)
MINUTES_PER_HOUR = 60
BASIS_POINTS = Decimal(10_000)


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Convierte a Decimal sin arrastrar el error binario de los float."""
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip().replace(",", "."))
        except InvalidOperation as error:
            raise ValidationError(f"El valor '{value}' no es un número válido.") from error
    if not result.is_finite():
        raise ValidationError(f"El valor '{value}' no es un número válido.")
    return result


def round_pesos(amount: Decimal) -> int:
    """Redondea un monto a peso entero con la regla ROUND_HALF_UP (0,5 sube)."""
    return int(amount.quantize(ONE, rounding=ROUND_HALF_UP))


def hours_to_minutes(hours: Decimal | int | float | str) -> int:
    """Convierte horas a minutos enteros; rechaza fracciones menores a un minuto."""
    minutes = to_decimal(hours) * MINUTES_PER_HOUR
    if minutes != minutes.to_integral_value():
        raise ValidationError(f"Las horas deben expresarse en minutos enteros (valor recibido: {hours}).")
    return int(minutes)


def minutes_to_hours(minutes: int) -> Decimal:
    """Minutos enteros a horas decimales exactas (450 -> 7.5)."""
    return Decimal(minutes) / MINUTES_PER_HOUR


def fraction_to_bp(fraction: Decimal) -> int:
    """Fracción a puntos básicos enteros (0,1525 -> 1525)."""
    points = fraction * BASIS_POINTS
    if points != points.to_integral_value():
        raise ValidationError("Los porcentajes admiten como máximo dos decimales (por ejemplo, 15,25 %).")
    return int(points)


def bp_to_fraction(points: int) -> Decimal:
    """Puntos básicos a fracción exacta (1525 -> 0.1525)."""
    return Decimal(points) / BASIS_POINTS


def safe_ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal | None:
    """Cociente exacto o None cuando el denominador es cero."""
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)
