"""Parsers deterministas: RUT, montos en formato chileno, números decimales, fechas y períodos."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from receipt_reader.domain.dates import Period, find_long_date, find_numeric_date, infer_year, parse_date
from receipt_reader.domain.money import parse_clp, parse_decimal, parse_percent_bp, round_half_up, try_parse_clp
from receipt_reader.domain.rut import (
    compute_dv,
    find_ruts,
    format_rut,
    is_valid_rut,
    make_rut,
    normalize_rut,
    parse_rut,
)
from receipt_reader.domain.text import UNICODE_DASHES, fold, month_from_name, normalize_text
from receipt_reader.errors import FieldFormatError

# RUT


def test_dv_module_11_including_k_and_zero() -> None:
    assert compute_dv(41_234_567) == "3"
    bodies_by_dv = {compute_dv(body): body for body in range(40_000_000, 40_000_200)}
    assert "K" in bodies_by_dv
    assert "0" in bodies_by_dv
    for dv, body in bodies_by_dv.items():
        assert is_valid_rut(f"{body}-{dv}")


def test_rut_with_dots_and_unicode_minus_is_canonical() -> None:
    # M01: la boleta electrónica imprime el guion del RUT como signo menos Unicode.
    assert normalize_rut("41.234.567\u22123") == "41234567-3"
    assert parse_rut(" 41.234.567 - 3 ") == "41234567-3"
    for dash in UNICODE_DASHES:
        assert parse_rut(f"41.234.567{dash}3") == "41234567-3"


def test_rut_lowercase_k_is_uppercased() -> None:
    body = next(b for b in range(40_000_000, 40_001_000) if compute_dv(b) == "K")
    assert parse_rut(f"{body}-k") == f"{body}-K"


def test_rut_with_wrong_dv_is_rejected() -> None:
    assert not is_valid_rut("41.234.567-4")
    with pytest.raises(FieldFormatError, match="dígito verificador"):
        parse_rut("41.234.567-4")


def test_rut_without_shape_is_rejected() -> None:
    assert normalize_rut("hola") is None
    assert not is_valid_rut(None)
    with pytest.raises(FieldFormatError):
        parse_rut("12345")


def test_format_rut_adds_dots_only_for_display() -> None:
    assert format_rut("41234567-3") == "41.234.567-3"
    assert format_rut(None) == ""


def test_find_ruts_reports_position_and_validity() -> None:
    text = "RUT: 41.234.567\u22123 y otro 42.345.678-0"
    found = find_ruts(text)
    assert [match.rut for match in found] == ["41234567-3", "42345678-0"]
    assert found[0].valid
    assert found[1].valid is (compute_dv(42_345_678) == "0")
    assert text[found[0].start : found[0].end].startswith("41.234.567")


def test_make_rut_round_trip() -> None:
    rut = make_rut(45_210_804)
    assert is_valid_rut(rut)
    assert parse_rut(format_rut(rut)) == rut


# Montos


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.234.567", 1_234_567),
        ("1.234.567,00", 1_234_567),
        ("45.000,00", 45_000),
        ("$ 500.000", 500_000),
        ("$: 500.000", 500_000),
        ("500000", 500_000),
        ("800", 800),
    ],
)
def test_parse_clp_accepts_chilean_format(text: str, expected: int) -> None:
    assert parse_clp(text) == expected


@pytest.mark.parametrize("text", ["12,5", "12.5", "1,234", "1.23.456", "45.000,50", "", "abc", "1.2345"])
def test_parse_clp_rejects_ambiguous_values(text: str) -> None:
    # B01: nunca se adivina; '45.000,00' no se multiplica por 100 y '12,5' no es 125.
    with pytest.raises(FieldFormatError):
        parse_clp(text)
    assert try_parse_clp(text) is None


def test_parse_decimal_hours_and_rates() -> None:
    assert parse_decimal("12,5") == Decimal("12.5")
    assert parse_decimal("14.50") == Decimal("14.50")
    assert parse_decimal("22") == Decimal(22)
    with pytest.raises(FieldFormatError):
        parse_decimal("1.234")


def test_parse_percent_bp() -> None:
    assert parse_percent_bp("14.50 %") == 1450
    assert parse_percent_bp("14,5") == 1450
    assert parse_percent_bp("15.25%") == 1525


def test_round_half_up_is_explicit() -> None:
    assert round_half_up(Decimal("0.5")) == 1
    assert round_half_up(Decimal("2.5")) == 3
    assert round_half_up(Decimal("2.49")) == 2


# Fechas y períodos


def test_long_date_in_receipt_format() -> None:
    assert find_long_date("Fecha: 03 de Noviembre de 2025") == date(2025, 11, 3)
    assert find_long_date("Fecha: 1 de setiembre de 2025") == date(2025, 9, 1)
    assert find_long_date("Fecha: 31 de febrero de 2025") is None


def test_numeric_date_is_always_day_month() -> None:
    # B19: '04/11/2025' es 4 de noviembre; nunca 11 de abril.
    assert find_numeric_date("Emisión: 04/11/2025 12:00") == date(2025, 11, 4)
    assert parse_date("04-11-2025") == date(2025, 11, 4)
    assert parse_date("2025-11-04") == date(2025, 11, 4)
    assert parse_date("4 de noviembre de 2025") == date(2025, 11, 4)


def test_impossible_day_month_is_rejected_not_reinterpreted() -> None:
    with pytest.raises(FieldFormatError):
        parse_date("13/25/2025")
    with pytest.raises(FieldFormatError):
        parse_date("11/13/2025")
    assert find_numeric_date("11/13/2025") is None


def test_period_parsing_and_arithmetic() -> None:
    assert Period.parse("2025-10") == Period(2025, 10)
    assert Period.parse("10/2025") == Period(2025, 10)
    assert Period.parse("octubre 2025") == Period(2025, 10)
    assert Period(2025, 12).shift(1) == Period(2026, 1)
    assert Period(2026, 1).shift(-1) == Period(2025, 12)
    assert Period(2025, 11).months_until(Period(2026, 2)) == 3
    assert Period(2024, 2).last_day() == date(2024, 2, 29)
    assert Period(2025, 10).label() == "octubre 2025"
    with pytest.raises(FieldFormatError):
        Period(2025, 13)
    with pytest.raises(FieldFormatError):
        Period.parse("2025")


def test_infer_year_for_service_month() -> None:
    # M04: diciembre facturado en enero corresponde al año anterior.
    assert infer_year(12, date(2026, 1, 5)) == 2025
    assert infer_year(1, date(2026, 1, 5)) == 2026
    assert infer_year(10, date(2025, 11, 5)) == 2025


# Texto


def test_normalize_text_replaces_unicode_dashes_and_spaces() -> None:
    raw = "41.234.567\u22123\u00a0y\u2013fin"
    assert normalize_text(raw) == "41.234.567-3 y-fin"


def test_fold_keeps_length_and_removes_accents() -> None:
    text = "Señor(es): atención"
    folded = fold(text)
    assert folded == "SENOR(ES): ATENCION"
    assert len(folded) == len(text)


def test_month_from_name() -> None:
    assert month_from_name("Octubre") == 10
    assert month_from_name("SETIEMBRE") == 9
    assert month_from_name("lunes") is None
