"""Revisión manual sobre la base de ejemplo: correcciones auditadas, aprobación, descarte y cola."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from receipt_reader.data.synthetic import build_demo_lot
from receipt_reader.domain.forms import form_text
from receipt_reader.domain.models import VALID_STATUSES, FieldSource, ReceiptStatus
from receipt_reader.domain.reporting import ReceiptFilter
from receipt_reader.errors import FieldFormatError, FormError, ValidationError
from receipt_reader.services.reports import ReportService
from receipt_reader.services.review import ReviewService
from support import DemoEnvironment, receipt_id_by_tag


def test_demo_database_has_every_review_case(demo_env: DemoEnvironment) -> None:
    result = demo_env.result
    assert result.batch.files_processed == result.batch.files_found == len(build_demo_lot().documents)
    assert (result.corrected, result.discarded, result.sample_files) == (1, 1, 12)
    rows = {row.id: row for row in ReportService(demo_env.db_path).rows()}
    expected = {
        "liquido_mal_leido": (ReceiptStatus.PENDING, "NET_MISMATCH"),
        "bruto_ilegible": (ReceiptStatus.PENDING, "GROSS_MISSING"),
        "tasa_otro_anio": (ReceiptStatus.PENDING, "RATE_MISMATCH"),
        "programa_en_conflicto": (ReceiptStatus.PENDING, "PROGRAM_CONFLICT"),
        "folio_repetido": (ReceiptStatus.PENDING, "DUPLICATE_FOLIO"),
        # La ventana de fechas fuera de rango solo advierte: no bloquea la aprobación.
        "fecha_fuera_de_ventana": (ReceiptStatus.APPROVED, "DATE_OUT_OF_WINDOW"),
        "receptor_distinto": (ReceiptStatus.APPROVED, "RECEIVER_MISMATCH"),
        "carpeta_desconocida": (ReceiptStatus.APPROVED, "FOLDER_PROGRAM_UNKNOWN"),
        # Confianza baja en todos los campos clave: cada uno manda a revisión por separado
        # (además del aviso de confianza media de la página).
        "baja_confianza": (ReceiptStatus.PENDING, "FIELD_LOW_CONFIDENCE"),
        "archivo_danado": (ReceiptStatus.ERROR, "UNREADABLE"),
        "sin_boleta": (ReceiptStatus.ERROR, "NOT_A_RECEIPT"),
        "copia_exacta": (ReceiptStatus.DISCARDED, "DUPLICATE_FILE"),
        "folio_ilegible": (ReceiptStatus.CORRECTED, None),
    }
    for tag, (status, code) in expected.items():
        row = rows[receipt_id_by_tag(demo_env.db_path, tag)]
        assert row.status is status, tag
        if code is not None:
            assert code in row.issue_codes, tag


def test_demo_status_counts_are_coherent_with_the_seeded_lot(demo_env: DemoEnvironment) -> None:
    """Coherencia de los datos sembrados: los estados cubren todo el lote sin perder ninguna boleta."""
    rows = ReportService(demo_env.db_path).rows()
    counts = Counter(row.status for row in rows)
    assert sum(counts.values()) == len(rows)
    # Casos deliberados de la demo: exactamente una corregida y una descartada (ver los tags arriba).
    assert counts[ReceiptStatus.CORRECTED] == 1
    assert counts[ReceiptStatus.DISCARDED] == 1
    assert counts[ReceiptStatus.PENDING] > 0
    assert counts[ReceiptStatus.ERROR] > 0
    assert counts[ReceiptStatus.APPROVED] > counts[ReceiptStatus.PENDING] + counts[ReceiptStatus.ERROR]
    # B33: la cola de revisión es exactamente pendientes más errores, sin depender de otro contador.
    queue_ids = {row.id for row in ReviewService(demo_env.db_path).queue()}
    assert queue_ids == {row.id for row in rows if row.status in (ReceiptStatus.PENDING, ReceiptStatus.ERROR)}
    valid_ids = {row.id for row in rows if row.status in VALID_STATUSES}
    assert valid_ids == {row.id for row in rows if row.status in (ReceiptStatus.APPROVED, ReceiptStatus.CORRECTED)}


def test_demo_correction_is_audited(demo_env: DemoEnvironment) -> None:
    receipt_id = receipt_id_by_tag(demo_env.db_path, "folio_ilegible")
    detail = ReviewService(demo_env.db_path).detail(receipt_id)
    assert detail.record.status is ReceiptStatus.CORRECTED
    assert detail.record.traces["folio"].source is FieldSource.USER
    assert [(c.field, c.old_value) for c in detail.corrections] == [("folio", None)]


def test_correction_changes_only_the_edited_receipt(review: ReviewService, demo_db: Path) -> None:
    # B08: guardar una corrección nunca propaga montos a otras boletas del mismo prestador.
    receipt_id = receipt_id_by_tag(demo_db, "bruto_ilegible")
    before = review.detail(receipt_id)
    issuer = before.record.data.issuer_rut
    others_before = {row.id: row for row in ReportService(demo_db).rows() if row.issuer_rut == issuer}
    suggestion = next(item for item in before.suggestions if item.field == "gross")
    assert before.record.data.gross is None  # la sugerencia no se aplicó sola

    after = review.save_correction(receipt_id, {"gross": suggestion.text})

    assert after.record.status is ReceiptStatus.CORRECTED
    assert after.record.data.gross == suggestion.value
    assert after.record.traces["gross"].source is FieldSource.USER
    assert [(c.field, c.old_value, c.new_value) for c in after.corrections] == [("gross", None, str(suggestion.value))]
    others_after = {row.id: row for row in ReportService(demo_db).rows() if row.issuer_rut == issuer}
    for other_id, row in others_before.items():
        if other_id != receipt_id:
            assert others_after[other_id] == row


def test_invalid_correction_changes_nothing(review: ReviewService, demo_db: Path) -> None:
    receipt_id = receipt_id_by_tag(demo_db, "liquido_mal_leido")
    before = review.detail(receipt_id).record
    with pytest.raises(FormError) as caught:
        review.save_correction(receipt_id, {"net": "12,5", "issuer_rut": "41.234.567-4"})
    assert set(caught.value.field_errors) == {"net", "issuer_rut"}
    with pytest.raises(ValidationError):
        review.save_correction(receipt_id, {"status": "approved"})
    assert review.detail(receipt_id).record == before


def test_fixing_the_net_amount_resolves_the_triple(review: ReviewService, demo_db: Path) -> None:
    receipt_id = receipt_id_by_tag(demo_db, "liquido_mal_leido")
    data = review.detail(receipt_id).record.data
    assert data.gross is not None
    assert data.retention is not None
    detail = review.save_correction(receipt_id, {"net": form_text("net", data.gross - data.retention)})
    assert detail.record.status is ReceiptStatus.CORRECTED
    assert not detail.record.blocking_issues


def test_approve_accepts_overridable_issues(review: ReviewService, demo_db: Path) -> None:
    for tag in ("programa_en_conflicto",):
        receipt_id = receipt_id_by_tag(demo_db, tag)
        assert review.detail(receipt_id).can_approve
        detail = review.approve(receipt_id)
        assert detail.record.status is ReceiptStatus.APPROVED
        assert detail.record.accepted_at is not None
        assert detail.corrections[-1].field == "status"
        with pytest.raises(ValidationError, match="ya está aprobada"):
            review.approve(receipt_id)


def test_approve_rejects_issues_that_need_a_correction(review: ReviewService, demo_db: Path) -> None:
    for tag in ("liquido_mal_leido", "tasa_otro_anio", "folio_repetido", "archivo_danado"):
        receipt_id = receipt_id_by_tag(demo_db, tag)
        assert not review.detail(receipt_id).can_approve
        with pytest.raises(ValidationError):
            review.approve(receipt_id)
        assert review.detail(receipt_id).record.status is not ReceiptStatus.APPROVED


def test_discard_keeps_the_receipt_and_restore_revalidates(review: ReviewService, demo_db: Path) -> None:
    # B07 y M06: descartar exige motivo y la boleta sigue registrada.
    receipt_id = receipt_id_by_tag(demo_db, "folio_repetido")
    with pytest.raises(ValidationError, match="motivo"):
        review.discard(receipt_id, " ")
    detail = review.discard(receipt_id, "Reenvío de una boleta ya pagada")
    assert detail.record.status is ReceiptStatus.DISCARDED
    assert detail.record.discard_reason == "Reenvío de una boleta ya pagada"
    assert receipt_id in {row.id for row in ReportService(demo_db).rows()}
    assert receipt_id not in {row.id for row in review.queue()}
    with pytest.raises(ValidationError):
        review.save_correction(receipt_id, {"folio": "1"})
    restored = review.restore(receipt_id)
    assert restored.record.status is ReceiptStatus.PENDING
    assert "DUPLICATE_FOLIO" in {issue.code.value for issue in restored.record.issues}


def test_unreadable_receipt_can_be_completed_by_hand(review: ReviewService, demo_db: Path) -> None:
    document = next(doc for doc in build_demo_lot().documents if "archivo_danado" in doc.tags)
    truth = document.receipt
    assert truth is not None
    receipt_id = receipt_id_by_tag(demo_db, "archivo_danado")
    values = {
        "issuer_rut": form_text("issuer_rut", truth.provider.rut),
        "issuer_name": truth.provider.name,
        "receiver_rut": form_text("receiver_rut", truth.receiver_rut),
        "folio": str(truth.folio),
        "issue_date": form_text("issue_date", truth.issue_date),
        "service_period": form_text("service_period", truth.service_period),
        "gross": form_text("gross", truth.gross),
        "retention": form_text("retention", truth.retention),
        "net": form_text("net", truth.net),
        "printed_rate_bp": form_text("printed_rate_bp", truth.rate_bp),
    }
    original = next(
        row
        for row in ReportService(demo_db).rows()
        if row.issuer_rut == truth.provider.rut and row.folio == truth.folio
    )
    assert original.status is ReceiptStatus.APPROVED
    detail = review.save_correction(receipt_id, values)
    # El original legible de este archivo ya está aprobado: la boleta completada a mano queda como
    # duplicado y el original sigue aprobado (corregir una boleta nunca deja pendiente a otra).
    codes = {issue.code.value for issue in detail.record.issues}
    assert "DUPLICATE_FOLIO" in codes
    assert "UNREADABLE" not in codes
    assert detail.record.status is ReceiptStatus.PENDING
    assert len(detail.corrections) == len(values)
    assert review.detail(original.id).record.status is ReceiptStatus.APPROVED


def test_queue_filters_by_issue(review: ReviewService, demo_db: Path) -> None:
    queue = review.queue()
    assert queue
    assert all(row.status in (ReceiptStatus.PENDING, ReceiptStatus.ERROR) for row in queue)
    net = review.queue(ReceiptFilter(issue_code="NET_MISMATCH"))
    assert [row.id for row in net] == [receipt_id_by_tag(demo_db, "liquido_mal_leido")]
    options = {option.code: option.count for option in review.issue_options()}
    assert options["NET_MISMATCH"] == 1
    assert "RECEIVER_MISMATCH" not in options  # solo cuenta boletas que esperan revisión


def test_page_preview_of_the_exact_page(review: ReviewService, demo_db: Path) -> None:
    for tag in ("tasa_otro_anio", "liquido_mal_leido"):
        png = review.page_image(receipt_id_by_tag(demo_db, tag), dpi=40)
        assert png.startswith(b"\x89PNG")


def test_confirm_provider_name_validates_the_rut(review: ReviewService) -> None:
    with pytest.raises(FieldFormatError):
        review.confirm_provider_name("41.234.567-4", "Camila Rojas")
    with pytest.raises(ValidationError):
        review.confirm_provider_name("41.234.567-3", " ")
    provider = review.confirm_provider_name("41.234.567-3", "  Camila   Rojas ")
    assert (provider.rut, provider.canonical_name) == ("41234567-3", "Camila Rojas")
