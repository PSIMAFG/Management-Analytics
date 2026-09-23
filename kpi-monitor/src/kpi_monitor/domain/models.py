"""Modelos inmutables del dominio: período, sedes, programas, indicadores y metas.

Cada indicador se define con campos declarativos (cómo se acumula el
numerador, de dónde sale el denominador, dirección, escala, techo natural)
en lugar de clases ad hoc. Las validaciones de consistencia se hacen al
construir el objeto, de modo que un indicador mal definido no llega al motor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from kpi_monitor.domain.enums import (
    Aggregation,
    DenominatorType,
    Direction,
    GoalRuleType,
    Origin,
    Scale,
)
from kpi_monitor.domain.text import period_label
from kpi_monitor.errors import ValidationError

MIN_YEAR = 2000
MAX_YEAR = 2100
# Denominadores cuyo valor no crece con el paso de los meses.
_STATIC_DENOMINATORS = (
    DenominatorType.FIXED_SITE,
    DenominatorType.STOCK,
    DenominatorType.K_STOCK,
    DenominatorType.POPULATION,
)
_STOCK_DENOMINATORS = (DenominatorType.STOCK, DenominatorType.K_STOCK)
_FLOW_DENOMINATORS = (DenominatorType.FLOW, DenominatorType.MANUAL)


def _check_months(months: tuple[int, ...], label: str) -> None:
    if any(not 1 <= m <= 12 for m in months):
        raise ValidationError(f"Los meses de {label} deben estar entre 1 y 12.")
    if list(months) != sorted(set(months)):
        raise ValidationError(f"Los meses de {label} deben ir en orden creciente y sin repetirse.")


@dataclass(frozen=True, order=True)
class Period:
    """Período de corte explícito (año y mes). Nunca se deduce de la fecha del sistema."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not MIN_YEAR <= self.year <= MAX_YEAR:
            raise ValidationError(f"El año {self.year} está fuera del rango permitido.")
        if not 1 <= self.month <= 12:
            raise ValidationError(f"El mes {self.month} no es válido; debe estar entre 1 y 12.")

    @property
    def label(self) -> str:
        return period_label(self.year, self.month)

    def previous(self) -> Period:
        """Mes anterior (diciembre del año previo si el período es enero)."""
        if self.month == 1:
            return Period(self.year - 1, 12)
        return Period(self.year, self.month - 1)


@dataclass(frozen=True)
class Site:
    """Sede de la organización. Se identifica siempre por código, nunca por nombre."""

    code: str
    name: str
    kind: str
    sort_order: int = 0


@dataclass(frozen=True)
class Program:
    """Programa que agrupa indicadores y define su propio índice ponderado."""

    code: str
    name: str
    description: str = ""
    sort_order: int = 0


@dataclass(frozen=True)
class IndicatorSheet:
    """Ficha técnica del indicador en lenguaje de gestión."""

    measures: str
    numerator: str
    denominator: str
    source: str
    zero_meaning: str
    notes: str = ""


@dataclass(frozen=True)
class Indicator:
    """Definición declarativa de un indicador.

    `direction` es obligatoria y no tiene valor neutro: de ella dependen el
    cumplimiento, el semáforo, la proyección y las alertas.
    """

    code: str
    program_code: str
    name: str
    short_name: str
    aggregation: Aggregation
    denominator_type: DenominatorType
    direction: Direction
    scale: Scale
    sheet: IndicatorSheet
    multiplier: float = 1.0
    natural_ceiling: float | None = None
    stock_months: tuple[int, ...] = ()
    cut_months: tuple[int, ...] = ()
    sort_order: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.direction, Direction):
            raise ValidationError(f"El indicador {self.code} debe declarar su dirección (mayor o menor es mejor).")
        if not isinstance(self.aggregation, Aggregation) or not isinstance(self.denominator_type, DenominatorType):
            raise ValidationError(f"El indicador {self.code} tiene un tipo de numerador o denominador inválido.")
        if self.multiplier <= 0:
            raise ValidationError(f"El multiplicador del indicador {self.code} debe ser positivo.")
        if self.denominator_type is not DenominatorType.K_STOCK and self.multiplier != 1.0:
            raise ValidationError(f"El indicador {self.code} solo puede usar multiplicador con denominador k × stock.")
        if self.natural_ceiling is not None and self.natural_ceiling <= 0:
            raise ValidationError(f"El techo natural del indicador {self.code} debe ser positivo.")
        _check_months(self.stock_months, f"stock del indicador {self.code}")
        _check_months(self.cut_months, f"corte del indicador {self.code}")
        if self.uses_stock and not self.stock_months:
            raise ValidationError(f"El indicador {self.code} usa un stock y debe declarar sus meses de corte de stock.")
        if self.aggregation is Aggregation.STOCK and self.denominator_type in _STOCK_DENOMINATORS:
            raise ValidationError(f"El indicador {self.code} no puede tener stock en el numerador y en el denominador.")

    @property
    def uses_stock(self) -> bool:
        return self.aggregation is Aggregation.STOCK or self.denominator_type in _STOCK_DENOMINATORS

    @property
    def has_flow_denominator(self) -> bool:
        return self.denominator_type in _FLOW_DENOMINATORS

    @property
    def has_stock_denominator(self) -> bool:
        return self.denominator_type in _STOCK_DENOMINATORS

    @property
    def grows_with_time(self) -> bool:
        """El valor acumulado crece con los meses: flujo sobre un denominador que no se acumula.

        Solo en ese caso la meta se prorratea (meta × mes / 12).
        """
        return self.aggregation is Aggregation.FLOW and self.denominator_type in _STATIC_DENOMINATORS

    def expects_data_in(self, month: int) -> bool:
        """Meses en que la sede debe informar: todos si el numerador es flujo; los de corte si es stock."""
        if self.aggregation is Aggregation.STOCK:
            return month in self.stock_months
        return True

    def expects_data_until(self, month: int) -> bool:
        """Indica si entre enero y `month` corresponde informar al menos un dato.

        Es falso en un numerador de stock antes de su primer corte del año: en esos
        meses no falta nada, el indicador todavía no se puede medir.
        """
        return any(self.expects_data_in(m) for m in range(1, month + 1))

    @property
    def first_stock_month(self) -> int | None:
        """Primer mes de corte del stock en el año, o None si el indicador no usa stock."""
        return self.stock_months[0] if self.stock_months else None

    @property
    def frequency_label(self) -> str:
        if self.aggregation is Aggregation.STOCK:
            return "En meses de corte de stock"
        return "Mensual"


@dataclass(frozen=True)
class GoalBand:
    """Tramo de cumplimiento: valores hasta `upper_bound` (inclusive) obtienen `compliance`.

    Los tramos son intervalos semiabiertos (cota anterior, cota]; el último no tiene cota.
    """

    upper_bound: float | None
    compliance: float


@dataclass(frozen=True)
class GoalRule:
    """Regla de meta y peso de un indicador para un año."""

    indicator_code: str
    year: int
    rule_type: GoalRuleType
    weight: float
    value: float | None = None
    factor: float | None = None
    ceiling: float | None = None
    bands: tuple[GoalBand, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        label = f"la regla de meta de {self.indicator_code} ({self.year})"
        if not 0 <= self.weight <= 1:
            raise ValidationError(f"El peso de {label} debe estar entre 0 % y 100 %.")
        if self.rule_type is GoalRuleType.ABSOLUTE and (self.value is None or self.value < 0):
            raise ValidationError(f"Falta el valor de {label} o es negativo.")
        if self.rule_type.needs_reference and (self.factor is None or self.factor <= 0):
            raise ValidationError(f"El factor de aumento de {label} debe ser positivo.")
        if self.rule_type is GoalRuleType.CAPPED_INCREASE and (self.ceiling is None or self.ceiling <= 0):
            raise ValidationError(f"Falta el tope de {label}.")
        if self.rule_type is GoalRuleType.BANDS:
            _check_bands(self.bands, label)
        elif self.bands:
            raise ValidationError(f"Solo las reglas por tramos pueden definir tramos ({label}).")


def _check_bands(bands: tuple[GoalBand, ...], label: str) -> None:
    if len(bands) < 2:
        raise ValidationError(f"{label.capitalize()} debe tener al menos dos tramos.")
    bounds = [band.upper_bound for band in bands]
    if bounds[-1] is not None or any(bound is None for bound in bounds[:-1]):
        raise ValidationError(f"Solo el último tramo de {label} puede quedar sin cota superior.")
    finite = [bound for bound in bounds[:-1] if bound is not None]
    if any(nxt <= prev for prev, nxt in pairwise(finite)) or finite[0] < 0:
        raise ValidationError(f"Las cotas de {label} deben ser crecientes y no negativas.")
    if any(not 0 <= band.compliance <= 1 for band in bands):
        raise ValidationError(f"El cumplimiento de cada tramo de {label} debe estar entre 0 % y 100 %.")


@dataclass(frozen=True)
class SiteGoal:
    """Meta fija anual de una sede (denominador de los indicadores de compromiso fijo)."""

    indicator_code: str
    year: int
    site_code: str
    target: float

    def __post_init__(self) -> None:
        if self.target <= 0:
            raise ValidationError(
                f"La meta fija de {self.indicator_code} para {self.site_code} ({self.year}) debe ser positiva."
            )


@dataclass(frozen=True)
class SitePopulation:
    """Población de referencia de una sede en un año."""

    site_code: str
    year: int
    population: int

    def __post_init__(self) -> None:
        if self.population <= 0:
            raise ValidationError(f"La población de referencia de {self.site_code} ({self.year}) debe ser positiva.")


@dataclass(frozen=True)
class Observation:
    """Dato mensual de un indicador en una sede.

    `reported` distingue un mes informado (aunque sea con cero) de un mes
    faltante: un faltante nunca se interpreta como cero.
    """

    indicator_code: str
    site_code: str
    year: int
    month: int
    numerator: float | None
    denominator: float | None
    reported: bool = True
    origin: Origin = Origin.SYNTHETIC

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValidationError(f"El mes {self.month} no es válido.")
        if not MIN_YEAR <= self.year <= MAX_YEAR:
            raise ValidationError(f"El año {self.year} está fuera del rango permitido.")
        for value in (self.numerator, self.denominator):
            if value is not None and value < 0:
                raise ValidationError("El numerador y el denominador no pueden ser negativos.")
        if not self.reported and (self.numerator is not None or self.denominator is not None):
            raise ValidationError("Un mes marcado como no reportado no puede traer valores.")

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.indicator_code, self.site_code, self.year, self.month)


@dataclass(frozen=True)
class Thresholds:
    """Umbrales del semáforo oficial sobre el cumplimiento a la fecha."""

    green: float = 1.0
    yellow: float = 0.85

    def __post_init__(self) -> None:
        if not 0 < self.yellow < self.green:
            raise ValidationError("El umbral amarillo debe ser positivo y menor que el umbral verde.")


@dataclass(frozen=True)
class MonitorSettings:
    """Parámetros de cálculo configurables."""

    thresholds: Thresholds = field(default_factory=Thresholds)
    prevalence: float = 0.20
    default_period: Period = field(default_factory=lambda: Period(2026, 8))
    bootstrap_draws: int = 2000
    bootstrap_seed: int = 20260
    min_months_projection: int = 3

    def __post_init__(self) -> None:
        if not 0 < self.prevalence <= 1:
            raise ValidationError("La prevalencia debe estar entre 0 % y 100 %.")
        if self.bootstrap_draws < 100:
            raise ValidationError("El bootstrap necesita al menos 100 simulaciones.")
        if self.min_months_projection < 2:
            raise ValidationError("La proyección necesita al menos dos meses observados.")
