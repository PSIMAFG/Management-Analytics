"""Importación en formato largo: validación fila por fila, motivos de rechazo y lectura de Excel y CSV."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from kpi_monitor.data.excel_io import read_observation_file
from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.enums import Origin
from kpi_monitor.domain.importing import RawRow, _RowError, parse_number, validate_rows
from kpi_monitor.errors import DataError


def _row(number: int, *values: object) -> RawRow:
    site, indicator, year, month, numerator, denominator, *rest = values
    return RawRow(number, site, indicator, year, month, numerator, denominator, rest[0] if rest else None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12, 12.0),
        (3.5, 3.5),
        ("1.234", 1234.0),
        ("1.234,5", 1234.5),
        ("0,75", 0.75),
        (" 42 ", 42.0),
        ("", None),
        (None, None),
    ],
)
def test_parse_number_accepts_excel_and_chilean_text(raw: object, expected: float | None) -> None:
    """Los números se aceptan como valores de Excel o como texto con formato chileno (punto de miles y coma
    decimal)."""
    assert parse_number(raw) == expected


@pytest.mark.parametrize("raw", ["abc", True, "nan", "1,2,3"])
def test_parse_number_rejects_invalid_values(raw: object) -> None:
    """Los textos que no son números, los valores lógicos y los NaN se rechazan."""
    with pytest.raises(_RowError):
        parse_number(raw)


def test_valid_rows_are_accepted_with_origin(catalog: Catalog) -> None:
    """Las filas válidas se aceptan normalizando códigos y tipos, con origen importado salvo en los indicadores de
    denominador manual."""
    rows = [
        _row(2, "NOR", "CG02", 2026, 3, 120, 150),
        _row(3, "sur", "cg01", 2026.0, "3", "180", None),
        _row(4, "CEN", "CG05", 2026, 6, 300, 410),
        _row(5, "ORI", "CG16", 2026, 2, 3, 5),
    ]
    result = validate_rows(rows, catalog)
    assert result.is_clean
    assert result.total_rows == 4
    assert [o.key for o in result.accepted] == [
        ("CG02", "NOR", 2026, 3),
        ("CG01", "SUR", 2026, 3),
        ("CG05", "CEN", 2026, 6),
        ("CG16", "ORI", 2026, 2),
    ]
    assert result.accepted[0].origin is Origin.IMPORTED
    assert result.accepted[3].origin is Origin.MANUAL


def test_not_reported_month_is_kept_as_missing_not_zero(catalog: Catalog) -> None:
    """Regla: un mes marcado como no reportado se guarda como faltante, sin numerador ni denominador, nunca como
    cero."""
    result = validate_rows([_row(2, "NOR", "CG02", 2026, 3, None, None, "No")], catalog)
    obs = result.accepted[0]
    assert obs.reported is False
    assert obs.numerator is None
    assert obs.denominator is None


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        (("XYZ", "CG02", 2026, 3, 1, 2), "sede"),
        (("NOR", "ZZ01", 2026, 3, 1, 2), "indicador"),
        (("", "CG02", 2026, 3, 1, 2), "falta el código de sede"),
        (("NOR", "CG02", 2026, 13, 1, 2), "mes"),
        (("NOR", "CG02", 2019, 3, 1, 2), "no tiene metas"),
        (("NOR", "CG16", 2025, 3, 1, 2), "no está vigente"),
        (("NOR", "CG02", 2026, 3, -1, 2), "negativos"),
        (("NOR", "CG02", 2026, 3, None, 2), "falta el numerador"),
        (("NOR", "CG02", 2026, 3, 1, None), "falta el denominador"),
        (("NOR", "CG02", 2026, 3, 5, 0), "denominador es cero"),
        (("NOR", "CG01", 2026, 3, 5, 100), "meta fija"),
        (("SUR", "CG04", 2026, 3, 5, None), "no tiene meta asignada"),
        (("NOR", "IA01", 2026, 6, 5, 100), "población de referencia"),
        (("NOR", "CG02", 2026, 3, 1, 2, "No"), "no reportado"),
        (("NOR", "CG02", 2026, 3, 1, 2, "talvez"), "reportado"),
        (("NOR", "CG02", 2026, 3.5, 1, 2), "entero"),
        (("NOR", "CG02", 2026, 3, "muchos", 2), "no es un número"),
    ],
)
def test_invalid_rows_are_rejected_with_a_reason(catalog: Catalog, values: tuple, reason: str) -> None:
    """Regla: cada fila rechazada trae un motivo legible en español, con mayúscula inicial y punto final."""
    result = validate_rows([_row(7, *values)], catalog)
    assert not result.accepted
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.row_number == 7
    assert reason.lower() in rejected.reason.lower()
    assert rejected.reason[0].isupper()
    assert rejected.reason.endswith(".")


def test_duplicate_rows_keep_the_first_and_reject_the_rest(catalog: Catalog) -> None:
    """Regla: si un archivo repite sede, indicador y período se conserva la primera fila y se rechazan las demás,
    citando la fila original."""
    rows = [_row(2, "NOR", "CG02", 2026, 3, 1, 2), _row(3, "NOR", "CG02", 2026, 3, 5, 6)]
    result = validate_rows(rows, catalog)
    assert len(result.accepted) == 1
    assert result.accepted[0].numerator == 1
    assert "fila 2" in result.rejected[0].reason


def test_mixed_file_reports_every_rejected_row(catalog: Catalog) -> None:
    """Un archivo con filas válidas e inválidas informa cada fila rechazada con su número, sin detenerse en la
    primera."""
    rows = [
        _row(2, "NOR", "CG02", 2026, 3, 1, 2),
        _row(3, "XXX", "CG02", 2026, 3, 1, 2),
        _row(4, "NOR", "CG02", 2026, 4, 1, 2),
        _row(5, "NOR", "CG02", 2026, 0, 1, 2),
    ]
    result = validate_rows(rows, catalog)
    assert result.total_rows == 4
    assert [r.row_number for r in result.rejected] == [3, 5]
    assert len(result.accepted) == 2


def _write_xlsx(path: Path, header: list[str], rows: list[list[object]], sheet: str = "Observaciones") -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws.title = sheet
    ws.append(header)
    for row in rows:
        ws.append(row)
    workbook.save(path)
    return path


def test_read_xlsx_recognizes_headers_without_accents_and_skips_blank_rows(tmp_path: Path) -> None:
    """La lectura de Excel reconoce encabezados sin tildes, ignora columnas extra y omite filas en blanco
    conservando el número de fila."""
    path = _write_xlsx(
        tmp_path / "datos.xlsx",
        ["Sede", "Indicador", "Anio", "Mes", "Numerador", "Denominador", "Otra"],
        [
            ["NOR", "CG02", 2026, 3, 10, 12, "x"],
            [None, None, None, None, None, None, None],
            ["SUR", "CG02", 2026, 3, 8, 9],
        ],
    )
    rows = read_observation_file(path)
    assert [r.row_number for r in rows] == [2, 4]
    assert rows[0].year == 2026
    assert rows[1].site == "SUR"
    assert rows[0].reported is None


def test_read_csv_with_semicolons_and_chilean_decimals(tmp_path: Path, catalog: Catalog) -> None:
    """La lectura de CSV acepta punto y coma como separador y decimales con formato chileno."""
    path = tmp_path / "datos.csv"
    path.write_text("sede;indicador;año;mes;numerador;denominador;reportado\nNOR;CG06;2026;2;1.234,5;60;Sí\n", "utf-8")
    result = validate_rows(read_observation_file(path), catalog)
    assert result.is_clean
    assert result.accepted[0].numerator == 1234.5


def test_missing_required_columns_is_a_clear_error(tmp_path: Path) -> None:
    """Un archivo sin las columnas obligatorias da un error legible que las menciona."""
    path = _write_xlsx(tmp_path / "malo.xlsx", ["sede", "indicador", "mes"], [["NOR", "CG02", 3]])
    with pytest.raises(DataError, match="faltan columnas obligatorias"):
        read_observation_file(path)


@pytest.mark.parametrize("name", ["datos.txt", "no_existe.xlsx"])
def test_unsupported_or_missing_file_is_a_clear_error(tmp_path: Path, name: str) -> None:
    """Un formato no soportado o un archivo inexistente dan un error de datos legible."""
    with pytest.raises(DataError):
        read_observation_file(tmp_path / name)


def test_corrupt_excel_is_a_clear_error(tmp_path: Path) -> None:
    """Regla: un archivo .xlsx corrupto se traduce a un error de datos legible, no a una excepción técnica cruda."""
    path = tmp_path / "roto.xlsx"
    path.write_bytes(b"no es un libro de Excel")
    with pytest.raises(DataError, match="Excel válido"):
        read_observation_file(path)
