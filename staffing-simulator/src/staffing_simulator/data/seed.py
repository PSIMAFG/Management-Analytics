"""Generador reproducible de datos de ejemplo (semilla fija).

Los datos son sintéticos pero conservan la estructura de un caso real de
costeo de dotación en atención primaria: unas sesenta posiciones en cuatro
programas y cinco sedes, la misma mezcla de cargos y jornadas semanales (44,
41, 37, 35, 33, 32, 30, 28, 22, 18, 15, 11, 7,5 y 6 h), honorarios con la
proporción habitual entre categorías (un cargo médico con tarifa específica
cercana a 2,3 veces la profesional), jornada completa de 44 h y estructura
financiera por ítems (recurso humano y operación) del orden del costo
proyectado.

Incluye los casos estructurales que el motor debe resolver: una persona con
dos posiciones simultáneas en programas distintos, cambios de jornada a
mitad de año modelados como dos tramos, contratos por horas, contratos de
pocas semanas, un cargo con tarifa distinta, vacantes (una con cantidad 2),
plazo fijo y planta con el sueldo del grado 15 y reajustes del sector
público, y un cargo con grado real distinto de 15. Los nombres combinan
listas de nombres y apellidos comunes y los RUT son ficticios (rango
40.000.000 a 45.999.999, con dígito verificador válido).
"""

from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from staffing_simulator.data.db import transaction
from staffing_simulator.data.repositories import (
    CatalogRepository,
    ExecutionRepository,
    FinancialRepository,
    ParameterRepository,
    ScenarioRepository,
)
from staffing_simulator.domain.models import (
    BudgetItemDraft,
    BudgetItemType,
    ContractType,
    CostMethod,
    JobRole,
    OtherExpenseDraft,
    OtherExpenseType,
    PartialMonthMethod,
    Person,
    Program,
    ProgramBudget,
    RetentionBasis,
    ScenarioDraft,
    Site,
)
from staffing_simulator.domain.parameters import CostSettings
from staffing_simulator.domain.projection import Dimension, project_scenario
from staffing_simulator.domain.rut import check_digit
from staffing_simulator.domain.units import hours_to_minutes, round_pesos
from staffing_simulator.domain.validation import ValidPosition

log = logging.getLogger(__name__)

SEED = 2026
DEMO_YEAR = 2026
EXECUTED_MONTHS = tuple(range(1, 9))
CREATED_AT = datetime(2026, 1, 5, 9, 0, 0)
RUT_RANGE = (40_000_000, 45_999_999)

SITES = (
    ("SC", "Sede Centro"),
    ("SN", "Sede Norte"),
    ("SS", "Sede Sur"),
    ("SO", "Sede Oriente"),
    ("SP", "Sede Poniente"),
)
PROGRAMS = (
    ("P1", "Programa Base"),
    ("P2", "Programa Comunitario"),
    ("P3", "Programa Especializado"),
    ("P4", "Programa Territorial"),
)
JOB_ROLES = (
    ("MED", "Médico psiquiatra", "A"),
    ("MEG", "Médico general", "A"),
    ("PSI", "Psicólogo", "B"),
    ("TOC", "Terapeuta ocupacional", "B"),
    ("TSO", "Trabajador social", "B"),
    ("ENF", "Enfermero", "B"),
    ("FON", "Fonoaudiólogo", "B"),
    ("KIN", "Kinesiólogo", "B"),
    ("NUT", "Nutricionista", "B"),
    ("TNS", "Técnico de nivel superior", "C"),
    ("TEC", "Técnico de apoyo", "D"),
    ("ADM", "Administrativo", "E"),
    ("AUX", "Auxiliar de servicio", "F"),
)
# Código, nombre, forma de costeo y si aplica retención de honorarios.
CONTRACT_TYPES = (
    ("HON", "Honorarios", CostMethod.WEEKLY_FEE, True),
    ("HPH", "Honorarios por horas", CostMethod.HOURLY_FEE, True),
    ("PF", "Plazo fijo", CostMethod.SALARIED, False),
    ("PLA", "Planta", CostMethod.SALARIED, False),
)

# Valor hora de honorarios 2026 por categoría (pesos), digitado por el usuario cada año. La
# categoría B es la tarifa profesional; las demás mantienen una proporción típica respecto de ella.
CATEGORY_RATES_2026 = {"A": 16_600, "B": 8_300, "C": 5_900, "D": 5_100, "E": 4_500, "F": 3_900}
# Tarifa específica de un cargo médico, cercana a 2,3 veces la profesional.
ROLE_RATES_2026 = {"MED": 19_100}
# Reajuste de honorarios respecto de 2026 para los años vecinos (reajuste propio del municipio).
RATE_INDEX = {2025: Decimal("0.958"), 2026: Decimal(1), 2027: Decimal("1.038")}

# Tasas legales de retención de honorarios (dato público); la de 2028 rige en adelante.
RETENTION_RATES = {
    2020: "0.1075",
    2021: "0.115",
    2022: "0.1225",
    2023: "0.13",
    2024: "0.1375",
    2025: "0.145",
    2026: "0.1525",
    2027: "0.16",
    2028: "0.17",
}
# Sueldo mensual del grado 15 por categoría (jornada completa, 44 h), vigente desde 2025.
SALARY_SCALE_2025 = {"A": 2_150_000, "B": 1_180_000, "C": 890_000, "D": 780_000, "E": 720_000, "F": 650_000}
# Reajustes del sector público (aproximados, editables): diciembre 2025 y junio 2026.
SALARY_ADJUSTMENTS = (
    (date(2025, 12, 1), Decimal("0.03"), "Reajuste sector público diciembre 2025"),
    (date(2026, 6, 1), Decimal("0.014"), "Reajuste sector público junio 2026 (segunda parte)"),
)

FIRST_NAMES = (
    "Camila",
    "Valentina",
    "Renata",
    "Francisca",
    "Constanza",
    "Daniela",
    "Catalina",
    "Carolina",
    "Paula",
    "Andrea",
    "Macarena",
    "Antonia",
    "Florencia",
    "Ignacia",
    "Josefa",
    "Sebastián",
    "Diego",
    "Nicolás",
    "Felipe",
    "Tomás",
    "Joaquín",
    "Benjamín",
    "Cristóbal",
    "Ignacio",
    "Rodrigo",
    "Gonzalo",
    "Martín",
    "Vicente",
    "Álvaro",
    "Pablo",
)
SURNAMES = (
    "González",
    "Muñoz",
    "Díaz",
    "Pérez",
    "Soto",
    "Contreras",
    "Silva",
    "Martínez",
    "Sepúlveda",
    "Morales",
    "Rodríguez",
    "López",
    "Fuentes",
    "Hernández",
    "Torres",
    "Araya",
    "Flores",
    "Espinoza",
    "Valenzuela",
    "Castillo",
    "Tapia",
    "Reyes",
    "Gutiérrez",
    "Castro",
    "Pizarro",
    "Álvarez",
    "Vásquez",
    "Sánchez",
    "Carrasco",
    "Cortés",
)

NEW = "nueva"
VACANCY = "vacante"

# Ítems de operación por programa: código, nombre y proporción aproximada del ítem de recurso
# humano (para que los montos guarden una magnitud realista frente a la dotación).
OPERATION_ITEMS = (
    ("ARR", "Arriendo de sede", Decimal("0.09")),
    ("INS", "Insumos clínicos y de oficina", Decimal("0.04")),
    ("CAP", "Capacitación", Decimal("0.015")),
    ("MOV", "Movilización", Decimal("0.02")),
)


@dataclass(frozen=True)
class PositionSpec:
    """Posición de ejemplo: horas semanales (o mensuales si el contrato es por horas)."""

    role: str
    contract: str
    hours: str
    program: str
    site: str
    start: date = date(DEMO_YEAR, 1, 1)
    end: date | None = None
    holder: str = NEW
    quantity: int = 1
    grade: int | None = None
    note: str = ""


def _d(month: int, day: int) -> date:
    return date(DEMO_YEAR, month, day)


CURRENT_STAFF = (
    PositionSpec("PSI", "HON", "44", "P1", "SC"),
    PositionSpec("PSI", "HON", "44", "P1", "SC"),
    PositionSpec("PSI", "HON", "44", "P1", "SN"),
    PositionSpec("PSI", "HON", "22", "P1", "SC"),
    PositionSpec("PSI", "HON", "22", "P1", "SS"),
    PositionSpec("PSI", "HON", "15", "P1", "SC"),
    PositionSpec("TOC", "HON", "44", "P1", "SC"),
    PositionSpec("TOC", "HON", "44", "P1", "SC"),
    PositionSpec("TOC", "HON", "35", "P1", "SN"),
    PositionSpec("TOC", "HON", "22", "P1", "SC"),
    PositionSpec("TSO", "HON", "44", "P1", "SC"),
    PositionSpec("TSO", "HON", "33", "P1", "SC", holder="concurrente", note="Posición principal"),
    PositionSpec("ENF", "HON", "44", "P1", "SC"),
    PositionSpec("ENF", "HON", "44", "P1", "SO", holder="sobrecarga", note="Posición principal"),
    PositionSpec("FON", "HON", "30", "P1", "SC"),
    PositionSpec("FON", "HON", "22", "P1", "SN"),
    PositionSpec("MED", "HON", "6", "P1", "SC", note="Tarifa específica del cargo"),
    PositionSpec(
        "PSI", "HON", "32", "P1", "SC", end=_d(3, 31), holder="cambio_abril", note="Tramo anterior al cambio de jornada"
    ),
    PositionSpec("PSI", "HON", "41", "P1", "SC", start=_d(4, 1), holder="cambio_abril", note="Jornada ampliada"),
    PositionSpec("TNS", "HON", "28", "P1", "SC"),
    PositionSpec("TNS", "HON", "15", "P1", "SC"),
    PositionSpec("KIN", "HON", "44", "P1", "SN", start=_d(3, 2)),
    PositionSpec("ADM", "PLA", "44", "P1", "SC", grade=15),
    PositionSpec(
        "PSI", "HON", "44", "P1", "SC", start=_d(10, 1), holder=VACANCY, quantity=2, note="Vacantes en concurso"
    ),
    PositionSpec("TOC", "HON", "44", "P1", "SC", start=_d(3, 9), end=_d(3, 31), note="Reemplazo de tres semanas"),
    PositionSpec("TSO", "HON", "22", "P1", "SS", end=_d(2, 28), note="Refuerzo de verano"),
    PositionSpec("PSI", "HON", "44", "P2", "SN"),
    PositionSpec("PSI", "HON", "44", "P2", "SN"),
    PositionSpec("TOC", "HON", "44", "P2", "SS"),
    PositionSpec(
        "TOC", "HON", "22", "P2", "SN", end=_d(3, 8), holder="cambio_marzo", note="Tramo anterior al cambio de jornada"
    ),
    PositionSpec("TOC", "HON", "44", "P2", "SN", start=_d(3, 9), holder="cambio_marzo", note="Jornada completa"),
    PositionSpec("TSO", "HON", "33", "P2", "SS"),
    PositionSpec("TSO", "HON", "7.5", "P2", "SC", holder="concurrente", note="Segunda posición simultánea"),
    PositionSpec("ENF", "HON", "22", "P2", "SN", start=_d(4, 1)),
    PositionSpec("FON", "HON", "30", "P2", "SS", start=_d(3, 2)),
    PositionSpec("PSI", "HON", "18", "P2", "SO"),
    PositionSpec("PSI", "HON", "11", "P2", "SN", start=_d(4, 1)),
    PositionSpec("TNS", "HON", "15", "P2", "SS"),
    PositionSpec("TOC", "PF", "44", "P2", "SN", grade=15),
    PositionSpec("PSI", "PF", "44", "P2", "SS", grade=12, note="Grado real 12: la diferencia la asume el municipio"),
    PositionSpec("TSO", "PF", "22", "P2", "SN", grade=15),
    PositionSpec("PSI", "HON", "22", "P2", "SN", end=_d(2, 28), note="Refuerzo de verano"),
    PositionSpec("FON", "HON", "22", "P2", "SO", end=_d(3, 31)),
    PositionSpec("TSO", "HON", "22", "P2", "SN", start=_d(10, 5), end=_d(10, 30), note="Contrato de cuatro semanas"),
    PositionSpec("PSI", "HON", "44", "P3", "SO"),
    PositionSpec("ENF", "HON", "44", "P3", "SO", start=_d(4, 1)),
    PositionSpec("TOC", "HON", "30", "P3", "SP"),
    PositionSpec("PSI", "HON", "22", "P3", "SP", start=_d(3, 2)),
    PositionSpec("PSI", "HON", "44", "P3", "SS", holder="por_horas", note="Posición con jornada semanal"),
    PositionSpec("PSI", "HPH", "38", "P3", "SS", holder="por_horas", note="Turnos de extensión horaria"),
    PositionSpec("ENF", "HPH", "38", "P3", "SS", note="Turnos de extensión horaria"),
    PositionSpec("TNS", "HPH", "30", "P3", "SO", note="Turnos de extensión horaria"),
    PositionSpec("TSO", "HON", "15", "P3", "SP", start=_d(4, 1)),
    PositionSpec("TEC", "HON", "11", "P3", "SP", end=_d(2, 28), note="Refuerzo de verano"),
    PositionSpec("PSI", "HON", "44", "P4", "SP"),
    PositionSpec("TOC", "HON", "22", "P4", "SP", start=_d(3, 2)),
    PositionSpec("ENF", "HON", "7.5", "P4", "SO", holder="sobrecarga", note="Segunda posición simultánea"),
    PositionSpec("TEC", "HON", "22", "P4", "SP"),
    PositionSpec("PSI", "HON", "7.5", "P4", "SC"),
    PositionSpec("TSO", "HON", "15", "P4", "SP", end=_d(3, 31)),
    PositionSpec("FON", "HON", "37", "P4", "SO", start=_d(4, 1)),
    PositionSpec("TSO", "HON", "22", "P4", "SP", start=_d(9, 1), holder=VACANCY, note="Vacante presupuestada"),
)

EXPANSION_START = _d(7, 1)
EXPANSION_STAFF = (
    PositionSpec("PSI", "HON", "44", "P1", "SC", start=EXPANSION_START, holder=VACANCY, quantity=2, note="Expansión"),
    PositionSpec("TNS", "HON", "44", "P1", "SC", start=EXPANSION_START, holder=VACANCY, note="Expansión"),
    PositionSpec("TSO", "HON", "44", "P2", "SN", start=EXPANSION_START, holder=VACANCY, note="Expansión"),
    PositionSpec("FON", "HON", "22", "P2", "SS", start=_d(8, 3), holder=VACANCY, note="Expansión"),
    PositionSpec("TOC", "HON", "44", "P3", "SO", start=EXPANSION_START, holder=VACANCY, note="Expansión"),
    PositionSpec("ENF", "HON", "22", "P4", "SP", start=EXPANSION_START, holder=VACANCY, note="Expansión"),
)
RECONVERSION_DATE = _d(5, 1)
RECONVERTED_PROGRAMS = ("P1", "P2")
RECONVERSION_MIN_HOURS = Decimal(22)
EXPECTED_ABSENCE = Decimal("0.03")

# Ítem de recurso humano de cada programa respecto del costo proyectado de la dotación vigente.
HR_ITEM_FACTORS = {"P1": Decimal("1.07"), "P2": Decimal("0.965"), "P3": Decimal("1.14"), "P4": Decimal("1.04")}


@dataclass
class _Refs:
    sites: dict[str, Site]
    programs: dict[str, Program]
    roles: dict[str, JobRole]
    contracts: dict[str, ContractType]


class _PeopleFactory:
    """Nombres y RUT sintéticos únicos."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used_names: set[str] = set()
        self.used_ruts: set[int] = set()

    def name(self) -> str:
        while True:
            first = self.rng.choice(FIRST_NAMES)
            last_1, last_2 = self.rng.sample(SURNAMES, 2)
            full = f"{first} {last_1} {last_2}"
            if full not in self.used_names:
                self.used_names.add(full)
                return full

    def rut(self) -> str:
        while True:
            body = self.rng.randint(*RUT_RANGE)
            if body not in self.used_ruts:
                self.used_ruts.add(body)
                return f"{body}-{check_digit(body)}"


def _rate(value: int, year: int) -> int:
    """Tarifa del año redondeada a decenas de pesos."""
    return round_pesos(Decimal(value) * RATE_INDEX[year] / 10) * 10


def _seed_catalog(catalog: CatalogRepository) -> _Refs:
    return _Refs(
        sites={code: catalog.add_site(code, name) for code, name in SITES},
        programs={code: catalog.add_program(code, name) for code, name in PROGRAMS},
        roles={code: catalog.add_job_role(code, name, category) for code, name, category in JOB_ROLES},
        contracts={
            code: catalog.add_contract_type(code, name, method, retention)
            for code, name, method, retention in CONTRACT_TYPES
        },
    )


def _seed_parameters(params: ParameterRepository, refs: _Refs) -> None:
    for year in RATE_INDEX:
        for category, value in CATEGORY_RATES_2026.items():
            params.set_category_rate(category, year, _rate(value, year))
        for role_code, value in ROLE_RATES_2026.items():
            params.set_role_rate(refs.roles[role_code].id, year, _rate(value, year))
    for year, rate in RETENTION_RATES.items():
        params.set_retention_rate(year, Decimal(rate))
    for category, amount in SALARY_SCALE_2025.items():
        params.set_salary_scale(category, date(2025, 1, 1), amount)
    for valid_from, percent, description in SALARY_ADJUSTMENTS:
        params.set_salary_adjustment(valid_from, percent, description)
    # Aporte del empleador: 0 % por defecto (dato no provisto por el usuario).
    for code in ("PF", "PLA"):
        for year in RATE_INDEX:
            params.set_contribution(refs.contracts[code].id, year, Decimal(0))
    params.save_settings(CostSettings(weeks_per_month=Decimal(4), retention_basis=RetentionBasis.SERVICE))


def _valid(spec: PositionSpec, refs: _Refs, person: Person | None, item_id: int) -> ValidPosition:
    contract = refs.contracts[spec.contract]
    minutes = hours_to_minutes(spec.hours)
    weekly = contract.cost_method.uses_weekly_hours
    return ValidPosition(
        job_role_id=refs.roles[spec.role].id,
        contract_type_id=contract.id,
        site_id=refs.sites[spec.site].id,
        program_id=refs.programs[spec.program].id,
        budget_item_id=item_id,
        person_id=None if person is None else person.id,
        weekly_minutes=minutes if weekly else None,
        monthly_minutes=None if weekly else minutes,
        quantity=spec.quantity,
        start_date=spec.start,
        end_date=spec.end,
        grade=spec.grade,
        note=spec.note,
    )


def _reconversion_segments(spec: PositionSpec) -> tuple[PositionSpec, ...]:
    """Honorarios anuales de 22 h o más de los programas elegidos: dos tramos, el segundo a plazo fijo."""
    full_year = spec.start <= _d(1, 1) and spec.end is None
    eligible = (
        spec.contract == "HON"
        and spec.holder != VACANCY
        and spec.program in RECONVERTED_PROGRAMS
        and Decimal(spec.hours) >= RECONVERSION_MIN_HOURS
        and full_year
    )
    if not eligible:
        return (spec,)
    return (
        replace(spec, end=RECONVERSION_DATE - timedelta(days=1), note="Honorarios hasta la reconversión"),
        replace(spec, contract="PF", grade=15, start=RECONVERSION_DATE, note="Reconversión a plazo fijo"),
    )


PLACEHOLDER_HR_AMOUNT = 400_000_000


def _seed_financial_structure(conn: sqlite3.Connection, refs: _Refs) -> dict[str, int]:
    """Ítems de recurso humano y de operación por programa, con un monto provisorio.

    El monto de recurso humano se ajusta al costo real proyectado en
    `_resize_hr_items`, una vez creadas las posiciones (los ítems deben
    existir antes, porque cada posición se imputa a uno).
    """
    financial = FinancialRepository(conn)
    hr_item_by_program: dict[str, int] = {}
    for code, program in refs.programs.items():
        hr_item = financial.add_item(
            BudgetItemDraft(
                program.id,
                DEMO_YEAR,
                f"{code}-RRHH",
                "Recurso humano",
                BudgetItemType.HUMAN_RESOURCES,
                PLACEHOLDER_HR_AMOUNT,
            )
        )
        hr_item_by_program[code] = hr_item.id
        for item_code, name, factor in OPERATION_ITEMS:
            amount = round_pesos(Decimal(PLACEHOLDER_HR_AMOUNT) * factor / 10_000) * 10_000
            financial.add_item(
                BudgetItemDraft(program.id, DEMO_YEAR, f"{code}-{item_code}", name, BudgetItemType.OPERATION, amount)
            )
    return hr_item_by_program


def _seed_other_expenses(conn: sqlite3.Connection, refs: _Refs, scenario_id: int) -> None:
    """Un par de otros gastos por programa: uno mensual recurrente y uno único."""
    financial = FinancialRepository(conn)
    for program in refs.programs.values():
        items = {
            item.code.rsplit("-", 1)[1]: item
            for item in financial.items(program.id, DEMO_YEAR)
            if item.code.endswith(("ARR", "CAP"))
        }
        rent = items.get("ARR")
        training = items.get("CAP")
        if rent is not None:
            financial.add_other_expense(
                OtherExpenseDraft(
                    scenario_id,
                    rent.id,
                    f"Arriendo de sede de {program.name}",
                    OtherExpenseType.MONTHLY,
                    date(DEMO_YEAR, 1, 1),
                    None,
                    rent.amount // 12 or 1,
                )
            )
        if training is not None:
            financial.add_other_expense(
                OtherExpenseDraft(
                    scenario_id,
                    training.id,
                    f"Jornada de capacitación de {program.name}",
                    OtherExpenseType.ONE_TIME,
                    date(DEMO_YEAR, 5, 15),
                    None,
                    training.amount,
                )
            )


def seed_demo_data(conn: sqlite3.Connection, seed: int = SEED) -> None:
    """Puebla una base recién creada con el caso de ejemplo completo."""
    rng = random.Random(seed)
    people = _PeopleFactory(rng)
    with transaction(conn):
        catalog = CatalogRepository(conn)
        params = ParameterRepository(conn)
        scenarios = ScenarioRepository(conn)
        refs = _seed_catalog(catalog)
        _seed_parameters(params, refs)

        # Ítems provisorios (el monto real se calcula después de costear las posiciones).
        hr_items = _seed_financial_structure(conn, refs)

        shared: dict[str, Person] = {}
        holders: list[Person | None] = []
        for spec in CURRENT_STAFF:
            if spec.holder == VACANCY:
                holders.append(None)
            elif spec.holder in shared:
                holders.append(shared[spec.holder])
            else:
                person = catalog.add_person(people.name(), people.rut())
                if spec.holder != NEW:
                    shared[spec.holder] = person
                holders.append(person)

        current_id = scenarios.insert(
            ScenarioDraft(
                name="Dotación vigente",
                year=DEMO_YEAR,
                description="Dotación contratada para el año, con dos vacantes en concurso y una presupuestada.",
                partial_month_method=PartialMonthMethod.PROPORTIONAL,
                expected_absence=EXPECTED_ABSENCE,
            ),
            CREATED_AT,
        )
        expansion_id = scenarios.insert(
            ScenarioDraft(
                name="Expansión",
                year=DEMO_YEAR,
                description="Refuerzo de los cuatro programas con siete posiciones nuevas desde julio y agosto.",
                partial_month_method=PartialMonthMethod.PROPORTIONAL,
                expected_absence=EXPECTED_ABSENCE,
                base_scenario_id=current_id,
            ),
            CREATED_AT,
        )
        reconversion_id = scenarios.insert(
            ScenarioDraft(
                name="Reconversión a plazo fijo",
                year=DEMO_YEAR,
                description="Los honorarios anuales de 22 h o más de los programas Base y Comunitario pasan a "
                "plazo fijo desde mayo.",
                partial_month_method=PartialMonthMethod.PROPORTIONAL,
                expected_absence=EXPECTED_ABSENCE,
                base_scenario_id=current_id,
            ),
            CREATED_AT,
        )

        for spec, holder in zip(CURRENT_STAFF, holders, strict=True):
            scenarios.insert_position(current_id, _valid(spec, refs, holder, hr_items[spec.program]))
        for spec, holder in zip(CURRENT_STAFF, holders, strict=True):
            scenarios.insert_position(expansion_id, _valid(spec, refs, holder, hr_items[spec.program]))
        for spec in EXPANSION_STAFF:
            scenarios.insert_position(expansion_id, _valid(spec, refs, None, hr_items[spec.program]))
        for spec, holder in zip(CURRENT_STAFF, holders, strict=True):
            for segment in _reconversion_segments(spec):
                scenarios.insert_position(reconversion_id, _valid(segment, refs, holder, hr_items[spec.program]))

        # Los ítems de operación se ajustan al costo real antes de sembrar otros gastos, para que su
        # magnitud sea proporcional a la dotación (y no a los montos provisorios de la creación de ítems).
        _resize_hr_items(conn, refs, current_id, hr_items)
        for scenario_id in (current_id, expansion_id, reconversion_id):
            _seed_other_expenses(conn, refs, scenario_id)
        _seed_execution(conn, rng, refs, current_id)
    log.info("Datos de ejemplo generados con semilla %d", seed)


def _resize_hr_items(conn: sqlite3.Connection, refs: _Refs, scenario_id: int, hr_items: dict[str, int]) -> None:
    """Ajusta el ítem de recurso humano y los de operación al costo proyectado real, y fija el total del convenio."""
    params = ParameterRepository(conn)
    scenarios = ScenarioRepository(conn)
    financial = FinancialRepository(conn)
    projection = project_scenario(
        scenarios.get(scenario_id), scenarios.positions(scenario_id), params.cost_parameters()
    )
    by_program = {row.key: row for row in projection.breakdown(Dimension.PROGRAM)}
    for code, program in refs.programs.items():
        cost = by_program[program.id].total if program.id in by_program else 0
        hr_item = financial.item(hr_items[code])
        hr_amount = round_pesos(Decimal(cost) * HR_ITEM_FACTORS[code] / 100_000) * 100_000
        financial.update_item(hr_item.id, replace(hr_item.to_draft(), amount=hr_amount))
        total = hr_amount
        for item in financial.items(program.id, DEMO_YEAR):
            if item.item_type is BudgetItemType.HUMAN_RESOURCES:
                continue
            factor = next(factor for op_code, _name, factor in OPERATION_ITEMS if item.code.endswith(op_code))
            amount = round_pesos(Decimal(hr_amount) * factor / 10_000) * 10_000
            financial.update_item(item.id, replace(item.to_draft(), amount=amount))
            total += amount
        financial.set_program_budget(
            ProgramBudget(program.id, DEMO_YEAR, total, date(DEMO_YEAR, 1, 1), date(DEMO_YEAR, 12, 31), "")
        )


def _seed_execution(conn: sqlite3.Connection, rng: random.Random, refs: _Refs, scenario_id: int) -> None:
    """Ejecución sintética por ítem, de enero a agosto, derivada de la proyección de la dotación vigente."""
    params = ParameterRepository(conn)
    scenarios = ScenarioRepository(conn)
    financial = FinancialRepository(conn)
    projection = project_scenario(
        scenarios.get(scenario_id), scenarios.positions(scenario_id), params.cost_parameters()
    )
    by_item = {row.key: row for row in projection.breakdown(Dimension.BUDGET_ITEM)}
    executions = ExecutionRepository(conn)
    for code, program in refs.programs.items():
        for item in financial.items(program.id, DEMO_YEAR):
            if item.item_type is BudgetItemType.HUMAN_RESOURCES:
                row = by_item.get(item.id)
                monthly = row.monthly if row is not None else (0,) * 12
            else:
                other = financial.other_expenses(scenario_id)
                monthly = tuple(
                    sum(expense.monthly_amounts(DEMO_YEAR)[m] for expense in other if expense.budget_item.id == item.id)
                    for m in range(12)
                )
            for month in EXECUTED_MONTHS:
                planned = monthly[month - 1]
                if planned == 0:
                    continue
                factor = Decimal(str(round(min(max(rng.gauss(0.988, 0.014), 0.95), 1.03), 4)))
                if code == "P2" and month == 3:
                    factor += Decimal("0.035")
                amount = round_pesos(Decimal(planned) * factor)
                executions.upsert(item.id, month, amount)
