"""Extracción de campos, reconstrucción de líneas y clasificación de páginas."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from receipt_reader.data.synthetic import ORGANIZATION_RUT, printed_rut
from receipt_reader.domain.classify import is_receipt_page, page_markers
from receipt_reader.domain.dates import Period
from receipt_reader.domain.extraction import extract_receipt, find_decree, looks_like_person_name
from receipt_reader.domain.layout import TextBox, build_lines, lines_from_text, mean_confidence
from receipt_reader.domain.models import Decree, WorkdayType
from receipt_reader.domain.text import fold
from support import ISSUER_RUT, OTHER_ISSUER_RUT, receipt_lines


def test_full_receipt_fields() -> None:
    result = extract_receipt(receipt_lines(), organization_rut=ORGANIZATION_RUT)
    assert result.value("issuer_rut") == ISSUER_RUT
    assert result.value("issuer_name") == "CAMILA ANDREA ROJAS SOTO"
    assert result.value("receiver_rut") == ORGANIZATION_RUT
    assert result.value("folio") == 245
    assert result.value("issue_date") == date(2025, 11, 3)
    assert result.value("gross") == 800_000
    assert result.value("retention") == 116_000
    assert result.value("net") == 684_000
    assert result.value("printed_rate_bp") == 1450
    assert result.value("service_period") == Period(2025, 10)
    assert result.value("hours") == Decimal(22)
    assert result.value("workday_type") is WorkdayType.WEEKLY
    assert result.value("decree") == Decree(3619, 2025)


def test_gross_label_with_colon_before_dollar_sign() -> None:
    # B02: la boleta imprime 'Total Honorarios: $: 800.000' (dos puntos antes y después del signo).
    result = extract_receipt(receipt_lines(gross_line="Total Honorarios: $: 1.250.000"))
    assert result.value("gross") == 1_250_000
    assert result.fields["gross"].raw == "1.250.000"


def test_amount_on_the_next_line() -> None:
    lines = receipt_lines(gross_line="Total Honorarios: $:\n800.000")
    assert extract_receipt(lines).value("gross") == 800_000


def test_unreadable_amount_stays_empty_with_raw_text() -> None:
    # Una O leída en lugar de un cero no se interpreta como 80: el bruto queda vacío y se conserva lo leído.
    misread = extract_receipt(receipt_lines(gross_line="Total Honorarios: $: 80O.000")).fields["gross"]
    assert misread.value is None
    assert misread.raw == "80O.000"
    lines = receipt_lines(gross_line="Total Honorarios: $: 45.000,50")
    ambiguous = extract_receipt(lines).fields["gross"]
    assert ambiguous.value is None
    assert ambiguous.raw == "45.000,50"


def test_cents_are_not_multiplied() -> None:
    result = extract_receipt(receipt_lines(gross_line="Total Honorarios: $: 800.000,00"))
    assert result.value("gross") == 800_000


def test_folio_is_anchored_to_header_and_not_the_phone() -> None:
    # B18: 'TELEFONO:' contiene 'NO' y el teléfono no debe tomarse como folio.
    result = extract_receipt(receipt_lines(folio_line="N °", phone_line="TELEFONO: 912345678"))
    assert result.value("folio") is None
    same_line = extract_receipt(receipt_lines(folio_line="", name="CAMILA ANDREA ROJAS SOTO ELECTRONICA N° 88"))
    assert same_line.value("folio") == 88


def test_folio_ignores_the_year() -> None:
    result = extract_receipt(receipt_lines(folio_line="N° 1520"))
    assert result.value("folio") == 1520


def test_receiver_rut_is_never_taken_as_issuer() -> None:
    # B20 y M03: el RUT del bloque 'Señor(es)' es el receptor.
    lines = receipt_lines(rut_line="")
    result = extract_receipt(lines, organization_rut=ORGANIZATION_RUT)
    assert result.value("issuer_rut") is None
    assert result.value("receiver_rut") == ORGANIZATION_RUT


def test_receiver_name_is_not_the_issuer_name() -> None:
    receiver = f"Señor(es): TOMAS IGNACIO REYES MUÑOZ Rut: {printed_rut(OTHER_ISSUER_RUT)}"
    result = extract_receipt(receipt_lines(receiver_line=receiver))
    assert result.value("issuer_name") == "CAMILA ANDREA ROJAS SOTO"
    assert result.value("receiver_name") == "TOMAS IGNACIO REYES MUÑOZ"
    assert result.value("receiver_rut") == OTHER_ISSUER_RUT


def test_rut_with_unicode_minus_in_native_text() -> None:
    result = extract_receipt(receipt_lines(rut_line="RUT: 41.234.567\u22123"))
    assert result.value("issuer_rut") == ISSUER_RUT


def test_name_filter_uses_whole_words() -> None:
    # B15: apellidos que contienen 'por', 'rut' o 'total' como subcadena siguen siendo válidos.
    assert looks_like_person_name("Juan Porras Soto")
    assert looks_like_person_name("Ana Ruturi Díaz")
    assert not looks_like_person_name("TOTAL HONORARIOS")
    assert not looks_like_person_name("BOLETA DE HONORARIOS")
    assert not looks_like_person_name("Solo")


def test_gloss_keeps_lines_and_excludes_amounts() -> None:
    # B14: la glosa es el bloque de la prestación, no el encabezado del documento.
    gloss = ("PRESTACIÓN DE SERVICIOS EN ATENCIÓN COMUNITARIA 800.000", "MES DE OCTUBRE - 22 HORAS SEMANALES")
    result = extract_receipt(receipt_lines(gloss=gloss))
    assert (
        result.value("gloss") == "PRESTACIÓN DE SERVICIOS EN ATENCIÓN COMUNITARIA\nMES DE OCTUBRE - 22 HORAS SEMANALES"
    )
    assert "RUT" not in result.value("gloss")


def test_service_period_year_inferred_from_issue_date() -> None:
    gloss = ("ATENCIÓN PROFESIONAL CORRESPONDIENTE AL MES DE DICIEMBRE, 22 HORAS SEMANALES",)
    lines = receipt_lines(gloss=gloss, date_line="Fecha: 05 de Enero de 2026")
    assert extract_receipt(lines).value("service_period") == Period(2025, 12)


def test_decree_date_is_not_the_service_period() -> None:
    gloss = ("SERVICIOS SEGÚN D.A. N° 2210 DE 12 DE ENERO DE 2026, 11 HRS. SEMANALES",)
    result = extract_receipt(receipt_lines(gloss=gloss, date_line="Fecha: 04 de Marzo de 2026"))
    assert result.value("service_period") is None
    assert result.value("decree") == Decree(2210, 2026)


def test_hours_with_decimals_and_workday() -> None:
    # B16: horas con coma decimal y jornada junto a las horas.
    gloss = ("APOYO FAMILIAR MES DE FEBRERO 2026, 12,5 HORAS SEMANALES",)
    result = extract_receipt(receipt_lines(gloss=gloss))
    assert result.value("hours") == Decimal("12.5")
    assert result.value("workday_type") is WorkdayType.WEEKLY
    monthly = extract_receipt(receipt_lines(gloss=("MES DE FEBRERO 2026, 64 HORAS MENSUALES",)))
    assert monthly.value("hours") == Decimal(64)
    assert monthly.value("workday_type") is WorkdayType.MONTHLY


def test_hours_do_not_match_loose_h_and_workday_stays_empty() -> None:
    # B16 y A14: 'CALLE 5 H' no son horas; sin jornada explícita no se asume 'semanal'.
    result = extract_receipt(receipt_lines(gloss=("SERVICIOS MES DE FEBRERO 2026 CALLE 5 H",)))
    assert result.value("hours") is None
    assert result.value("workday_type") is None


def test_hours_out_of_the_plausible_range_are_rejected() -> None:
    # B16: horas leídas fuera de un rango humano (por ejemplo un dígito de más) no se extraen.
    result = extract_receipt(receipt_lines(gloss=("SERVICIOS MES DE FEBRERO 2026, 999 HORAS SEMANALES",)))
    assert result.value("hours") is None


def test_decree_number_and_year() -> None:
    # B17: se reconoce 'N°', se guarda el año y el año no se toma como número.
    assert find_decree(fold("SEGÚN D.A. N° 1234 DEL 12.01.25")) == (Decree(1234, 2025), "D.A. N° 1234 DEL 12.01.25")
    assert find_decree(fold("DECRETO ALCALDICIO N° 3619/2025"))[0] == Decree(3619, 2025)
    assert find_decree(fold("DCTO. 845"))[0] == Decree(845, None)
    assert find_decree(fold("DECRETO 2025"))[0] is None


def test_printed_rate_with_decimal_point() -> None:
    result = extract_receipt(receipt_lines(retention_line="14.5 % Impto. Retenido: 116.000"))
    assert result.value("printed_rate_bp") == 1450


def test_emission_stamp_is_the_fallback_date() -> None:
    result = extract_receipt(receipt_lines(date_line=""))
    assert result.value("issue_date") == date(2025, 11, 3)


def test_build_lines_groups_by_vertical_overlap_and_orders_by_x() -> None:
    # B10: las cajas del OCR se agrupan en líneas; nunca una palabra por línea.
    boxes = [
        TextBox("Honorarios:", 60, 101, 120, 111, 0.9),
        TextBox("Total", 10, 100, 55, 110, 0.95),
        TextBox("800.000", 300, 99, 360, 110, 0.99),
        TextBox("Total:", 10, 130, 50, 140, 0.9),
        TextBox("684.000", 300, 131, 360, 141, 0.8),
    ]
    lines = build_lines(boxes)
    assert [line.text for line in lines] == ["Total Honorarios: 800.000", "Total: 684.000"]
    assert 0.8 < lines[1].confidence < 0.9
    assert mean_confidence(lines) is not None


def test_build_lines_joins_tight_fragments_without_space() -> None:
    boxes = [TextBox("41.234.567", 0, 0, 80, 10), TextBox("-3", 80.5, 0, 95, 10)]
    assert build_lines(boxes)[0].text == "41.234.567-3"


def test_lines_from_text_never_collapses_lines() -> None:
    # B13: el texto nativo conserva sus saltos de línea.
    lines = lines_from_text("uno\n\n  dos   tres \ncuatro")
    assert [line.text for line in lines] == ["uno", "dos tres", "cuatro"]


def test_page_classification() -> None:
    assert is_receipt_page(receipt_lines())
    assert page_markers(receipt_lines()) >= {"header", "gross", "retention", "service", "receiver"}
    annex = lines_from_text("INFORME MENSUAL\nSe adjunta la boleta de honorarios del período.\nFirma")
    assert not is_receipt_page(annex)
    only_mentions = lines_from_text("Total Honorarios del año\nObservaciones")
    assert not is_receipt_page(only_mentions)
