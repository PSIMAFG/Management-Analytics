"""Validación fila por fila de observaciones importadas en formato largo.

Formato: sede, indicador, año, mes, numerador, denominador y, opcionalmente,
reportado. Las sedes y los indicadores se identifican por código; nunca se
adivina una sede por parecido de nombre.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Origin, Scale
from kpi_monitor.domain.models import Indicator, Observation
from kpi_monitor.domain.text import join_months
from kpi_monitor.errors import ValidationError

_THOUSANDS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")
_TRUE = {"si", "sí", "s", "1", "true", "verdadero", "x"}
_FALSE = {"no", "n", "0", "false", "falso"}


@dataclass(frozen=True)
class RawRow:
    """Fila tal como viene del archivo (valores sin interpretar)."""

    row_number: int
    site: object
    indicator: object
    year: object
    month: object
    numerator: object
    denominator: object
    reported: object = None


@dataclass(frozen=True)
class RejectedRow:
    """Fila descartada con el motivo, para informarla al usuario."""

    row_number: int
    site: str
    indicator: str
    period: str
    reason: str


@dataclass(frozen=True)
class ImportValidation:
    """Resultado de validar un archivo completo."""

    accepted: tuple[Observation, ...]
    rejected: tuple[RejectedRow, ...]
    total_rows: int

    @property
    def is_clean(self) -> bool:
        return not self.rejected


@dataclass(frozen=True)
class InputSpec:
    """Cómo se capturan el numerador y el denominador de un indicador.

    Es la única definición de esas reglas: la usan la validación de filas, la
    plantilla de importación y el diálogo de registro manual.
    """

    numerator_hint: str
    denominator_enabled: bool
    denominator_required: bool
    denominator_label: str
    denominator_hint: str


def input_spec(indicator: Indicator) -> InputSpec:
    """Qué va en el numerador y en el denominador según el tipo de indicador."""
    if indicator.aggregation is Aggregation.STOCK:
        numerator = f"Valor al corte (no se suma entre meses); se informa en {join_months(indicator.stock_months)}."
    elif indicator.scale is Scale.DAYS:
        numerator = "Suma de los días de espera de las personas atendidas en el mes."
    else:
        numerator = "Cantidad del mes; el acumulado del año es la suma de los meses."
    kind = indicator.denominator_type
    if kind is DenominatorType.FIXED_SITE:
        return InputSpec(
            numerator,
            False,
            False,
            "Denominador",
            "El denominador es la meta fija anual de la sede; deje la celda vacía.",
        )
    if kind is DenominatorType.POPULATION:
        return InputSpec(
            numerator,
            False,
            False,
            "Denominador",
            "El denominador se calcula con la población de referencia de la sede; deje la celda vacía.",
        )
    if kind in (DenominatorType.STOCK, DenominatorType.K_STOCK):
        months = join_months(indicator.stock_months)
        return InputSpec(
            numerator,
            True,
            False,
            "Personas en seguimiento (stock)",
            f"Personas al corte; se informa en {months}. En los demás meses queda vacío.",
        )
    if kind is DenominatorType.MANUAL:
        return InputSpec(
            numerator, True, True, "Denominador", "Total comprometido en el plan del mes (carga manual); obligatorio."
        )
    return InputSpec(
        numerator, True, True, "Denominador", "Total del mes sobre el que se calcula la proporción; obligatorio."
    )


class _RowError(Exception):
    """Motivo de rechazo de una fila (uso interno)."""


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_number(value: object) -> float | None:
    """Convierte un número de Excel o un texto con formato chileno ('1.234,5')."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise _RowError("se esperaba un número y se encontró un valor lógico")
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = str(value).strip().replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        elif _THOUSANDS.match(text):
            text = text.replace(".", "")
        try:
            number = float(text)
        except ValueError:
            raise _RowError(f"el valor '{value}' no es un número válido") from None
    if math.isnan(number) or math.isinf(number):
        raise _RowError("el número no es válido")
    return number


def _parse_int(value: object, label: str) -> int:
    number = parse_number(value)
    if number is None:
        raise _RowError(f"falta el {label}")
    if not number.is_integer():
        raise _RowError(f"el {label} debe ser un número entero")
    return int(number)


def _parse_reported(value: object, has_values: bool) -> bool:
    text = _text(value).lower()
    if not text:
        return has_values
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise _RowError(f"el valor de 'reportado' ({value}) debe ser Sí o No")


def _validate(row: RawRow, catalog: Catalog, origin: Origin) -> Observation:
    site_code = _text(row.site).upper()
    indicator_code = _text(row.indicator).upper()
    if not site_code:
        raise _RowError("falta el código de sede")
    if not catalog.has_site(site_code):
        raise _RowError(f"la sede '{site_code}' no existe en el catálogo")
    if not indicator_code:
        raise _RowError("falta el código de indicador")
    if not catalog.has_indicator(indicator_code):
        raise _RowError(f"el indicador '{indicator_code}' no existe en el catálogo")
    year = _parse_int(row.year, "año")
    month = _parse_int(row.month, "mes")
    if not 1 <= month <= 12:
        raise _RowError("el mes debe estar entre 1 y 12")
    if year not in catalog.years:
        raise _RowError(f"el año {year} no tiene metas configuradas")
    if not catalog.is_active(indicator_code, year):
        raise _RowError(f"el indicador no está vigente en {year}")
    indicator = catalog.indicator(indicator_code)
    numerator = parse_number(row.numerator)
    denominator = parse_number(row.denominator)
    reported = _parse_reported(row.reported, numerator is not None or denominator is not None)
    if not reported:
        if numerator is not None or denominator is not None:
            raise _RowError("un mes no reportado no puede traer numerador ni denominador")
        return Observation(indicator_code, site_code, year, month, None, None, False, origin)
    if numerator is None:
        raise _RowError("falta el numerador")
    if numerator < 0 or (denominator is not None and denominator < 0):
        raise _RowError("el numerador y el denominador no pueden ser negativos")
    spec = input_spec(indicator)
    if not spec.denominator_enabled and denominator is not None:
        raise _RowError(spec.denominator_hint.rstrip("."))
    if spec.denominator_required:
        if denominator is None:
            raise _RowError("falta el denominador")
        if denominator == 0 and numerator > 0:
            raise _RowError("el denominador es cero y el numerador es positivo")
    if not catalog.is_applicable(indicator, year, site_code):
        raise _RowError("la sede no tiene meta asignada para este indicador en el año")
    if indicator.denominator_type is DenominatorType.MANUAL:
        origin = Origin.MANUAL
    return Observation(indicator_code, site_code, year, month, numerator, denominator, True, origin)


def validate_rows(rows: Iterable[RawRow], catalog: Catalog, origin: Origin = Origin.IMPORTED) -> ImportValidation:
    """Valida todas las filas; las duplicadas dentro del archivo se rechazan (se conserva la primera)."""
    accepted: list[Observation] = []
    rejected: list[RejectedRow] = []
    seen: dict[tuple[str, str, int, int], int] = {}
    total = 0
    for row in rows:
        total += 1
        try:
            obs = _validate(row, catalog, origin)
            if obs.key in seen:
                raise _RowError(f"repite sede, indicador y período de la fila {seen[obs.key]}")
        except (_RowError, ValidationError) as error:
            reason = str(error).rstrip(".")
            rejected.append(
                RejectedRow(
                    row_number=row.row_number,
                    site=_text(row.site),
                    indicator=_text(row.indicator),
                    period=f"{_text(row.month)}-{_text(row.year)}",
                    reason=reason[:1].upper() + reason[1:] + ".",
                )
            )
            continue
        seen[obs.key] = row.row_number
        accepted.append(obs)
    return ImportValidation(accepted=tuple(accepted), rejected=tuple(rejected), total_rows=total)
