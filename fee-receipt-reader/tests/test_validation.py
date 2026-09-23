"""Validación con incidencias tipadas y derivación del estado."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from receipt_reader.data.synthetic import ORGANIZATION_RUT
from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import (
    FieldSource,
    FieldTrace,
    ReadStatus,
    ReceiptData,
    ReceiptStatus,
    ReferenceRate,
    Settings,
    Severity,
    WorkdayType,
)
from receipt_reader.domain.retention import LEGAL_RATES_BP, RetentionTable
from receipt_reader.domain.validation import (
    DuplicateInfo,
    IssueCode,
    ReceiptDraft,
    ValidationContext,
    acceptable_codes,
    acceptance_blockers,
    derive_status,
    validate_receipt,
)

SETTINGS = Settings(
    organization_rut=ORGANIZATION_RUT,
    organization_name="Organización Ejemplo",
    date_window_start=date(2025, 10, 1),
    date_window_end=date(2026, 7, 31),
)
CONTEXT = ValidationContext(
    settings=SETTINGS,
    retention=RetentionTable(LEGAL_RATES_BP),
    reference_rates={(1, 2025): ReferenceRate(1, 2025, 7_500, 8_800)},
    program_names={1: "Programa de Atención Comunitaria", 2: "Programa de Apoyo Familiar"},
)
GOOD = ReceiptData(
    issuer_rut="41234567-3",
    issuer_name="Camila Andrea Rojas Soto",
    receiver_rut=ORGANIZATION_RUT,
    folio=245,
    issue_date=date(2025, 11, 3),
    service_period=Period(2025, 10),
    payment_period=Period(2025, 11),
    gross=704_000,
    retention=102_080,
    net=601_920,
    printed_rate_bp=1450,
    program_id=1,
    hours=Decimal(22),
    workday_type=WorkdayType.WEEKLY,
)


def draft(data: ReceiptData = GOOD, **context: Any) -> ReceiptDraft:
    return replace(ReceiptDraft(data=data, folder_program_id=1, text_program_id=1), **context)


def codes(data: ReceiptData = GOOD, duplicates: DuplicateInfo | None = None, **context: Any) -> set[IssueCode]:
    return {issue.code for issue in validate_receipt(draft(data, **context), CONTEXT, duplicates)}


def status_of(data: ReceiptData, *, accept: bool = False, **context: Any) -> ReceiptStatus:
    """Estado tras validar; con `accept=True` simula que el usuario aprobó aceptando lo aceptable."""
    issues = validate_receipt(draft(data, **context), CONTEXT)
    accepted = acceptable_codes(issues) if accept else frozenset()
    return derive_status(issues, read_status=ReadStatus.OK, edited=False, accepted=accepted, discarded=False)


def test_complete_receipt_has_no_issues_and_is_approved() -> None:
    assert codes() == set()
    assert status_of(GOOD) is ReceiptStatus.APPROVED


def test_missing_gross_is_pending_and_never_invented() -> None:
    # B03 y M06: sin bruto leído la boleta queda pendiente; la validación no calcula montos.
    data = GOOD.with_changes(gross=None)
    assert IssueCode.GROSS_MISSING in codes(data)
    assert status_of(data) is ReceiptStatus.PENDING
    validate_receipt(draft(data), CONTEXT)
    assert data.gross is None


def test_triple_tolerance_of_one_peso() -> None:
    # B05: retención = redondeo(bruto x tasa) con tolerancia de 1 peso; líquido = bruto - retención.
    within = GOOD.with_changes(retention=102_081, net=601_919)
    assert IssueCode.RETENTION_MISMATCH not in codes(within)
    outside = GOOD.with_changes(retention=102_082, net=601_918)
    assert IssueCode.RETENTION_MISMATCH in codes(outside)
    wrong_net = GOOD.with_changes(net=651_920)
    assert IssueCode.NET_MISMATCH in codes(wrong_net)
    assert status_of(wrong_net) is ReceiptStatus.PENDING


def test_rate_of_another_year_is_blocking() -> None:
    # Tasa impresa de 2024 en una boleta emitida en 2025.
    data = GOOD.with_changes(printed_rate_bp=1375, retention=96_800, net=607_200)
    found = codes(data)
    assert {IssueCode.RATE_MISMATCH, IssueCode.RETENTION_MISMATCH} <= found
    assert status_of(data) is ReceiptStatus.PENDING


def test_rate_follows_the_issue_year() -> None:
    data = GOOD.with_changes(
        issue_date=date(2026, 3, 4),
        service_period=Period(2026, 2),
        payment_period=Period(2026, 3),
        printed_rate_bp=1525,
        retention=107_360,
        net=596_640,
    )
    assert codes(data) == set()


def test_derived_amount_is_flagged_as_warning() -> None:
    traces = {"net": FieldTrace("601920", None, FieldSource.DERIVED)}
    issues = validate_receipt(draft(GOOD, traces=traces), CONTEXT)
    derived = next(issue for issue in issues if issue.code is IssueCode.TRIPLE_DERIVED)
    assert derived.severity is Severity.WARNING


def test_only_gross_without_retention_or_net_is_blocking() -> None:
    data = GOOD.with_changes(retention=None, net=None)
    assert IssueCode.TRIPLE_INCOMPLETE in codes(data)


def test_issuer_rut_rules() -> None:
    assert IssueCode.ISSUER_RUT_MISSING in codes(GOOD.with_changes(issuer_rut=None))
    assert IssueCode.ISSUER_RUT_INVALID in codes(GOOD.with_changes(issuer_rut="41234567-4"))
    # B20: el emisor no puede ser la organización receptora.
    assert IssueCode.ISSUER_IS_RECEIVER in codes(GOOD.with_changes(issuer_rut=ORGANIZATION_RUT))


def test_other_receiver_is_only_a_warning() -> None:
    data = GOOD.with_changes(receiver_rut="42345678-5")
    issues = validate_receipt(draft(data), CONTEXT)
    mismatch = next(issue for issue in issues if issue.code is IssueCode.RECEIVER_MISMATCH)
    assert mismatch.severity is Severity.WARNING
    assert status_of(data) is ReceiptStatus.APPROVED


def test_folio_is_required() -> None:
    # A11: el folio es obligatorio para aprobar.
    assert IssueCode.FOLIO_MISSING in codes(GOOD.with_changes(folio=None))
    assert status_of(GOOD.with_changes(folio=None)) is ReceiptStatus.PENDING


def test_date_out_of_window_is_only_a_warning() -> None:
    # La ventana de fechas es un parámetro configurable: fuera de ella solo se advierte,
    # nunca bloquea la aprobación (el usuario puede ajustarla desde los parámetros).
    old = GOOD.with_changes(issue_date=date(2015, 3, 10), payment_period=None)
    issues = validate_receipt(draft(old), CONTEXT)
    window = next(issue for issue in issues if issue.code is IssueCode.DATE_OUT_OF_WINDOW)
    assert window.severity is Severity.WARNING
    assert not window.blocking
    assert status_of(old) is ReceiptStatus.APPROVED


def test_date_far_from_payment_folder_is_blocking_but_overridable() -> None:
    # B19: un mes de emisión muy distinto al de la carpeta manda a revisión, pero se puede aprobar.
    data = GOOD.with_changes(payment_period=Period(2026, 2))
    issues = validate_receipt(draft(data), CONTEXT)
    mismatch = next(issue for issue in issues if issue.code is IssueCode.DATE_FOLDER_MISMATCH)
    assert mismatch.blocking
    assert mismatch.overridable
    assert not acceptance_blockers([mismatch])
    assert status_of(data) is ReceiptStatus.PENDING
    assert status_of(data, accept=True) is ReceiptStatus.APPROVED


def test_service_after_issue_is_warning() -> None:
    data = GOOD.with_changes(service_period=Period(2025, 12))
    assert IssueCode.SERVICE_AFTER_ISSUE in codes(data)


def test_program_conflict_between_folder_and_text() -> None:
    # B22: carpeta y texto distintos se marcan para que una persona decida.
    found = codes(GOOD, text_program_id=2)
    assert IssueCode.PROGRAM_CONFLICT in found
    user_choice = {"program_id": FieldTrace("1", 1.0, FieldSource.USER)}
    assert IssueCode.PROGRAM_CONFLICT not in codes(GOOD, text_program_id=2, traces=user_choice)


def test_missing_program_and_unknown_folder_code() -> None:
    data = GOOD.with_changes(program_id=None)
    found = codes(data, folder_program_id=None, folder_program_code="550", text_program_id=None)
    assert {IssueCode.PROGRAM_MISSING, IssueCode.FOLDER_PROGRAM_UNKNOWN} <= found


def test_missing_issuer_name_is_only_a_warning() -> None:
    data = GOOD.with_changes(issuer_name=None)
    issues = validate_receipt(draft(data), CONTEXT)
    warning = next(issue for issue in issues if issue.code is IssueCode.ISSUER_NAME_MISSING)
    assert warning.severity is Severity.WARNING
    assert status_of(data) is ReceiptStatus.APPROVED


def test_missing_receiver_rut_is_only_a_warning() -> None:
    data = GOOD.with_changes(receiver_rut=None)
    issues = validate_receipt(draft(data), CONTEXT)
    warning = next(issue for issue in issues if issue.code is IssueCode.RECEIVER_RUT_MISSING)
    assert warning.severity is Severity.WARNING
    assert status_of(data) is ReceiptStatus.APPROVED


def test_missing_issue_date_is_blocking() -> None:
    data = GOOD.with_changes(issue_date=None)
    assert codes(data) == {IssueCode.DATE_MISSING}
    assert status_of(data) is ReceiptStatus.PENDING


def test_missing_service_period_is_blocking_but_overridable() -> None:
    # Sin período de servicio la boleta pasa por la cola (donde se ofrece la sugerencia);
    # el usuario puede aprobarla igual sabiendo que queda en "Sin período" en los informes.
    data = GOOD.with_changes(service_period=None)
    issues = validate_receipt(draft(data), CONTEXT)
    missing = next(issue for issue in issues if issue.code is IssueCode.SERVICE_PERIOD_MISSING)
    assert missing.blocking
    assert missing.overridable
    assert not acceptance_blockers([missing])
    assert status_of(data) is ReceiptStatus.PENDING
    assert status_of(data, accept=True) is ReceiptStatus.APPROVED


def test_missing_printed_rate_is_only_a_warning() -> None:
    data = GOOD.with_changes(printed_rate_bp=None)
    issues = validate_receipt(draft(data), CONTEXT)
    warning = next(issue for issue in issues if issue.code is IssueCode.RATE_MISSING)
    assert warning.severity is Severity.WARNING
    assert status_of(data) is ReceiptStatus.APPROVED


def test_year_without_a_legal_rate_is_only_a_warning() -> None:
    # A03: fuera de la tabla de tasas legales se advierte; no impide validar lo demás.
    data = GOOD.with_changes(issue_date=date(2019, 5, 4))
    issues = validate_receipt(draft(data), CONTEXT)
    warning = next(issue for issue in issues if issue.code is IssueCode.RATE_UNKNOWN_YEAR)
    assert warning.severity is Severity.WARNING


def test_amount_out_of_range_is_blocking_but_overridable() -> None:
    # A08: un monto fuera del rango configurado manda a revisión; nunca se descarta en silencio.
    data = GOOD.with_changes(gross=20_000, retention=2_900, net=17_100)
    issues = validate_receipt(draft(data), CONTEXT)
    out_of_range = next(issue for issue in issues if issue.code is IssueCode.AMOUNT_OUT_OF_RANGE)
    assert out_of_range.blocking
    assert out_of_range.overridable
    assert not acceptance_blockers([out_of_range])
    assert status_of(data) is ReceiptStatus.PENDING


def test_hourly_rate_out_of_reference_is_only_a_warning() -> None:
    # B03 y B04: el valor hora solo avisa; el monto no se corrige.
    half_month = GOOD.with_changes(gross=352_000, retention=51_040, net=300_960)
    issues = validate_receipt(draft(half_month), CONTEXT)
    hourly = next(issue for issue in issues if issue.code is IssueCode.HOURLY_RATE_OUT_OF_RANGE)
    assert hourly.severity is Severity.WARNING
    assert status_of(half_month) is ReceiptStatus.APPROVED
    assert half_month.gross == 352_000


def test_low_ocr_confidence_is_reported_without_floor() -> None:
    # M07: la confianza medida se usa tal cual (sin pisos artificiales).
    assert IssueCode.LOW_OCR_CONFIDENCE in codes(GOOD, ocr_confidence=0.6)
    assert IssueCode.LOW_OCR_CONFIDENCE not in codes(GOOD, ocr_confidence=0.95)


def test_duplicates_by_hash_and_by_issuer_folio() -> None:
    # B40: dos claves de duplicado, ambas bloqueantes y no aceptables.
    found = codes(
        GOOD, DuplicateInfo(original_file="2025-11/110 X/a.pdf", folio_peer_id=7, folio_peer_same_content=True)
    )
    assert {IssueCode.DUPLICATE_FILE, IssueCode.DUPLICATE_FOLIO} <= found
    issues = validate_receipt(draft(), CONTEXT, DuplicateInfo(folio_peer_id=7, folio_peer_same_content=False))
    assert acceptance_blockers(issues)
    assert "difieren" in issues[-1].message


def test_unreadable_and_no_receipt_are_errors() -> None:
    unreadable = ReceiptDraft(data=ReceiptData(), read_status=ReadStatus.UNREADABLE)
    issues = validate_receipt(unreadable, CONTEXT)
    assert [issue.code for issue in issues] == [IssueCode.UNREADABLE]
    status = derive_status(
        issues, read_status=ReadStatus.UNREADABLE, edited=False, accepted=frozenset(), discarded=False
    )
    assert status is ReceiptStatus.ERROR
    no_receipt = ReceiptDraft(data=ReceiptData(), read_status=ReadStatus.NO_RECEIPT)
    assert [issue.code for issue in validate_receipt(no_receipt, CONTEXT)] == [IssueCode.NOT_A_RECEIPT]


def test_status_derivation_with_user_actions() -> None:
    # El bug corregido: solo los códigos que el usuario aceptó al aprobar cuentan como
    # revisados. Una incidencia aceptable que no se aceptó deja la boleta pendiente.
    data = GOOD.with_changes(gross=20_000, retention=2_900, net=17_100)
    issues = validate_receipt(draft(data), CONTEXT)
    accepted = acceptable_codes(issues)
    ok = ReadStatus.OK
    empty: frozenset[IssueCode] = frozenset()
    assert derive_status(issues, read_status=ok, edited=False, accepted=empty, discarded=False) is ReceiptStatus.PENDING
    assert (
        derive_status(issues, read_status=ok, edited=False, accepted=accepted, discarded=False)
        is ReceiptStatus.APPROVED
    )
    assert (
        derive_status(issues, read_status=ok, edited=True, accepted=accepted, discarded=False)
        is ReceiptStatus.CORRECTED
    )
    assert (
        derive_status(issues, read_status=ok, edited=False, accepted=empty, discarded=True) is ReceiptStatus.DISCARDED
    )
    missing = validate_receipt(draft(GOOD.with_changes(gross=None)), CONTEXT)
    # GROSS_MISSING no es aceptable: aunque se acepte todo lo que había, sigue pendiente.
    still_pending = derive_status(missing, read_status=ok, edited=False, accepted=accepted, discarded=False)
    assert still_pending is ReceiptStatus.PENDING


def test_edited_unreadable_receipt_is_validated_normally() -> None:
    edited = ReceiptDraft(data=GOOD, read_status=ReadStatus.UNREADABLE, folder_program_id=1, edited=True)
    issues = validate_receipt(edited, CONTEXT)
    status = derive_status(
        issues, read_status=ReadStatus.UNREADABLE, edited=True, accepted=frozenset(), discarded=False
    )
    assert status is ReceiptStatus.CORRECTED
