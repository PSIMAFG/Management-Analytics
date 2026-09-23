"""Lectura y validación fila por fila de planillas de posiciones, otros gastos y ejecución.

Las funciones de este módulo no escriben en la base: devuelven las filas
aceptadas y las rechazadas con su motivo. El servicio decide si guardar
(todo en una sola transacción) o no guardar nada.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.datavalidation import DataValidation

from staffing_simulator.data.excel_style import DATE, HOURS, MONEY, ExcelColumn, write_table, write_title
from staffing_simulator.domain.imports import RejectedRow
from staffing_simulator.domain.models import (
    MONTH_LABELS,
    MONTH_NAMES,
    BudgetItem,
    BudgetItemType,
    Catalog,
    ContractType,
    ExecutionRecord,
    JobRole,
    OtherExpenseDraft,
    OtherExpenseType,
    Person,
    Position,
    PositionDraft,
    Program,
    Site,
)
from staffing_simulator.domain.parameters import CostParameters, ensure_position_costable
from staffing_simulator.domain.rut import format_rut, normalize_rut
from staffing_simulator.domain.units import round_pesos, to_decimal
from staffing_simulator.domain.validation import (
    PositionIdentity,
    identical_position_message,
    validate_grade,
    validate_name,
    validate_position_draft,
    validate_year,
)
from staffing_simulator.errors import AppError, DataError, ValidationError

POSITION_SHEET = "Posiciones"
EXECUTION_SHEET = "Ejecución"
OTHER_EXPENSE_SHEET = "Otros gastos"
LISTS_SHEET = "Listas"
EXCEL_EPOCH = date(1899, 12, 30)

POSITION_COLUMNS = (
    ExcelColumn("Cargo", 26),
    ExcelColumn("Tipo de contrato", 22),
    ExcelColumn("Sede", 16),
    ExcelColumn("Programa", 24),
    ExcelColumn("Ítem de recurso humano", 26),
    ExcelColumn("RUT", 14),
    ExcelColumn("Nombre", 30),
    ExcelColumn("Horas semanales", 12, HOURS),
    ExcelColumn("Horas mensuales", 12, HOURS),
    ExcelColumn("Cantidad", 10),
    ExcelColumn("Grado", 8),
    ExcelColumn("Inicio", 12, DATE),
    ExcelColumn("Término", 12, DATE),
    ExcelColumn("Nota", 36),
)
POSITION_REQUIRED = ("Cargo", "Tipo de contrato", "Sede", "Programa", "Inicio")
EXECUTION_COLUMNS = (
    ExcelColumn("Programa", 26),
    ExcelColumn("Ítem", 26),
    ExcelColumn("Año", 8),
    ExcelColumn("Mes", 8),
    ExcelColumn("Monto ejecutado", 18, MONEY),
)
EXECUTION_REQUIRED = ("Programa", "Ítem", "Año", "Mes", "Monto ejecutado")
OTHER_EXPENSE_COLUMNS = (
    ExcelColumn("Descripción", 32),
    ExcelColumn("Programa", 24),
    ExcelColumn("Ítem", 26),
    ExcelColumn("Tipo", 20),
    ExcelColumn("Inicio", 12, DATE),
    ExcelColumn("Término", 12, DATE),
    ExcelColumn("Monto", 16, MONEY),
)
OTHER_EXPENSE_REQUIRED = ("Descripción", "Programa", "Ítem", "Tipo", "Inicio", "Monto")
EXAMPLE_NOTE = "Fila de ejemplo: copie el formato en la hoja Posiciones"

T = TypeVar("T", JobRole, ContractType, Site, Program)

NBSP = chr(0xA0)
# Montos en texto: solo dígitos o miles agrupados con punto, y hasta dos decimales con coma.
AMOUNT_TEXT = re.compile(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?")
NUMBER_CHARACTERS = re.compile(r"[\d.,]+")
# Abreviaturas de meses de uso común además de las tres primeras letras.
MONTH_ALIASES = {"sept": 9, "set": 9, "setiembre": 9}


def normalize_key(text: object) -> str:
    """Texto comparable: sin tildes, en minúsculas y con espacios simples."""
    plain = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(plain.casefold().split())


# Claves de las filas leídas: el encabezado visible normalizado con normalize_key.
YEAR_KEY = normalize_key("Año")
END_KEY = normalize_key("Término")


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def read_rows(path: Path, sheet_name: str, required: Sequence[str]) -> list[tuple[int, dict[str, Any]]]:
    """Filas no vacías de la hoja como (número de fila, {encabezado normalizado: valor}).

    `required` son los encabezados visibles obligatorios (por ejemplo, «Año»); si
    falta alguno, el mensaje los nombra tal como aparecen en la plantilla.
    """
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except FileNotFoundError as error:
        raise DataError(f"No se encontró el archivo {path}.") from error
    except PermissionError as error:
        raise DataError(
            "No se pudo abrir el archivo. Ciérrelo si está abierto en Excel e intente nuevamente."
        ) from error
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError) as error:
        raise DataError("El archivo no es una planilla Excel válida (.xlsx).") from error
    try:
        sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        header_row = next(iterator, None)
        if header_row is None:
            raise ValidationError("La planilla está vacía: falta la fila de encabezados.")
        headers = [normalize_key(value) if not _is_empty(value) else "" for value in header_row]
        missing = [name for name in required if normalize_key(name) not in headers]
        if missing:
            raise ValidationError(
                "A la planilla le faltan columnas obligatorias: " + ", ".join(missing) + ". Use la plantilla."
            )
        rows: list[tuple[int, dict[str, Any]]] = []
        for number, values in enumerate(iterator, start=2):
            if all(_is_empty(value) for value in values):
                continue
            rows.append((number, {key: value for key, value in zip(headers, values, strict=False) if key}))
        return rows
    finally:
        workbook.close()


def parse_date_value(value: Any, label: str) -> date | None:
    """Fecha desde una celda: fecha de Excel, número de serie o texto dd-mm-aaaa, dd/mm/aaaa o aaaa-mm-dd."""
    if _is_empty(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        if 1 <= value <= 100_000:
            return EXCEL_EPOCH + timedelta(days=int(value))
        raise ValidationError(f"La {label} no es una fecha válida.")
    text = str(value).strip()
    for pattern in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValidationError(f"La {label} '{text}' no es una fecha válida (use dd-mm-aaaa).")


def parse_decimal_value(value: Any, label: str) -> Decimal | None:
    if _is_empty(value):
        return None
    if isinstance(value, bool):
        raise ValidationError(f"El valor de {label} no es un número.")
    try:
        return to_decimal(value)
    except ValidationError as error:
        raise ValidationError(f"El valor de {label} ('{value}') no es un número.") from error


def _amount_from_text(value: str) -> Decimal:
    """Monto escrito como texto: solo dígitos o miles agrupados con punto, y hasta dos decimales con coma.

    Un texto como «1234567.89» (punto decimal, típico de datos pegados desde
    otro sistema) es ambiguo: borrar el punto multiplicaría el monto por 100.
    """
    text = value.replace("$", "").replace(NBSP, "").replace(" ", "")
    negative = text.startswith("-")
    digits = text.removeprefix("-")
    if not AMOUNT_TEXT.fullmatch(digits):
        shown = value.strip()
        if NUMBER_CHARACTERS.fullmatch(digits):
            raise ValidationError(
                f"Monto ambiguo ('{shown}'): use una celda numérica o el formato 1.234.567 (coma para decimales)."
            )
        raise ValidationError(f"El monto ('{shown}') no es un número.")
    amount = Decimal(digits.replace(".", "").replace(",", "."))
    return -amount if negative else amount


def parse_amount(value: Any, *, label: str = "monto ejecutado", allow_zero: bool = True) -> int:
    """Monto en pesos: celda numérica o texto con formato chileno (1.234.567 o 1.234.567,50)."""
    if _is_empty(value):
        raise ValidationError(f"Falta el {label}.")
    if isinstance(value, str):
        amount: Decimal | None = _amount_from_text(value)
    else:
        amount = parse_decimal_value(value, label)
    if amount is None:
        raise ValidationError(f"Falta el {label}.")
    if amount < 0:
        raise ValidationError(f"El {label} no puede ser negativo.")
    if not allow_zero and amount == 0:
        raise ValidationError(f"El {label} debe ser mayor que 0.")
    return round_pesos(amount)


def parse_whole_number(value: Any, label: str) -> int | None:
    number = parse_decimal_value(value, label)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise ValidationError(f"El valor de {label} debe ser un número entero.")
    return int(number)


def parse_month(value: Any) -> int:
    """Mes desde un número de 1 a 12, su nombre o una abreviatura común («mar», «sept», «set»)."""
    if _is_empty(value):
        raise ValidationError("Falta el mes.")
    key = normalize_key(value).rstrip(".")
    for index, name in enumerate(MONTH_NAMES, start=1):
        if key in (name, name[:3]):
            return index
    if key in MONTH_ALIASES:
        return MONTH_ALIASES[key]
    invalid = ValidationError(f"El mes '{value}' no es válido (use un número de 1 a 12 o el nombre del mes).")
    try:
        month = parse_whole_number(value, "mes")
    except ValidationError as error:
        raise invalid from error
    if month is None or not 1 <= month <= 12:
        raise invalid
    return month


def lookup(items: Iterable[T], value: Any, label: str) -> T:
    """Busca por código o por nombre, sin distinguir mayúsculas ni tildes."""
    if _is_empty(value):
        raise ValidationError(f"Falta {label}.")
    key = normalize_key(value)
    for item in items:
        if key in (normalize_key(item.code), normalize_key(item.name)):
            return item
    raise ValidationError(f"{label[0].upper()}{label[1:]} '{value}' no existe en el catálogo.")


def lookup_item(
    items: Iterable[BudgetItem], program: Program, value: Any, year: int, label: str = "el ítem"
) -> BudgetItem:
    """Busca un ítem presupuestario por código o nombre, dentro del programa y año indicados."""
    if _is_empty(value):
        raise ValidationError(f"Falta {label}.")
    key = normalize_key(value)
    candidates = [item for item in items if item.program.id == program.id and item.year == year]
    for item in candidates:
        if key in (normalize_key(item.code), normalize_key(item.name), normalize_key(item.label)):
            return item
    raise ValidationError(f"{label[0].upper()}{label[1:]} '{value}' no existe en {program.name} para {year}.")


@dataclass(frozen=True)
class NewPerson:
    """Persona que se creará al importar (aún no existe en la base)."""

    full_name: str
    rut: str | None


@dataclass(frozen=True)
class ParsedPosition:
    row_number: int
    draft: PositionDraft
    new_person: NewPerson | None = None


@dataclass(frozen=True)
class PositionParseResult:
    total_rows: int
    positions: tuple[ParsedPosition, ...]
    rejected: tuple[RejectedRow, ...]

    @property
    def new_persons(self) -> tuple[NewPerson, ...]:
        unique: dict[NewPerson, None] = {}
        for item in self.positions:
            if item.new_person is not None:
                unique[item.new_person] = None
        return tuple(unique)


class _PersonResolver:
    """Resuelve la persona de una fila por RUT o por nombre, sin duplicarla."""

    def __init__(self, persons: Sequence[Person]) -> None:
        self.by_rut = {person.rut: person for person in persons if person.rut}
        self.by_name: dict[str, list[Person]] = {}
        for person in persons:
            self.by_name.setdefault(normalize_key(person.full_name), []).append(person)
        self.pending_by_rut: dict[str, NewPerson] = {}
        self.pending_by_name: dict[str, NewPerson] = {}

    def resolve(self, rut_value: Any, name_value: Any) -> tuple[int | None, NewPerson | None]:
        name = None if _is_empty(name_value) else validate_name(str(name_value), "El nombre de la persona")
        rut = None if _is_empty(rut_value) else normalize_rut(str(rut_value))
        if rut is not None:
            existing = self.by_rut.get(rut)
            if existing is not None:
                if name is not None and normalize_key(name) != normalize_key(existing.full_name):
                    raise ValidationError(
                        f"El RUT {format_rut(rut)} ya está registrado con otro nombre ({existing.full_name})."
                    )
                return existing.id, None
            pending = self.pending_by_rut.get(rut)
            if pending is not None:
                if name is not None and normalize_key(name) != normalize_key(pending.full_name):
                    raise ValidationError(f"El RUT {format_rut(rut)} aparece en la planilla con dos nombres distintos.")
                return None, pending
            if name is None:
                raise ValidationError(f"El RUT {format_rut(rut)} no está registrado: indique también el nombre.")
            new = NewPerson(name, rut)
            self.pending_by_rut[rut] = new
            return None, new
        if name is None:
            return None, None
        matches = self.by_name.get(normalize_key(name), [])
        if len(matches) > 1:
            raise ValidationError(f"Hay varias personas llamadas {name}: indique el RUT para distinguirlas.")
        if matches:
            return matches[0].id, None
        key = normalize_key(name)
        pending = self.pending_by_name.get(key)
        if pending is None:
            pending = NewPerson(name, None)
            self.pending_by_name[key] = pending
        return None, pending


def _duplicate_reason(
    identity: PositionIdentity,
    existing: Mapping[PositionIdentity, Position],
    seen: Mapping[PositionIdentity, int],
) -> str | None:
    """Motivo de rechazo si la fila repite una posición del escenario u otra fila de la planilla."""
    if identity in existing:
        return identical_position_message(existing[identity])
    if identity in seen:
        reason = f"Es idéntica a la fila {seen[identity]} de la planilla."
        if identity.holder is None:
            reason += " Para varias vacantes iguales use la columna Cantidad en una sola fila."
        return reason
    return None


def parse_position_rows(
    rows: Sequence[tuple[int, dict[str, Any]]],
    catalog: Catalog,
    budget_items: Sequence[BudgetItem],
    scenario_year: int,
    params: CostParameters,
    existing: Sequence[Position] = (),
) -> PositionParseResult:
    """Valida cada fila como una posición del escenario; nunca detiene la lectura por una fila mala.

    Una fila idéntica a una posición del escenario (`existing`) o a otra fila
    ya aceptada de la planilla se rechaza con el motivo: así, importar dos
    veces la misma planilla no duplica la dotación. El ítem presupuestario es
    opcional en la planilla: si se deja vacío, el servicio le asigna el
    primer ítem de recurso humano del programa.
    """
    resolver = _PersonResolver(catalog.persons)
    current = {PositionIdentity.of_position(item): item for item in existing}
    seen: dict[PositionIdentity, int] = {}
    accepted: list[ParsedPosition] = []
    rejected: list[RejectedRow] = []
    for row_number, values in rows:
        try:
            role = lookup(catalog.job_roles, values.get("cargo"), "el cargo")
            contract = lookup(catalog.contract_types, values.get("tipo de contrato"), "el tipo de contrato")
            site = lookup(catalog.sites, values.get("sede"), "la sede")
            program = lookup(catalog.programs, values.get("programa"), "el programa")
            item_value = values.get("item de recurso humano")
            if _is_empty(item_value):
                default = next(
                    (
                        i
                        for i in budget_items
                        if i.program.id == program.id
                        and i.year == scenario_year
                        and i.item_type is BudgetItemType.HUMAN_RESOURCES
                    ),
                    None,
                )
                if default is None:
                    raise ValidationError(
                        f"El programa {program.name} no tiene ítems de recurso humano en {scenario_year}: "
                        "cárguelos en Estructura del programa o indique el ítem en la planilla."
                    )
                budget_item_id = default.id
            else:
                budget_item_id = lookup_item(
                    budget_items, program, item_value, scenario_year, "el ítem de recurso humano"
                ).id
            start = parse_date_value(values.get("inicio"), "fecha de inicio")
            if start is None:
                raise ValidationError("Falta la fecha de inicio.")
            quantity = parse_whole_number(values.get("cantidad"), "cantidad")
            grade = parse_whole_number(values.get("grado"), "grado")
            person_id, new_person = resolver.resolve(values.get("rut"), values.get("nombre"))
            draft = PositionDraft(
                job_role_id=role.id,
                contract_type_id=contract.id,
                site_id=site.id,
                program_id=program.id,
                budget_item_id=budget_item_id,
                start_date=start,
                end_date=parse_date_value(values.get(END_KEY), "fecha de término"),
                person_id=person_id,
                weekly_hours=parse_decimal_value(values.get("horas semanales"), "horas semanales"),
                monthly_hours=parse_decimal_value(values.get("horas mensuales"), "horas mensuales"),
                quantity=1 if quantity is None else quantity,
                grade=grade,
                note="" if _is_empty(values.get("nota")) else str(values.get("nota")),
            )
            if new_person is not None and draft.quantity != 1:
                raise ValidationError("Una posición asignada a una persona debe tener cantidad 1.")
            validate_grade(draft.grade, contract)
            valid = validate_position_draft(draft, contract, scenario_year)
            ensure_position_costable(params, role, contract, scenario_year)
            identity = PositionIdentity.of_valid(valid, new_person)
            duplicate = _duplicate_reason(identity, current, seen)
            if duplicate is not None:
                raise ValidationError(duplicate)
        except AppError as error:
            rejected.append(RejectedRow(row_number, error.user_message))
            continue
        seen[identity] = row_number
        accepted.append(ParsedPosition(row_number, draft, new_person))
    return PositionParseResult(len(rows), tuple(accepted), tuple(rejected))


@dataclass(frozen=True)
class ExecutionParseResult:
    total_rows: int
    records: tuple[tuple[int, ExecutionRecord], ...]
    rejected: tuple[RejectedRow, ...]


def parse_execution_rows(
    rows: Sequence[tuple[int, dict[str, Any]]], items: Sequence[BudgetItem]
) -> ExecutionParseResult:
    """Valida cada fila de ejecución (programa, ítem, año, mes y monto) y detecta duplicados.

    Las filas con el monto en blanco se omiten: la plantilla trae una fila por
    ítem y mes y solo se completan los meses ya ejecutados.
    """
    programs = {item.program.id: item.program for item in items}
    program_list = list(programs.values())
    accepted: list[tuple[int, ExecutionRecord]] = []
    rejected: list[RejectedRow] = []
    seen: dict[tuple[int, int], int] = {}
    rows = [(number, values) for number, values in rows if not _is_empty(values.get("monto ejecutado"))]
    for row_number, values in rows:
        try:
            program = lookup(program_list, values.get("programa"), "el programa")
            year = parse_whole_number(values.get(YEAR_KEY), "año")
            if year is None:
                raise ValidationError("Falta el año.")
            validate_year(year)
            item = lookup_item(items, program, values.get("item"), year, "el ítem")
            month = parse_month(values.get("mes"))
            amount = parse_amount(values.get("monto ejecutado"))
            key = (item.id, month)
            if key in seen:
                raise ValidationError(f"Repite el ítem, año y mes de la fila {seen[key]}.")
            seen[key] = row_number
        except AppError as error:
            rejected.append(RejectedRow(row_number, error.user_message))
            continue
        accepted.append((row_number, ExecutionRecord(item.id, year, month, amount)))
    return ExecutionParseResult(len(rows), tuple(accepted), tuple(rejected))


@dataclass(frozen=True)
class ParsedOtherExpense:
    row_number: int
    draft: OtherExpenseDraft


@dataclass(frozen=True)
class OtherExpenseParseResult:
    total_rows: int
    expenses: tuple[ParsedOtherExpense, ...]
    rejected: tuple[RejectedRow, ...]


EXPENSE_TYPE_LABELS = {
    normalize_key(OtherExpenseType.MONTHLY.label): OtherExpenseType.MONTHLY,
    "mensual": OtherExpenseType.MONTHLY,
    normalize_key(OtherExpenseType.ONE_TIME.label): OtherExpenseType.ONE_TIME,
    "unico": OtherExpenseType.ONE_TIME,
}


def _parse_expense_type(value: Any) -> OtherExpenseType:
    key = normalize_key(value)
    match = EXPENSE_TYPE_LABELS.get(key)
    if match is None:
        raise ValidationError(f"El tipo de gasto '{value}' debe ser 'Mensual recurrente' o 'Único'.")
    return match


def parse_other_expense_rows(
    rows: Sequence[tuple[int, dict[str, Any]]], scenario_id: int, items: Sequence[BudgetItem], year: int
) -> OtherExpenseParseResult:
    """Valida cada fila de otro gasto planificado del escenario."""
    programs = {item.program.id: item.program for item in items}
    program_list = list(programs.values())
    accepted: list[ParsedOtherExpense] = []
    rejected: list[RejectedRow] = []
    for row_number, values in rows:
        try:
            description = validate_name(str(values.get("descripcion") or ""), "La descripción del gasto")
            program = lookup(program_list, values.get("programa"), "el programa")
            item = lookup_item(items, program, values.get("item"), year, "el ítem")
            if item.item_type is BudgetItemType.HUMAN_RESOURCES:
                raise ValidationError("Los otros gastos no se imputan a un ítem de recurso humano.")
            expense_type = _parse_expense_type(values.get("tipo"))
            start = parse_date_value(values.get("inicio"), "fecha de inicio")
            if start is None:
                raise ValidationError("Falta la fecha de inicio.")
            end = (
                None
                if expense_type is OtherExpenseType.ONE_TIME
                else parse_date_value(values.get(END_KEY), "fecha de término")
            )
            amount = parse_amount(values.get("monto"), label="monto del gasto", allow_zero=False)
        except AppError as error:
            rejected.append(RejectedRow(row_number, error.user_message))
            continue
        draft = OtherExpenseDraft(scenario_id, item.id, description, expense_type, start, end, amount)
        accepted.append(ParsedOtherExpense(row_number, draft))
    return OtherExpenseParseResult(len(rows), tuple(accepted), tuple(rejected))


def _list_validation(ws_range: str, source: str) -> DataValidation:
    validation = DataValidation(type="list", formula1=source, allow_blank=True, showErrorMessage=False)
    validation.add(ws_range)
    return validation


def write_positions_template(path: Path, catalog: Catalog, budget_items: Sequence[BudgetItem], year: int) -> Path:
    """Plantilla de posiciones: la hoja de datos solo con encabezados y listas de valores válidos.

    La fila de ejemplo va en la hoja Instrucciones, que no se importa, para
    que nunca termine guardada como una posición real.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = POSITION_SHEET
    write_table(sheet, POSITION_COLUMNS, [])
    lists = workbook.create_sheet(LISTS_SHEET)
    groups: list[tuple[str, list[str]]] = [
        ("Cargos", [role.name for role in catalog.job_roles]),
        ("Tipos de contrato", [item.name for item in catalog.contract_types]),
        ("Sedes", [site.name for site in catalog.sites]),
        ("Programas", [program.name for program in catalog.programs]),
        (
            "Ítems de recurso humano",
            [
                item.label
                for item in budget_items
                if item.item_type is BudgetItemType.HUMAN_RESOURCES and item.year == year
            ],
        ),
    ]
    for column, (title, values) in enumerate(groups, start=1):
        lists.cell(row=1, column=column, value=title)
        for row, value in enumerate(values, start=2):
            lists.cell(row=row, column=column, value=value)
        letter = get_column_letter(column)
        lists.column_dimensions[letter].width = 30
        if values:
            source = f"={LISTS_SHEET}!${letter}$2:${letter}${len(values) + 1}"
            sheet.add_data_validation(_list_validation(f"{letter}2:{letter}1000", source))
    _write_instructions(workbook, catalog, year)
    return _save(workbook, path)


def _write_instructions(workbook: Workbook, catalog: Catalog, year: int) -> None:
    notes = workbook.create_sheet("Instrucciones")
    instructions = (
        "Una fila por posición en la hoja Posiciones. Las columnas Cargo, Tipo de contrato, Sede, Programa e Inicio "
        "son obligatorias.",
        "Ítem de recurso humano: opcional. Si se deja vacío, se usa el primer ítem de recurso humano del programa.",
        "Honorarios y contratos dependientes: complete Horas semanales y deje vacías las Horas mensuales.",
        "Honorarios por horas: complete Horas mensuales estimadas y deje vacías las Horas semanales.",
        "Grado: solo informativo, para plazo fijo y planta (el costo siempre usa el grado 15).",
        "Término vacío significa vigencia hasta fin de año. Fechas en formato dd-mm-aaaa.",
        "RUT y Nombre son opcionales: si ambos están vacíos la posición se registra como vacante.",
        "Si alguna fila tiene errores no se importa nada, salvo que elija importar solo las filas válidas.",
    )
    write_title(notes, 1, "Instrucciones")
    for row, text in enumerate(instructions, start=2):
        notes.cell(row=row, column=1, value=text)
    example = _example_row(catalog, year)
    if example is None:
        return
    first = len(instructions) + 3
    write_title(notes, first, "Ejemplo de fila (esta hoja no se importa)")
    write_table(notes, POSITION_COLUMNS, [example], start_row=first + 1, freeze=False)


def _example_row(catalog: Catalog, year: int) -> list[Any] | None:
    role = next((item for item in catalog.job_roles if item.category == "B"), None)
    contract = next((item for item in catalog.contract_types if item.cost_method.uses_weekly_hours), None)
    if role is None or contract is None or not catalog.sites or not catalog.programs:
        return None
    return [
        role.name,
        contract.name,
        catalog.sites[0].name,
        catalog.programs[0].name,
        None,
        None,
        None,
        22,
        None,
        1,
        None,
        date(year, 3, 1),
        date(year, 12, 31),
        EXAMPLE_NOTE,
    ]


def write_execution_template(path: Path, items: Sequence[BudgetItem], year: int) -> Path:
    """Plantilla de ejecución: una fila por ítem y mes, con el monto en blanco."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = EXECUTION_SHEET
    rows = [[item.program.name, item.label, year, month, None] for item in items for month in range(1, 13)]
    write_table(sheet, EXECUTION_COLUMNS, rows)
    lists = workbook.create_sheet(LISTS_SHEET)
    lists.cell(row=1, column=1, value="Ítems")
    for row, item in enumerate(items, start=2):
        lists.cell(row=row, column=1, value=item.label)
    lists.cell(row=1, column=2, value="Meses")
    for row, label in enumerate(MONTH_LABELS, start=2):
        lists.cell(row=row, column=2, value=label)
    lists.column_dimensions["A"].width = 34
    if items:
        sheet.add_data_validation(_list_validation("B2:B4000", f"={LISTS_SHEET}!$A$2:$A${len(items) + 1}"))
    return _save(workbook, path)


def write_other_expenses_template(path: Path, items: Sequence[BudgetItem], year: int) -> Path:
    """Plantilla de otros gastos: encabezados y listas de programas e ítems que no son de recurso humano."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = OTHER_EXPENSE_SHEET
    write_table(sheet, OTHER_EXPENSE_COLUMNS, [])
    lists = workbook.create_sheet(LISTS_SHEET)
    non_hr = [item for item in items if item.item_type is not BudgetItemType.HUMAN_RESOURCES]
    lists.cell(row=1, column=1, value="Programas")
    programs = sorted({item.program.name for item in non_hr})
    for row, name in enumerate(programs, start=2):
        lists.cell(row=row, column=1, value=name)
    lists.cell(row=1, column=2, value="Ítems")
    for row, item in enumerate(non_hr, start=2):
        lists.cell(row=row, column=2, value=item.label)
    lists.cell(row=1, column=3, value="Tipo")
    for row, expense_type in enumerate(OtherExpenseType, start=2):
        lists.cell(row=row, column=3, value=expense_type.label)
    for column in (1, 2, 3):
        letter = get_column_letter(column)
        lists.column_dimensions[letter].width = 30
    if programs:
        sheet.add_data_validation(_list_validation("B2:B2000", f"={LISTS_SHEET}!$A$2:$A${len(programs) + 1}"))
    if non_hr:
        sheet.add_data_validation(_list_validation("C2:C2000", f"={LISTS_SHEET}!$B$2:$B${len(non_hr) + 1}"))
    sheet.add_data_validation(
        _list_validation("D2:D2000", f"={LISTS_SHEET}!$C$2:$C${len(tuple(OtherExpenseType)) + 1}")
    )
    notes = workbook.create_sheet("Instrucciones")
    write_title(notes, 1, "Instrucciones")
    instructions = (
        f"Año de los ítems de la lista: {year}.",
        "Tipo 'Mensual recurrente': se paga entero cada mes entre Inicio y Término (vacío = hasta fin de año).",
        "Tipo 'Único': se paga entero en el mes de Inicio; deje Término vacío.",
    )
    for row, text in enumerate(instructions, start=2):
        notes.cell(row=row, column=1, value=text)
    return _save(workbook, path)


def _save(workbook: Workbook, path: Path) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(path)
    except PermissionError as error:
        raise DataError(
            "No se pudo guardar el archivo. Ciérrelo si está abierto en Excel e intente nuevamente."
        ) from error
    except OSError as error:
        raise DataError(f"No se pudo guardar el archivo {path.name}: {error.strerror or error}.") from error
    return path
