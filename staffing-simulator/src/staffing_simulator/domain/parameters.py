"""Parámetros de costeo versionados por año: tarifas, escalas, reajustes, retenciones y aportes.

Todas las búsquedas fallan con un mensaje claro cuando falta el dato; nunca
se devuelve un costo 0 silencioso.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from staffing_simulator.domain.models import CATEGORIES, ContractType, CostMethod, JobRole, RetentionBasis
from staffing_simulator.domain.units import MINUTES_PER_HOUR, ZERO, minutes_to_hours
from staffing_simulator.errors import MissingParameterError, ValidationError

DEFAULT_WEEKS_PER_MONTH = Decimal(4)
MAX_WEEKS_PER_MONTH = Decimal(5)
DEFAULT_FULL_TIME_WEEKLY_MINUTES = 44 * MINUTES_PER_HOUR
MAX_FULL_TIME_WEEKLY_HOURS = 96


@dataclass(frozen=True)
class CategoryRate:
    """Valor hora de honorarios de una categoría en un año."""

    category: str
    year: int
    hourly_rate: int


@dataclass(frozen=True)
class RoleRate:
    """Valor hora de honorarios específico de un cargo en un año (prevalece sobre la categoría)."""

    job_role_id: int
    year: int
    hourly_rate: int


@dataclass(frozen=True)
class RetentionRate:
    """Tasa de retención de honorarios vigente desde un año."""

    year: int
    rate: Decimal


@dataclass(frozen=True)
class EmployerContribution:
    """Aporte del empleador (fracción sobre la remuneración bruta) por tipo de contrato y año."""

    contract_type_id: int
    year: int
    rate: Decimal


@dataclass(frozen=True)
class SalaryScale:
    """Sueldo base del grado 15 de una categoría: monto mensual para la jornada completa, vigente desde una fecha."""

    category: str
    valid_from: date
    monthly_amount: int


@dataclass(frozen=True)
class SalaryAdjustment:
    """Reajuste del sector público (plazo fijo y planta), multiplicativo desde su fecha de vigencia."""

    valid_from: date
    percent: Decimal
    description: str = ""


@dataclass(frozen=True)
class RateTable:
    """Tarifas de honorarios por categoría y año, con tarifas específicas por cargo."""

    category_rates: Mapping[tuple[str, int], int] = field(default_factory=dict)
    role_rates: Mapping[tuple[int, int], int] = field(default_factory=dict)

    @classmethod
    def from_records(cls, categories: Iterable[CategoryRate], roles: Iterable[RoleRate] = ()) -> RateTable:
        return cls(
            category_rates={(item.category, item.year): item.hourly_rate for item in categories},
            role_rates={(item.job_role_id, item.year): item.hourly_rate for item in roles},
        )

    def role_rate_years(self, role: JobRole) -> tuple[int, ...]:
        """Años en que el cargo tiene una tarifa específica."""
        return tuple(sorted(year for role_id, year in self.role_rates if role_id == role.id))

    def hourly_rate(self, role: JobRole, year: int) -> Decimal:
        """Valor hora del cargo: el específico del cargo o, si el cargo nunca lo tuvo, el de su categoría.

        Un cargo con tarifa específica en otros años pero no en `year` no cae en
        silencio a la tarifa de su categoría (por ejemplo, un cargo médico con
        tarifa propia se subestimaría al pasar a un año nuevo): se pide cargarla.
        """
        specific = self.role_rates.get((role.id, year))
        if specific is not None:
            return Decimal(specific)
        own_years = self.role_rate_years(role)
        if own_years:
            listed = ", ".join(str(item) for item in own_years)
            raise MissingParameterError(
                f"El cargo {role.name} tiene tarifa propia en {listed} pero no en {year}: agréguela en Parámetros "
                f"(puede ser igual a la de la categoría {role.category} si ya no corresponde una tarifa distinta)."
            )
        general = self.category_rates.get((role.category, year))
        if general is None:
            raise MissingParameterError(
                f"No hay valor hora de honorarios definido para la categoría {role.category} en {year} "
                f"(cargo {role.name}). Agréguelo en Parámetros antes de costear."
            )
        return Decimal(general)


@dataclass(frozen=True)
class RetentionTable:
    """Tasas de retención de honorarios; cada tasa rige desde su año hasta la siguiente."""

    rates: tuple[RetentionRate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rates", tuple(sorted(self.rates, key=lambda item: item.year)))

    def rate_for(self, year: int) -> Decimal:
        applicable = [item.rate for item in self.rates if item.year <= year]
        if not applicable:
            raise MissingParameterError(
                f"No hay tasa de retención de honorarios definida para {year} ni para años anteriores."
            )
        return applicable[-1]


@dataclass(frozen=True)
class ContributionTable:
    """Aportes del empleador por tipo de contrato dependiente y año."""

    rates: Mapping[tuple[int, int], Decimal] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: Iterable[EmployerContribution]) -> ContributionTable:
        return cls({(item.contract_type_id, item.year): item.rate for item in records})

    def rate_for(self, contract_type: ContractType, year: int) -> Decimal:
        """Fracción de aporte; los honorarios no tienen aporte (su costo es el bruto)."""
        if contract_type.cost_method is not CostMethod.SALARIED:
            return ZERO
        rate = self.rates.get((contract_type.id, year))
        if rate is None:
            raise MissingParameterError(
                f"No hay aporte del empleador definido para el tipo de contrato {contract_type.name} en {year}. "
                "Agréguelo en Parámetros antes de costear."
            )
        return rate


@dataclass(frozen=True)
class SalaryTable:
    """Escala de sueldo del grado 15 por categoría y los reajustes del sector público."""

    scales: tuple[SalaryScale, ...] = ()
    adjustments: tuple[SalaryAdjustment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scales", tuple(sorted(self.scales, key=lambda item: item.valid_from)))
        object.__setattr__(self, "adjustments", tuple(sorted(self.adjustments, key=lambda item: item.valid_from)))

    def _scale_for(self, category: str, day: date) -> SalaryScale | None:
        applicable = [item for item in self.scales if item.category == category and item.valid_from <= day]
        return applicable[-1] if applicable else None

    def amount_for(self, category: str, day: date) -> Decimal:
        """Sueldo del grado 15 vigente en `day`, con los reajustes aplicados (jornada completa)."""
        scale = self._scale_for(category, day)
        if scale is None:
            raise MissingParameterError(
                f"No hay escala de sueldo grado 15 definida para la categoría {category} vigente el "
                f"{day.strftime('%d-%m-%Y')}. Agréguela en Parámetros antes de costear."
            )
        factor = Decimal(1)
        for adjustment in self.adjustments:
            if scale.valid_from < adjustment.valid_from <= day:
                factor *= Decimal(1) + adjustment.percent
        return Decimal(scale.monthly_amount) * factor


@dataclass(frozen=True)
class CostSettings:
    """Convenciones generales del costeo y de las advertencias.

    `full_time_weekly_minutes` es la jornada completa de referencia (44 h en
    atención primaria): se usa para prorratear el sueldo del grado 15 según
    las horas semanales de la posición y como umbral de las advertencias de
    carga por persona.
    """

    weeks_per_month: Decimal = DEFAULT_WEEKS_PER_MONTH
    retention_basis: RetentionBasis = RetentionBasis.SERVICE
    full_time_weekly_minutes: int = DEFAULT_FULL_TIME_WEEKLY_MINUTES

    def __post_init__(self) -> None:
        if not ZERO < self.weeks_per_month <= MAX_WEEKS_PER_MONTH:
            raise ValidationError("Las semanas por mes deben estar entre 0 y 5 (la convención de los contratos es 4).")
        if not MINUTES_PER_HOUR <= self.full_time_weekly_minutes <= MAX_FULL_TIME_WEEKLY_HOURS * MINUTES_PER_HOUR:
            raise ValidationError(f"La jornada completa debe estar entre 1 y {MAX_FULL_TIME_WEEKLY_HOURS} horas.")

    @property
    def full_time_weekly_hours(self) -> Decimal:
        return minutes_to_hours(self.full_time_weekly_minutes)

    def retention_year(self, service_year: int, month: int) -> int:
        """Año de la tasa de retención para un mes de servicio.

        Con base en el pago se supone que el mes se paga el mes siguiente, por
        lo que diciembre usa la tasa del año siguiente.
        """
        if self.retention_basis is RetentionBasis.PAYMENT and month == 12:
            return service_year + 1
        return service_year


@dataclass(frozen=True)
class CostParameters:
    """Todo lo que el motor de costeo necesita además de las posiciones."""

    rates: RateTable
    retention: RetentionTable
    contributions: ContributionTable
    salary: SalaryTable = field(default_factory=SalaryTable)
    settings: CostSettings = field(default_factory=CostSettings)


def validate_category(category: str) -> str:
    code = category.strip().upper()
    if code not in CATEGORIES:
        raise ValidationError(f"La categoría '{category}' no existe. Use una letra de la A a la F.")
    return code


def ensure_position_costable(params: CostParameters, role: JobRole, contract_type: ContractType, year: int) -> None:
    """Comprueba que exista el parámetro de costeo del tipo de contrato (lanza MissingParameterError si falta)."""
    if contract_type.cost_method is CostMethod.SALARIED:
        params.salary.amount_for(role.category, date(year, 1, 1))
    else:
        params.rates.hourly_rate(role, year)
    params.contributions.rate_for(contract_type, year)
