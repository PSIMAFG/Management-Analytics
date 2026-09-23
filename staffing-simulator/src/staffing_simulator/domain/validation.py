"""Validación de escenarios y posiciones, y advertencias de jornada por persona."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from staffing_simulator.domain.models import (
    ContractType,
    CostMethod,
    PartialMonthMethod,
    Person,
    Position,
    PositionDraft,
    ScenarioDraft,
)
from staffing_simulator.domain.parameters import (
    DEFAULT_FULL_TIME_WEEKLY_MINUTES,
    DEFAULT_WEEKS_PER_MONTH,
    CostParameters,
)
from staffing_simulator.domain.units import MINUTES_PER_HOUR, ZERO, hours_to_minutes, minutes_to_hours, to_decimal
from staffing_simulator.errors import ValidationError

MIN_YEAR = 2000
MAX_YEAR = 2100
MAX_ABSENCE = Decimal("0.5")
MAX_WEEKLY_HOURS = Decimal(48)
MAX_MONTHLY_HOURS = Decimal(220)
MAX_QUANTITY = 100
MAX_NAME_LENGTH = 80
MAX_NOTE_LENGTH = 500
MIN_GRADE = 1
MAX_GRADE = 30


def _fmt_date(value: date) -> str:
    return value.strftime("%d-%m-%Y")


def _fmt_hours(value: Decimal) -> str:
    """Horas con un decimal como máximo y coma decimal: 53.5 -> '53,5'; 44 -> '44'."""
    rounded = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP).normalize()
    return f"{rounded:f}".replace(".", ",")


def validate_year(year: int) -> int:
    if isinstance(year, bool) or not isinstance(year, int) or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValidationError(f"El año debe ser un número entre {MIN_YEAR} y {MAX_YEAR}.")
    return year


def validate_name(name: str, what: str = "El nombre") -> str:
    clean = " ".join((name or "").split())
    if not clean:
        raise ValidationError(f"{what} no puede quedar vacío.")
    if len(clean) > MAX_NAME_LENGTH:
        raise ValidationError(f"{what} admite como máximo {MAX_NAME_LENGTH} caracteres.")
    return clean


def validate_absence(value: Decimal | float | str) -> Decimal:
    absence = to_decimal(value)
    if not ZERO <= absence <= MAX_ABSENCE:
        raise ValidationError("El ausentismo esperado debe estar entre 0 % y 50 %.")
    return absence


def validate_scenario_draft(draft: ScenarioDraft) -> ScenarioDraft:
    """Devuelve el borrador normalizado o lanza ValidationError con un mensaje claro."""
    try:
        method = PartialMonthMethod(draft.partial_month_method)
    except ValueError as error:
        raise ValidationError("El método de meses parciales debe ser 'proporcional' o 'mes completo'.") from error
    description = (draft.description or "").strip()
    if len(description) > MAX_NOTE_LENGTH:
        raise ValidationError(f"La descripción admite como máximo {MAX_NOTE_LENGTH} caracteres.")
    return ScenarioDraft(
        name=validate_name(draft.name, "El nombre del escenario"),
        year=validate_year(draft.year),
        description=description,
        partial_month_method=method,
        expected_absence=validate_absence(draft.expected_absence),
        base_scenario_id=draft.base_scenario_id,
    )


@dataclass(frozen=True)
class ValidPosition:
    """Posición validada en unidades de almacenamiento (horas en minutos)."""

    job_role_id: int
    contract_type_id: int
    site_id: int
    program_id: int
    budget_item_id: int
    person_id: int | None
    weekly_minutes: int | None
    monthly_minutes: int | None
    quantity: int
    start_date: date
    end_date: date | None
    grade: int | None
    note: str


@dataclass(frozen=True)
class PositionIdentity:
    """Datos que hacen idénticas dos posiciones de un escenario (BUG-11).

    Una persona puede tener varias posiciones concurrentes, pero dos registros
    con el mismo cargo, contrato, sede, programa, persona, horas y fechas son
    un duplicado. La cantidad, el ítem, el grado y la nota no cuentan: varias
    vacantes iguales se registran con la cantidad. `holder` es el identificador
    de la persona, una persona por registrar (importación) o None para una vacante.
    """

    job_role_id: int
    contract_type_id: int
    site_id: int
    program_id: int
    holder: Hashable | None
    weekly_minutes: int | None
    monthly_minutes: int | None
    start_date: date
    end_date: date | None

    @classmethod
    def of_position(cls, position: Position) -> PositionIdentity:
        return cls(
            job_role_id=position.job_role.id,
            contract_type_id=position.contract_type.id,
            site_id=position.site.id,
            program_id=position.program.id,
            holder=None if position.person is None else position.person.id,
            weekly_minutes=position.weekly_minutes,
            monthly_minutes=position.monthly_minutes,
            start_date=position.start_date,
            end_date=position.end_date,
        )

    @classmethod
    def of_valid(cls, valid: ValidPosition, holder: Hashable | None = None) -> PositionIdentity:
        """Identidad de una posición validada; `holder` reemplaza a la persona si aún no existe en la base."""
        return cls(
            job_role_id=valid.job_role_id,
            contract_type_id=valid.contract_type_id,
            site_id=valid.site_id,
            program_id=valid.program_id,
            holder=valid.person_id if holder is None else holder,
            weekly_minutes=valid.weekly_minutes,
            monthly_minutes=valid.monthly_minutes,
            start_date=valid.start_date,
            end_date=valid.end_date,
        )


def identical_position_message(existing: Position) -> str:
    """Motivo de rechazo de una posición idéntica a otra que ya está en el escenario."""
    end = _fmt_date(existing.end_date) if existing.end_date else "fin de año"
    text = (
        f"Es idéntica a una posición que ya está en el escenario ({existing.job_role.name}, "
        f"{existing.holder_label}, {existing.program.name}, del {_fmt_date(existing.start_date)} a {end})."
    )
    if existing.is_vacancy:
        text += " Para más vacantes iguales, aumente la cantidad de esa posición."
    return text


def find_identical(
    valid: ValidPosition, positions: Iterable[Position], exclude_id: int | None = None
) -> Position | None:
    """Posición del escenario idéntica a la validada (sin contar `exclude_id`), o None."""
    identity = PositionIdentity.of_valid(valid)
    return next(
        (item for item in positions if item.id != exclude_id and PositionIdentity.of_position(item) == identity),
        None,
    )


def _positive_hours(value: Decimal | float | str | None, label: str, maximum: Decimal) -> int:
    if value is None:
        raise ValidationError(f"Indique {label}.")
    hours = to_decimal(value)
    if not ZERO < hours <= maximum:
        raise ValidationError(f"{label[0].upper()}{label[1:]} deben ser mayores que 0 y no superar {maximum}.")
    return hours_to_minutes(hours)


def validate_grade(grade: int | None, contract_type: ContractType) -> int | None:
    """El grado es solo informativo y únicamente aplica a contratos dependientes."""
    if grade is None:
        return None
    if contract_type.cost_method is not CostMethod.SALARIED:
        raise ValidationError("El grado solo aplica a contratos dependientes (plazo fijo o planta).")
    if isinstance(grade, bool) or not isinstance(grade, int) or not MIN_GRADE <= grade <= MAX_GRADE:
        raise ValidationError(f"El grado debe ser un número entero entre {MIN_GRADE} y {MAX_GRADE}.")
    return grade


def validate_position_draft(draft: PositionDraft, contract_type: ContractType, scenario_year: int) -> ValidPosition:
    """Valida una posición según su tipo de contrato y el año del escenario.

    No comprueba que el ítem presupuestario exista ni pertenezca al programa:
    esa comprobación necesita la base de datos y la hace el servicio, igual
    que con la sede, el programa o la persona.
    """
    if not isinstance(draft.start_date, date):
        raise ValidationError("Indique una fecha de inicio válida.")
    if draft.end_date is not None:
        if not isinstance(draft.end_date, date):
            raise ValidationError("La fecha de término no es válida.")
        if draft.end_date < draft.start_date:
            raise ValidationError(
                f"La fecha de término ({_fmt_date(draft.end_date)}) es anterior a la de inicio "
                f"({_fmt_date(draft.start_date)})."
            )
    year_start, year_end = date(scenario_year, 1, 1), date(scenario_year, 12, 31)
    if draft.start_date > year_end or (draft.end_date is not None and draft.end_date < year_start):
        raise ValidationError(f"La posición no tiene vigencia en {scenario_year}, el año del escenario.")
    if draft.budget_item_id is None:
        raise ValidationError("Indique el ítem presupuestario de recurso humano al que se imputa la posición.")

    method = contract_type.cost_method
    weekly_minutes: int | None = None
    monthly_minutes: int | None = None
    if method.uses_weekly_hours:
        if draft.monthly_hours is not None:
            raise ValidationError(
                f"El tipo de contrato {contract_type.name} se costea por jornada semanal: "
                "deje vacías las horas mensuales."
            )
        weekly_minutes = _positive_hours(draft.weekly_hours, "las horas semanales", MAX_WEEKLY_HOURS)
    else:
        if draft.weekly_hours is not None:
            raise ValidationError(
                f"El tipo de contrato {contract_type.name} se costea por horas mensuales: "
                "deje vacías las horas semanales."
            )
        monthly_minutes = _positive_hours(draft.monthly_hours, "las horas mensuales estimadas", MAX_MONTHLY_HOURS)

    quantity = draft.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
        raise ValidationError(f"La cantidad debe ser un número entero entre 1 y {MAX_QUANTITY}.")
    if draft.person_id is not None and quantity != 1:
        raise ValidationError("Una posición asignada a una persona debe tener cantidad 1.")
    grade = validate_grade(draft.grade, contract_type)
    note = (draft.note or "").strip()
    if len(note) > MAX_NOTE_LENGTH:
        raise ValidationError(f"La nota admite como máximo {MAX_NOTE_LENGTH} caracteres.")
    return ValidPosition(
        job_role_id=draft.job_role_id,
        contract_type_id=draft.contract_type_id,
        site_id=draft.site_id,
        program_id=draft.program_id,
        budget_item_id=draft.budget_item_id,
        person_id=draft.person_id,
        weekly_minutes=weekly_minutes,
        monthly_minutes=monthly_minutes,
        quantity=quantity,
        start_date=draft.start_date,
        end_date=draft.end_date,
        grade=grade,
        note=note,
    )


@dataclass(frozen=True)
class WorkloadWarning:
    """Una persona supera la jornada completa en algún tramo del año.

    `weekly_minutes` es la suma de las horas semanales (equivalentes) de todas
    las posiciones vigentes de la persona en el tramo, sin importar el tipo de
    contrato ni en cuántos registros esté repartida la jornada; `limit_minutes`
    es la jornada completa configurada (Parámetros, Convenciones). Los
    contratos por horas aportan su equivalente semanal (horas mensuales /
    semanas por mes).
    """

    person: Person
    start: date
    end: date
    weekly_minutes: Decimal
    limit_minutes: int
    concurrent: int = 1
    includes_hourly: bool = False

    @property
    def weekly_hours(self) -> Decimal:
        return self.weekly_minutes / MINUTES_PER_HOUR

    @property
    def limit_hours(self) -> Decimal:
        return minutes_to_hours(self.limit_minutes)

    @property
    def message(self) -> str:
        detail = f" en {self.concurrent} posiciones simultáneas" if self.concurrent > 1 else ""
        hourly = " (incluye el equivalente semanal de contratos por horas)" if self.includes_hourly else ""
        limit = _fmt_hours(self.limit_hours)
        return (
            f"{self.person.full_name} suma {_fmt_hours(self.weekly_hours)} h semanales{detail} entre el "
            f"{_fmt_date(self.start)} y el {_fmt_date(self.end)}{hourly}; la jornada completa es {limit} h."
        )


def _segments(positions: Iterable[Position], year: int) -> list[tuple[date, date]]:
    """Tramos del año en que no cambian las posiciones vigentes de la persona."""
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    cuts = {year_start}
    for position in positions:
        period = position.period_in_year(year)
        if period is None:
            continue
        cuts.add(period[0])
        if period[1] < year_end:
            cuts.add(period[1] + timedelta(days=1))
    ordered = sorted(cuts)
    return [
        (start, (ordered[index + 1] - timedelta(days=1)) if index + 1 < len(ordered) else year_end)
        for index, start in enumerate(ordered)
    ]


def _total_minutes(positions: Sequence[Position], weeks_per_month: Decimal) -> Decimal:
    return sum((item.weekly_equivalent_minutes(weeks_per_month) for item in positions), ZERO)


def _segment_warning(
    person: Person, start: date, end: date, active: Sequence[Position], limit_minutes: int, weeks_per_month: Decimal
) -> WorkloadWarning | None:
    if not active:
        return None
    total = _total_minutes(active, weeks_per_month)
    if total <= limit_minutes:
        return None
    return WorkloadWarning(
        person=person,
        start=start,
        end=end,
        weekly_minutes=total,
        limit_minutes=limit_minutes,
        concurrent=len(active),
        includes_hourly=any(item.weekly_minutes is None for item in active),
    )


def _merge(items: Iterable[WorkloadWarning | None]) -> list[WorkloadWarning]:
    """Une los tramos consecutivos que repiten la misma situación."""
    merged: list[WorkloadWarning] = []
    for item in items:
        if item is None:
            continue
        if merged and _continues(merged[-1], item):
            merged[-1] = replace(merged[-1], end=item.end)
        else:
            merged.append(item)
    return merged


def _continues(previous: WorkloadWarning, current: WorkloadWarning) -> bool:
    """El tramo siguiente repite la misma situación y se une al anterior."""
    return (
        previous.weekly_minutes == current.weekly_minutes
        and previous.limit_minutes == current.limit_minutes
        and previous.concurrent == current.concurrent
        and previous.includes_hourly == current.includes_hourly
        and previous.end + timedelta(days=1) == current.start
    )


def workload_warnings(
    positions: Iterable[Position],
    year: int,
    weeks_per_month: Decimal = DEFAULT_WEEKS_PER_MONTH,
    full_time_minutes: int = DEFAULT_FULL_TIME_WEEKLY_MINUTES,
) -> tuple[WorkloadWarning, ...]:
    """Advierte (sin bloquear) cuando la suma semanal de una persona supera la jornada completa.

    Da lo mismo en cuántos registros o tipos de contrato esté repartida la
    jornada: 22 h + 22 h se tratan igual que una sola posición de 44 h.
    """
    by_person: dict[int, list[Position]] = defaultdict(list)
    people: dict[int, Person] = {}
    for position in positions:
        if position.person is None or position.period_in_year(year) is None:
            continue
        by_person[position.person.id].append(position)
        people[position.person.id] = position.person

    warnings: list[WorkloadWarning] = []
    for person_id, person_positions in by_person.items():
        person = people[person_id]
        segments = [
            _segment_warning(
                person,
                start,
                end,
                [item for item in person_positions if item.is_active_on(start)],
                full_time_minutes,
                weeks_per_month,
            )
            for start, end in _segments(person_positions, year)
        ]
        warnings += _merge(segments)
    return tuple(sorted(warnings, key=lambda item: (item.person.full_name, item.start)))


def scenario_workload_warnings(
    positions: Iterable[Position], params: CostParameters, year: int
) -> tuple[WorkloadWarning, ...]:
    """Advertencias de jornada con la jornada completa y las convenciones de los parámetros."""
    settings = params.settings
    return workload_warnings(positions, year, settings.weeks_per_month, settings.full_time_weekly_minutes)
