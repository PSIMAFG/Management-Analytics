"""Casos de uso de parámetros: tarifas de honorarios, escala de sueldo, reajustes, retenciones y aportes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from staffing_simulator.data.db import transaction
from staffing_simulator.data.repositories import CatalogRepository, ParameterRepository
from staffing_simulator.domain.models import CATEGORY_LABELS, Catalog, ContractType, CostMethod, JobRole, RetentionBasis
from staffing_simulator.domain.parameters import (
    CategoryRate,
    CostParameters,
    CostSettings,
    EmployerContribution,
    RetentionRate,
    RoleRate,
    SalaryAdjustment,
    SalaryScale,
    validate_category,
)
from staffing_simulator.domain.units import ZERO, hours_to_minutes, to_decimal
from staffing_simulator.domain.validation import validate_name, validate_year
from staffing_simulator.errors import NotFoundError, ValidationError
from staffing_simulator.services.common import ServiceBase

MAX_HOURLY_RATE = 1_000_000
MAX_RATE_FRACTION = Decimal("0.5")
MAX_SALARY_AMOUNT = 20_000_000
MAX_ADJUSTMENT_PERCENT = Decimal("0.5")
MAX_FULL_TIME_HOURS = Decimal(60)


@dataclass(frozen=True)
class RateRow:
    """Fila de la tabla de tarifas de honorarios: por categoría o específica de un cargo."""

    year: int
    category: str
    job_role_id: int | None
    label: str
    hourly_rate: int

    @property
    def is_role_specific(self) -> bool:
        return self.job_role_id is not None


def _validate_rate_amount(hourly_rate: int) -> int:
    if isinstance(hourly_rate, bool) or not isinstance(hourly_rate, int) or not 0 < hourly_rate <= MAX_HOURLY_RATE:
        raise ValidationError("El valor hora debe ser un número entero de pesos entre 1 y 1.000.000.")
    return hourly_rate


def _validate_fraction(rate: Decimal | float | str, label: str, maximum: Decimal = MAX_RATE_FRACTION) -> Decimal:
    value = to_decimal(rate)
    if not value >= ZERO or value > maximum:
        raise ValidationError(f"{label} debe estar entre 0 % y {maximum * 100:.0f} %.")
    return value


class ParameterService(ServiceBase):
    """Lectura y edición validada de los parámetros de costeo."""

    def catalog(self) -> Catalog:
        """Sedes, programas, cargos, tipos de contrato y personas para formularios."""
        with self._connection() as conn:
            return CatalogRepository(conn).catalog()

    def cost_parameters(self) -> CostParameters:
        with self._connection() as conn:
            return ParameterRepository(conn).cost_parameters()

    def category_rates(self) -> list[CategoryRate]:
        with self._connection() as conn:
            return ParameterRepository(conn).category_rates()

    def role_rates(self) -> list[RoleRate]:
        with self._connection() as conn:
            return ParameterRepository(conn).role_rates()

    def rate_rows(self, year: int | None = None) -> list[RateRow]:
        """Tarifas de honorarios por categoría y específicas por cargo, listas para mostrarse en una tabla."""
        with self._connection() as conn:
            params = ParameterRepository(conn)
            roles = {role.id: role for role in CatalogRepository(conn).job_roles()}
            rows = [
                RateRow(
                    item.year,
                    item.category,
                    None,
                    f"Categoría {item.category}: {CATEGORY_LABELS[item.category]}",
                    item.hourly_rate,
                )
                for item in params.category_rates()
            ]
            rows += [
                RateRow(
                    item.year,
                    roles[item.job_role_id].category,
                    item.job_role_id,
                    f"Cargo: {roles[item.job_role_id].name}",
                    item.hourly_rate,
                )
                for item in params.role_rates()
            ]
        if year is not None:
            rows = [row for row in rows if row.year == year]
        return sorted(rows, key=lambda row: (row.year, row.category, row.job_role_id or 0))

    def set_category_rate(self, category: str, year: int, hourly_rate: int) -> None:
        code = validate_category(category)
        validate_year(year)
        _validate_rate_amount(hourly_rate)
        with self._connection() as conn, transaction(conn):
            ParameterRepository(conn).set_category_rate(code, year, hourly_rate)

    def delete_category_rate(self, category: str, year: int) -> None:
        with self._connection() as conn, transaction(conn):
            if ParameterRepository(conn).delete_category_rate(validate_category(category), year) == 0:
                raise NotFoundError("La tarifa indicada no existe.")

    def set_role_rate(self, job_role_id: int, year: int, hourly_rate: int) -> None:
        """Tarifa de honorarios específica de un cargo (prevalece sobre la de su categoría)."""
        validate_year(year)
        _validate_rate_amount(hourly_rate)
        with self._connection() as conn, transaction(conn):
            CatalogRepository(conn).job_role(job_role_id)
            ParameterRepository(conn).set_role_rate(job_role_id, year, hourly_rate)

    def delete_role_rate(self, job_role_id: int, year: int) -> None:
        with self._connection() as conn, transaction(conn):
            if ParameterRepository(conn).delete_role_rate(job_role_id, year) == 0:
                raise NotFoundError("La tarifa específica indicada no existe.")

    def update_job_role_category(self, job_role_id: int, category: str) -> JobRole:
        code = validate_category(category)
        with self._connection() as conn, transaction(conn):
            catalog = CatalogRepository(conn)
            catalog.job_role(job_role_id)
            catalog.update_job_role_category(job_role_id, code)
            return catalog.job_role(job_role_id)

    def contract_types(self) -> list[ContractType]:
        with self._connection() as conn:
            return CatalogRepository(conn).contract_types()

    def update_contract_type(self, contract_type_id: int, name: str, applies_retention: bool) -> ContractType:
        """Renombra un tipo de contrato y define si aplica retención (nunca en contratos dependientes)."""
        clean = validate_name(name, "El nombre del tipo de contrato")
        with self._connection() as conn, transaction(conn):
            catalog = CatalogRepository(conn)
            current = catalog.contract_type(contract_type_id)
            if applies_retention and current.cost_method is CostMethod.SALARIED:
                raise ValidationError("Los contratos dependientes no tienen retención de honorarios.")
            other = next((item for item in catalog.contract_types() if item.name.casefold() == clean.casefold()), None)
            if other is not None and other.id != contract_type_id:
                raise ValidationError(f"Ya existe un tipo de contrato llamado «{clean}».")
            catalog.update_contract_type(contract_type_id, clean, applies_retention)
            return catalog.contract_type(contract_type_id)

    def contributions(self) -> list[EmployerContribution]:
        with self._connection() as conn:
            return ParameterRepository(conn).contributions()

    def set_contribution(self, contract_type_id: int, year: int, rate: Decimal) -> None:
        """Aporte del empleador (fracción, 0,05 = 5 %) de un tipo de contrato dependiente en un año."""
        validate_year(year)
        value = _validate_fraction(rate, "El aporte del empleador")
        with self._connection() as conn, transaction(conn):
            contract_type = CatalogRepository(conn).contract_type(contract_type_id)
            if contract_type.cost_method is not CostMethod.SALARIED:
                raise ValidationError(
                    "El aporte del empleador solo aplica a contratos dependientes: "
                    "el costo de un honorario es su monto bruto."
                )
            ParameterRepository(conn).set_contribution(contract_type_id, year, value)

    def delete_contribution(self, contract_type_id: int, year: int) -> None:
        with self._connection() as conn, transaction(conn):
            if ParameterRepository(conn).delete_contribution(contract_type_id, year) == 0:
                raise NotFoundError("El aporte indicado no existe.")

    def retention_rates(self) -> list[RetentionRate]:
        with self._connection() as conn:
            return ParameterRepository(conn).retention_rates()

    def set_retention_rate(self, year: int, rate: Decimal) -> None:
        """Tasa de retención de honorarios vigente desde el año indicado (fracción)."""
        validate_year(year)
        value = _validate_fraction(rate, "La tasa de retención")
        with self._connection() as conn, transaction(conn):
            ParameterRepository(conn).set_retention_rate(year, value)

    def delete_retention_rate(self, year: int) -> None:
        with self._connection() as conn, transaction(conn):
            if ParameterRepository(conn).delete_retention_rate(year) == 0:
                raise NotFoundError("La tasa indicada no existe.")

    def salary_scales(self) -> list[SalaryScale]:
        with self._connection() as conn:
            return ParameterRepository(conn).salary_scales()

    def set_salary_scale(self, category: str, valid_from: date, monthly_amount: int) -> None:
        """Sueldo mensual del grado 15 (jornada completa) de una categoría, vigente desde una fecha."""
        code = validate_category(category)
        if not isinstance(valid_from, date):
            raise ValidationError("Indique una fecha de vigencia válida.")
        validate_year(valid_from.year)
        if (
            isinstance(monthly_amount, bool)
            or not isinstance(monthly_amount, int)
            or not 0 < monthly_amount <= MAX_SALARY_AMOUNT
        ):
            raise ValidationError("El sueldo del grado 15 debe ser un monto entero de pesos mayor que 0.")
        with self._connection() as conn, transaction(conn):
            ParameterRepository(conn).set_salary_scale(code, valid_from, monthly_amount)

    def delete_salary_scale(self, category: str, valid_from: date) -> None:
        with self._connection() as conn, transaction(conn):
            if ParameterRepository(conn).delete_salary_scale(validate_category(category), valid_from) == 0:
                raise NotFoundError("La escala de sueldo indicada no existe.")

    def salary_adjustments(self) -> list[SalaryAdjustment]:
        with self._connection() as conn:
            return ParameterRepository(conn).salary_adjustments()

    def set_salary_adjustment(self, valid_from: date, percent: Decimal, description: str = "") -> None:
        """Reajuste del sector público (fracción, puede ser negativo) para plazo fijo y planta."""
        if not isinstance(valid_from, date):
            raise ValidationError("Indique una fecha de vigencia válida.")
        validate_year(valid_from.year)
        value = to_decimal(percent)
        if abs(value) > MAX_ADJUSTMENT_PERCENT:
            raise ValidationError("El reajuste debe estar entre -50 % y 50 %.")
        clean_description = (description or "").strip()[:200]
        with self._connection() as conn, transaction(conn):
            ParameterRepository(conn).set_salary_adjustment(valid_from, value, clean_description)

    def delete_salary_adjustment(self, valid_from: date) -> None:
        with self._connection() as conn, transaction(conn):
            if ParameterRepository(conn).delete_salary_adjustment(valid_from) == 0:
                raise NotFoundError("El reajuste indicado no existe.")

    def settings(self) -> CostSettings:
        with self._connection() as conn:
            return ParameterRepository(conn).settings()

    def update_settings(
        self,
        weeks_per_month: Decimal | float | str,
        retention_basis: RetentionBasis | str,
        full_time_weekly_hours: Decimal | float | str,
    ) -> CostSettings:
        """Guarda las convenciones: semanas por mes, año de la tasa de retención y jornada completa."""
        try:
            basis = RetentionBasis(retention_basis)
        except ValueError as error:
            raise ValidationError("La base del año de retención no es válida.") from error
        hours = to_decimal(full_time_weekly_hours)
        if not ZERO < hours <= MAX_FULL_TIME_HOURS:
            raise ValidationError(f"La jornada completa debe estar entre 0 y {MAX_FULL_TIME_HOURS} horas.")
        settings = CostSettings(
            weeks_per_month=to_decimal(weeks_per_month),
            retention_basis=basis,
            full_time_weekly_minutes=hours_to_minutes(hours),
        )
        with self._connection() as conn, transaction(conn):
            ParameterRepository(conn).save_settings(settings)
        return settings
