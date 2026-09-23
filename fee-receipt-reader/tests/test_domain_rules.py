"""Reglas del dominio: ubicación, programas, retención, formularios, sugerencias e informes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from receipt_reader.domain.dates import Period
from receipt_reader.domain.forms import form_text, parse_field, parse_receipt_form
from receipt_reader.domain.location import parse_location
from receipt_reader.domain.models import (
    Decree,
    FieldSource,
    PeriodAxis,
    ProgramAlias,
    ReceiptData,
    Settings,
    WorkdayType,
)
from receipt_reader.domain.programs import ProgramMatcher, TextProgramMatch, habitual_program, resolve_program
from receipt_reader.domain.reporting import period_for_axis, safe_filename, sanitize_sheet_name, unique_sheet_names
from receipt_reader.domain.retention import (
    LEGAL_RATES_BP,
    RetentionTable,
    complete_triple,
    expected_retention,
    implied_hourly_rate,
)
from receipt_reader.domain.suggestions import HabitualProgram, build_suggestions
from receipt_reader.errors import FieldFormatError, FormError

ALIASES = (
    ProgramAlias(1, "Programa de Atención Comunitaria", 10),
    ProgramAlias(1, "PAC", 40),
    ProgramAlias(2, "Programa de Apoyo Familiar", 10),
    ProgramAlias(2, "PAF", 40),
)


# Ubicación del archivo


def test_location_follows_the_folder_convention() -> None:
    hints = parse_location(("2025-11", "110 Atención Comunitaria", "Camila Rojas B 245 MES OCTUBRE.pdf"))
    assert hints.payment_period == Period(2025, 11)
    assert hints.program_code == "110"
    assert hints.folio_hint == 245
    assert hints.service_month_hint == 10
    # B15: el nombre sugerido desde el archivo no arrastra 'B', 'MES' ni el mes.
    assert hints.name_hint == "Camila Rojas"


def test_location_without_convention_is_empty() -> None:
    hints = parse_location(("boletas varias", "escaneo.pdf"))
    assert hints.payment_period is None
    assert hints.program_code is None
    assert hints.folio_hint is None
    assert hints.name_hint is None


# Programas


def test_program_text_match_uses_whole_words_and_priority() -> None:
    matcher = ProgramMatcher(ALIASES)
    assert matcher.match("SERVICIOS EN PROGRAMA DE ATENCIÓN COMUNITARIA").program_id == 1
    # 'PAC' dentro de otra palabra no cuenta (B22).
    assert matcher.match("IMPACTO DEL TRABAJO").program_id is None
    assert matcher.match("PROGRAMA DE APOYO FAMILIAR (PAC)").program_id == 2
    ambiguous = matcher.match("PAC y PAF")
    assert ambiguous.program_id is None
    assert ambiguous.ambiguous
    assert ambiguous.candidates == (1, 2)


def test_folder_has_precedence_and_conflict_is_reported() -> None:
    resolution = resolve_program(1, TextProgramMatch(2, "PAF"), FieldSource.NATIVE)
    assert resolution.program_id == 1
    assert resolution.source is FieldSource.FOLDER
    assert resolution.conflict
    from_text = resolve_program(None, TextProgramMatch(2, "PAF"), FieldSource.OCR)
    assert (from_text.program_id, from_text.source, from_text.conflict) == (2, FieldSource.OCR, False)
    assert resolve_program(None, TextProgramMatch(None), FieldSource.OCR).program_id is None


def test_habitual_program_counts_real_occurrences() -> None:
    # M05: el programa habitual es el más frecuente, no el primero visto; un empate no sugiere nada.
    assert habitual_program([2, 1, 1, 3]) == (1, 2)
    assert habitual_program([1, 2]) is None
    assert habitual_program([]) is None


# Retención


def test_legal_rate_by_year_and_after_the_table() -> None:
    table = RetentionTable(LEGAL_RATES_BP)
    assert table.rate_for(2024) == 1375
    assert table.rate_for(2025) == 1450
    assert table.rate_for(2026) == 1525
    assert table.rate_for(2031) == 1700
    assert table.rate_for(2019) is None


def test_expected_retention_rounds_half_up() -> None:
    assert expected_retention(800_000, 1450) == 116_000
    assert expected_retention(100_001, 1450) == 14_500
    assert expected_retention(100_003, 1450) == 14_500
    assert expected_retention(100_004, 1450) == 14_501


def test_complete_triple_never_derives_the_gross() -> None:
    assert complete_triple(800_000, None, 684_000).retention == 116_000
    assert complete_triple(800_000, 116_000, None).net == 684_000
    assert complete_triple(800_000, 116_000, None).derived == ("net",)
    no_gross = complete_triple(None, 116_000, 684_000)
    assert no_gross.gross is None
    assert no_gross.derived == ()


def test_implied_hourly_rate() -> None:
    assert implied_hourly_rate(704_000, Decimal(22), 4) == 8_000
    assert implied_hourly_rate(512_000, Decimal(64), 1) == 8_000
    assert implied_hourly_rate(None, Decimal(22), 4) is None
    assert implied_hourly_rate(704_000, None, 4) is None


# Formulario de revisión


def test_form_parsers_validate_each_field() -> None:
    assert parse_field("issuer_rut", "41.234.567-3") == "41234567-3"
    assert parse_field("gross", "$ 800.000") == 800_000
    assert parse_field("issue_date", "03-11-2025") == date(2025, 11, 3)
    assert parse_field("hours", "12,5") == Decimal("12.5")
    assert parse_field("service_period", "2025-10") == Period(2025, 10)
    assert parse_field("decree", "3619/2025") == Decree(3619, 2025)
    assert parse_field("workday_type", "Semanal") is WorkdayType.WEEKLY
    assert parse_field("printed_rate_bp", "14,50") == 1450
    assert parse_field("folio", "1.245") == 1245
    assert parse_field("gross", "  ") is None


def test_form_reports_all_errors_together() -> None:
    # B09: nada se guarda si algún campo no es válido y se informan todos los errores.
    with pytest.raises(FormError) as caught:
        parse_receipt_form({"issuer_rut": "41.234.567-4", "gross": "12,5", "hours": "300", "folio": "abc"})
    assert set(caught.value.field_errors) == {"issuer_rut", "gross", "hours", "folio"}
    with pytest.raises(FieldFormatError):
        parse_field("status", "approved")


def test_form_text_is_the_inverse_of_parse() -> None:
    values = {
        "issuer_rut": "41234567-3",
        "gross": 1_234_567,
        "issue_date": date(2026, 3, 4),
        "hours": Decimal("12.5"),
        "service_period": Period(2026, 2),
        "decree": Decree(1234, 2026),
        "printed_rate_bp": 1525,
        "workday_type": WorkdayType.MONTHLY,
    }
    for name, value in values.items():
        assert parse_field(name, form_text(name, value)) == value


# Sugerencias


def test_suggestions_are_proposals_not_changes() -> None:
    # B23 y B34: las sugerencias se muestran; los datos no cambian hasta que el usuario las aplique.
    data = ReceiptData(issuer_rut="41234567-3", issuer_name="CAMILA ROJAS", issue_date=date(2026, 3, 4), net=610_000)
    suggestions = build_suggestions(
        data,
        canonical_name="Camila Andrea Rojas Soto",
        habitual=HabitualProgram(1, 5, 6),
        program_names={1: "Programa de Atención Comunitaria"},
        folio_hint=245,
    )
    by_field = {item.field: item for item in suggestions}
    assert by_field["issuer_name"].value == "Camila Andrea Rojas Soto"
    assert by_field["program_id"].value == 1
    assert by_field["folio"].value == 245
    assert by_field["service_period"].value == Period(2026, 2)
    assert "gross" not in by_field  # sin retención impresa no se propone ningún bruto
    assert data.program_id is None
    assert data.folio is None


def test_gross_suggestion_only_from_printed_net_and_retention() -> None:
    data = ReceiptData(net=684_000, retention=116_000)
    suggestion = next(item for item in build_suggestions(data) if item.field == "gross")
    assert suggestion.value == 800_000
    assert suggestion.text == "800.000"


# Informes


def test_sheet_names_are_unique_case_insensitive_and_valid() -> None:
    # B21: nombres de hoja derivados del catálogo, sin duplicados ni caracteres prohibidos.
    names = unique_sheet_names(["Apoyo Familiar", "APOYO FAMILIAR", "Base", "a/b:c"], reserved=("Base",))
    assert names == ["Apoyo Familiar", "APOYO FAMILIAR (2)", "Base (2)", "a b c"]
    assert len(sanitize_sheet_name("x" * 40)) == 31


def test_safe_filename() -> None:
    assert safe_filename("41234567-3_Camila Muñoz Peña") == "41234567-3_Camila_Munoz_Pena"


def test_period_for_axis() -> None:
    data = ReceiptData(issue_date=date(2026, 3, 4), service_period=Period(2026, 2), payment_period=Period(2026, 4))
    assert period_for_axis(data, PeriodAxis.SERVICE) == Period(2026, 2)
    assert period_for_axis(data, PeriodAxis.ISSUE) == Period(2026, 3)
    assert period_for_axis(data, PeriodAxis.PAYMENT) == Period(2026, 4)


def test_default_date_window_is_wide() -> None:
    # A18: la ventana por defecto no debe dejar boletas antiguas ni recientes fuera sin querer.
    settings = Settings()
    assert settings.date_window_start == date(2018, 1, 1)
    assert settings.date_window_end >= date.today()
    assert (settings.date_window_end - date.today()).days >= 30
