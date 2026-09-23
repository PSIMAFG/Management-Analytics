"""Interfaz: lógica de presentación y pruebas de humo de la ventana (sin pantalla, QT_QPA_PLATFORM=offscreen)."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from matplotlib.figure import Figure
from PySide6.QtCore import QSortFilterProxyModel
from PySide6.QtWidgets import QApplication, QHeaderView, QTableView, QTableWidget

from receipt_reader.data.ocr import SyntheticOcrEngine
from receipt_reader.data.synthetic_render import write_documents
from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import FieldSource, FieldTrace, PeriodAxis, Program, ReceiptStatus
from receipt_reader.domain.records import Correction, RejectedRow
from receipt_reader.domain.reporting import ReceiptFilter, Totals
from receipt_reader.domain.retention import RetentionTable
from receipt_reader.paths import AppPaths
from receipt_reader.services.app_services import AppServices
from receipt_reader.services.catalog import CatalogService
from receipt_reader.services.reports import Overview, ReportService
from receipt_reader.ui.capture import capture_tabs
from receipt_reader.ui.charts import (
    HOURLY_OUT_OF_RANGE,
    draw_monthly_gross,
    fill_month_gaps,
    hourly_groups,
    jitter_offsets,
    ordered_series_names,
    period_tick_labels,
    program_styles,
    status_counts_in_order,
    tick_step,
)
from receipt_reader.ui.dialogs import DiscardDialog, ImportReportDialog
from receipt_reader.ui.formatting import fold_search, format_decimal, format_millions, format_quantity, plural
from receipt_reader.ui.main_window import MainWindow, kpi_values, remaining_text
from receipt_reader.ui.preview import ZOOM_STEPS, next_zoom, zoom_label
from receipt_reader.ui.receipt_form import amount_check, changed_fields, field_error, source_caption
from receipt_reader.ui.review_panel import correction_text
from receipt_reader.ui.settings_dialog import ParametersDialog
from receipt_reader.ui.style import SERIES, apply_style
from receipt_reader.ui.summary import build_summary_matrix
from receipt_reader.ui.workers import TaskRunner, Worker
from support import DemoEnvironment, document, synthetic_receipt

PROGRAMS = [
    Program(1, "110", "Programa Uno", "Uno"),
    Program(2, "220", "Programa Dos", "Dos"),
    Program(3, "330", "Programa Tres", "Tres"),
]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    # La plataforma se lee al crear la aplicación: sin ventanas visibles.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    apply_style(app)
    return app


@pytest.fixture
def data_dir(demo_env: DemoEnvironment, tmp_path: Path) -> Path:
    """Carpeta de datos propia con una copia de la base de ejemplo."""
    folder = tmp_path / "datos"
    folder.mkdir()
    shutil.copyfile(demo_env.db_path, folder / "receipt_reader.db")
    return folder


@pytest.fixture
def window(qapp: QApplication, data_dir: Path) -> Iterator[MainWindow]:
    main = MainWindow(AppServices.create(AppPaths(data_dir)))
    main.set_unattended(True)
    main.show()
    main.wait_idle()
    yield main
    main.close()
    qapp.processEvents()


# Lógica de presentación


def test_changed_fields_ignores_spacing_and_detects_edits() -> None:
    original = {"issuer_name": "ANA  PÉREZ", "gross": "800.000", "gloss": "LINEA 1\nLINEA 2"}
    same = {"issuer_name": " ANA PÉREZ ", "gross": "800.000", "gloss": "LINEA 1\n\n  LINEA 2 "}
    assert changed_fields(original, same) == {}
    edited = {**same, "gross": "810.000"}
    assert changed_fields(original, edited) == {"gross": "810.000"}


def test_field_error_uses_the_same_parsers_as_the_reading() -> None:
    assert field_error("gross", "45.000,00") is None
    assert field_error("gross", "12,5")
    assert field_error("gross", "") is None
    assert field_error("issue_date", "13/25/2025")
    assert field_error("issue_date", "03-11-2025") is None
    assert field_error("service_period", "2025-10") is None
    assert field_error("hours", "22,5") is None
    assert field_error("hours", "500")
    assert field_error("workday_type", "semanal") is None


def test_amount_check_reports_expected_values_without_changing_them() -> None:
    rates = RetentionTable({2025: 1450})
    values = {"gross": "363.880", "issue_date": "01-11-2025", "retention": "50.034", "net": "313.846"}
    text, tone = amount_check(values, rates)
    assert tone == "warning"
    assert "$ 52.763" in text
    assert "$ 311.117" in text
    coherent = {**values, "retention": "52.763", "net": "311.117"}
    assert amount_check(coherent, rates)[1] == "good"
    assert amount_check({**values, "issue_date": "01-11-2024"}, rates)[1] == "warning"
    assert amount_check({**values, "gross": ""}, rates)[1] == "muted"


def test_source_caption_marks_low_ocr_confidence() -> None:
    assert source_caption(FieldTrace("1", 0.62, FieldSource.OCR), 0.85) == ("OCR 62 %", True)
    assert source_caption(FieldTrace("1", 0.97, FieldSource.OCR), 0.85) == ("OCR 97 %", False)
    assert source_caption(FieldTrace("1", 1.0, FieldSource.NATIVE), 0.85) == ("Texto nativo", False)
    assert source_caption(None, 0.85)[0] == "Sin lectura"


def test_program_colors_follow_the_catalog_not_the_filter() -> None:
    styles = program_styles(list(reversed(PROGRAMS)))
    assert [style.color for style in styles.values()] == list(SERIES[:3])
    assert styles["Programa Tres"].color == SERIES[2]
    assert ordered_series_names(["Programa Tres", "Otro", "Programa Uno"], styles) == [
        "Programa Uno",
        "Programa Tres",
        "Otro",
    ]


def test_period_labels_status_order_and_jitter() -> None:
    periods = [Period(2025, 11), Period(2025, 12), Period(2026, 1)]
    assert period_tick_labels(periods) == ["nov\n2025", "dic", "ene\n2026"]
    ordered = status_counts_in_order({ReceiptStatus.PENDING: 3})
    assert ordered[0][0] is ReceiptStatus.APPROVED
    assert dict(ordered)[ReceiptStatus.PENDING] == 3
    offsets = jitter_offsets(20)
    assert offsets == jitter_offsets(20)
    assert all(abs(value) <= 0.22 for value in offsets)


def test_month_gaps_are_filled_so_the_time_axis_is_honest() -> None:
    periods = [Period(2025, 11), Period(2026, 2)]
    months, series, continuous = fill_month_gaps(periods, {"Uno": [100, 200], "Dos": [0, 50]})
    assert continuous
    assert months == [Period(2025, 11), Period(2025, 12), Period(2026, 1), Period(2026, 2)]
    assert series == {"Uno": [100, 0, 0, 200], "Dos": [0, 0, 0, 50]}
    # Una boleta aislada muy antigua no llena el gráfico de meses vacíos: se omiten y se avisa.
    months, series, continuous = fill_month_gaps([Period(2019, 1), Period(2026, 2)], {"Uno": [10, 20]})
    assert not continuous
    assert months == [Period(2019, 1), Period(2026, 2)]
    assert series == {"Uno": [10, 20]}
    assert fill_month_gaps([], {"Uno": []}) == ([], {"Uno": []}, True)


def test_monthly_chart_shows_empty_months_and_thins_long_axes() -> None:
    styles = program_styles(PROGRAMS)
    periods = [Period(2025, 11), Period(2026, 2)]
    ax = draw_monthly_gross(Figure(), periods, {"Programa Uno": [1_200_000, 2_500_000]}, styles)
    assert len(ax.get_xticks()) == 4
    assert ax.get_title(loc="left") == "Bruto mensual de Uno"  # una sola serie: sin leyenda, el título la nombra
    assert not ax.figure.legends
    texts = [text.get_text() for text in ax.texts]
    # Totales en millones solo en los meses con boletas; el último texto es el subtítulo.
    assert texts[:-1] == ["1,2", "2,5"]
    assert "Sobre cada barra" in texts[-1]
    long_axis = [Period(2024, 1).shift(offset) for offset in range(24)]
    ax = draw_monthly_gross(Figure(), long_axis, {"Programa Uno": [1_000_000] * 24}, styles)
    texts = [text.get_text() for text in ax.texts]
    assert len(texts) == 1  # con muchos meses no se rotula cada barra
    assert "Sobre cada barra" not in texts[0]
    labels = [label.get_text() for label in ax.get_xticklabels()]
    assert labels[:3] == ["ene\n2024", "", "mar"]
    assert tick_step(12) == 1
    assert tick_step(24) == 2
    assert period_tick_labels([Period(2025, 12), Period(2026, 1)], 1) == ["dic\n2025", "ene\n2026"]


def test_remaining_time_waits_for_enough_progress() -> None:
    assert remaining_text(2, 50) == ""
    assert remaining_text(60, 1) == ""
    assert remaining_text(30, 50) == "queda menos de un minuto"
    assert remaining_text(120, 50) == "quedan unos 2 minutos"
    assert remaining_text(3_600, 25) == "quedan unas 3 horas"
    assert remaining_text(500, 100) == ""


def test_formatting_helpers_use_chilean_conventions() -> None:
    assert format_millions(12_430_000) == "12,4"
    assert format_millions(5_000_000) == "5"
    assert format_decimal(Decimal("1234.55"), 1) == "1.234,6"
    assert format_quantity(Decimal("22.50")) == "22,5"
    assert format_quantity(Decimal(44)) == "44"
    assert plural(1, "boleta") == "1 boleta"
    assert plural(1200, "boleta") == "1.200 boletas"
    assert fold_search("Núñez 44.583.943-4") == "nunez 44583943-4"


def test_zoom_steps_and_labels() -> None:
    assert next_zoom(0.9, 1) == 1.0
    assert next_zoom(0.9, -1) == 0.75
    assert next_zoom(ZOOM_STEPS[-1], 1) == ZOOM_STEPS[-1]
    assert zoom_label(None) == "Ajustado"
    assert zoom_label(1.25) == "125 %"


def test_kpi_values_without_data_explain_the_empty_state() -> None:
    values = kpi_values(Overview(total_receipts=0, counts={}, valid=Totals()))
    assert values["approved"][:2] == ("0", "sin boletas registradas")
    assert values["retention"][:2] == ("$ 0", "sin boletas válidas")


def test_kpi_values_summarize_the_overview() -> None:
    overview = Overview(
        total_receipts=10,
        counts={ReceiptStatus.APPROVED: 7, ReceiptStatus.PENDING: 2, ReceiptStatus.DISCARDED: 1},
        valid=Totals(7, 7_000_000, 1_015_000, 5_985_000),
    )
    values = kpi_values(overview)
    assert values["receipts"][:2] == ("10", "incluye 1 descartada")
    assert values["approved"][:2] == ("7", "70 % del total")
    assert values["pending"] == ("2", "2 pendientes, 0 con error", "warning")
    assert values["gross"][0] == "$ 7.000.000"
    assert values["retention"][1] == "14,5 % del bruto"


def test_correction_text_translates_states() -> None:
    when = datetime(2026, 7, 20, 10, 0)
    assert correction_text(Correction(1, "gross", None, "800000", when)) == (
        "20-07-2026 10:00. Monto bruto: de vacío a 800000."
    )
    assert "de Pendiente a Aprobada" in correction_text(Correction(1, "status", "pending", "approved", when))


def test_summary_matrix_totals_come_from_sql(reports: ReportService) -> None:
    summary = reports.summary()
    order = [4, 3, 2, 1]
    matrix = build_summary_matrix(summary, "gross", program_order=order)
    assert matrix.grand_total == summary.grand_total.gross
    assert sum(row[-1] or 0 for row in matrix.cells[:-1]) == summary.grand_total.gross
    by_id = {row.program_id: row.gross for row in summary.program_totals}
    assert [matrix.column_total(index) for index in range(len(order))] == [by_id[pid] for pid in order]
    counts = build_summary_matrix(summary, "count")
    assert counts.grand_total == summary.grand_total.count
    with pytest.raises(ValueError, match="Medida"):
        build_summary_matrix(summary, "otra")


def test_hourly_groups_use_warnings_and_reference_ranges(demo_db: Path) -> None:
    catalog = CatalogService(demo_db)
    rows = ReportService(demo_db).rows()
    groups = hourly_groups(rows, catalog.programs(), catalog.reference_rates())
    valid_with_rate = [row for row in rows if row.status.is_valid and row.hourly_rate and row.program_id]
    assert sum(len(group.rates) for group in groups) == len(valid_with_rate)
    flagged = sum(1 for row in valid_with_rate if HOURLY_OUT_OF_RANGE in row.issue_codes)
    assert sum(group.flagged_count for group in groups) == flagged
    assert all(group.reference is not None and group.reference[0] < group.reference[1] for group in groups)


# Diálogos


@pytest.mark.usefixtures("qapp")
def test_discard_dialog_requires_a_reason() -> None:
    dialog = DiscardDialog("La boleta N° 1")
    assert not dialog.accept_button.isEnabled()
    dialog.reason_combo.setEditText("no")
    assert not dialog.accept_button.isEnabled()
    dialog.reason_combo.setEditText("  Copia   del mismo archivo ")
    assert dialog.accept_button.isEnabled()
    assert dialog.reason() == "Copia del mismo archivo"
    dialog.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_import_report_dialog_lists_rejected_rows() -> None:
    rejected = (RejectedRow(3, "RUT inválido"), RejectedRow(7, "Falta el nombre"))
    dialog = ImportReportDialog("Importación", "1 fila importada, 2 rechazadas.", rejected)
    tables = dialog.findChildren(QTableWidget)
    assert tables
    assert tables[0].rowCount() == 2
    dialog.deleteLater()


def test_parameters_dialog_validates_and_saves(qapp: QApplication, data_dir: Path) -> None:
    services = AppServices.create(AppPaths(data_dir))
    dialog = ParametersDialog(services)
    dialog.unattended = True
    # Las columnas numéricas no se estiran hasta el borde: el sobrante lo toma una columna de texto.
    assert not dialog.rates_view.horizontalHeader().stretchLastSection()
    assert dialog.reference_view.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    assert dialog.save_settings_button.isEnabled()
    dialog.min_edit.setText("6.000.000")
    assert not dialog.save_settings_button.isEnabled()
    assert dialog.settings_error.text()
    dialog.min_edit.setText("30.000")
    dialog.rut_edit.setText("12.345.678-0")
    assert not dialog.save_settings_button.isEnabled()
    dialog.rut_edit.setText(services.catalog.settings().organization_rut)
    dialog.max_edit.setText("4.000.000")
    assert dialog.save_settings_button.isEnabled()
    dialog.save_settings()
    dialog.tasks.wait_all()
    for _ in range(5):
        qapp.processEvents()
    assert dialog.data_changed
    assert services.catalog.settings().amount_max == 4_000_000
    assert dialog.tolerance_spin.suffix() == " peso"
    dialog.deleteLater()


def test_parameters_dialog_program_crud(qapp: QApplication, data_dir: Path) -> None:
    services = AppServices.create(AppPaths(data_dir))
    dialog = ParametersDialog(services)
    dialog.unattended = True

    def run_and_wait() -> None:
        dialog.tasks.wait_all()
        for _ in range(5):
            qapp.processEvents()

    # Crear.
    dialog.program_code_edit.setText("450")
    dialog.program_name_edit.setText("Programa de prueba")
    dialog.program_short_edit.setText("Prueba")
    assert dialog.save_program_button.isEnabled()
    dialog.save_program()
    run_and_wait()
    created = next(p for p in services.catalog.programs() if p.folder_code == "450")
    assert created.active
    assert dialog.data_changed

    # Editar: seleccionar la fila recién creada y cambiar el nombre corto.
    row_index = next(i for i, p in enumerate(dialog.programs_model.rows()) if p.id == created.id)
    _select_source_row(dialog.programs_view, row_index)
    dialog._pick_program()
    assert dialog.program_name_edit.text() == "Programa de prueba"
    dialog.program_short_edit.setText("Prueba corta")
    dialog.save_program()
    run_and_wait()
    edited = next(p for p in services.catalog.programs() if p.id == created.id)
    assert edited.short_name == "Prueba corta"

    # Desactivar.
    row_index = next(i for i, p in enumerate(dialog.programs_model.rows()) if p.id == created.id)
    _select_source_row(dialog.programs_view, row_index)
    dialog._pick_program()
    assert dialog.toggle_program_button.text() == "Desactivar"
    dialog.toggle_program_active()
    run_and_wait()
    deactivated = next(p for p in services.catalog.programs() if p.id == created.id)
    assert not deactivated.active

    # Alias: agregar y quitar.
    dialog.alias_program_combo.setCurrentIndex(0)
    dialog.alias_text_edit.setText("ALIAS DE PRUEBA")
    assert dialog.add_alias_button.isEnabled()
    dialog.add_alias()
    run_and_wait()
    assert any(a.alias == "ALIAS DE PRUEBA" for a in services.catalog.aliases())
    row_index = next(i for i, row in enumerate(dialog.aliases_model.rows()) if row["alias"] == "ALIAS DE PRUEBA")
    _select_source_row(dialog.aliases_view, row_index)
    dialog._pick_alias()
    dialog.remove_alias()
    run_and_wait()
    assert not any(a.alias == "ALIAS DE PRUEBA" for a in services.catalog.aliases())
    dialog.deleteLater()


# Ventana


def test_window_autotest_passes_without_ocr(window: MainWindow) -> None:
    assert window.autotest(check_ocr=False) == []
    assert window.tabs.tabText(0).startswith("Revisión (")
    assert str(window.services.samples_dir) in window.folder_edit.toolTip()


def test_filters_update_totals_and_tables(window: MainWindow) -> None:
    all_rows = window.records_model.rowCount()
    index = window.program_combo.findData(1)
    window.program_combo.setCurrentIndex(index)
    rows = window.records_model.rows()
    assert 0 < len(rows) < all_rows
    assert {row.program_id for row in rows} == {1}
    window.status_combo.setCurrentIndex(window.status_combo.findData("review"))
    statuses = {row.status for row in window.records_model.rows()}
    assert statuses <= {ReceiptStatus.PENDING, ReceiptStatus.ERROR}
    window.clear_filters()
    assert window.records_model.rowCount() == all_rows
    window.axis_combo.setCurrentIndex(window.axis_combo.findData(PeriodAxis.PAYMENT.value))
    periods = [window.period_combo.itemData(i) for i in range(1, window.period_combo.count())]
    assert periods == sorted(periods, reverse=True)
    window.period_combo.setCurrentIndex(1)
    assert window.current_filter().period == Period.parse(periods[0])
    assert window.current_filter().axis is PeriodAxis.PAYMENT


def _select_source_row(view: QTableView, source_row: int) -> None:
    """Selecciona en la vista (posiblemente ordenada por un proxy) la fila del modelo de origen."""
    proxy = view.model()
    if isinstance(proxy, QSortFilterProxyModel):
        source_index = proxy.sourceModel().index(source_row, 0)
        view.selectRow(proxy.mapFromSource(source_index).row())
    else:
        view.selectRow(source_row)


def _queued_with(window: MainWindow, issue_code: str) -> int:
    """Primera boleta de la cola que tiene la incidencia indicada."""
    rows = window.services.review.queue(ReceiptFilter(issue_code=issue_code))
    assert rows, issue_code
    return rows[0].id


def test_saving_a_correction_audits_only_that_receipt(window: MainWindow, data_dir: Path) -> None:
    db_path = data_dir / "receipt_reader.db"
    receipt_id = _queued_with(window, "GROSS_MISSING")
    before = {row.id: row.gross for row in ReportService(db_path).rows()}
    gross_before = window.services.reports.overview().valid.gross
    panel = window.review_panel
    assert panel.select_receipt(receipt_id)
    form = panel.form
    assert form.rows["gross"].suggestion_box.isVisibleTo(form)
    form.apply_suggestion("gross")
    assert form.is_dirty()
    assert panel.save_button.isEnabled()
    assert not panel.approve_button.isEnabled()
    panel.save_correction()
    saved = window.services.review.detail(receipt_id)
    assert saved.record.status is ReceiptStatus.CORRECTED
    assert [item.field for item in saved.corrections] == ["gross"]
    # Ya no espera revisión: la cola pasa a la boleta siguiente.
    assert receipt_id not in {row.id for row in panel.queue_rows()}
    assert panel.detail is not None and panel.detail.record.id != receipt_id
    assert "Corrección guardada" in window.statusBar().currentMessage()
    after = {row.id: row.gross for row in ReportService(db_path).rows()}
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {receipt_id}
    assert window.services.reports.overview().valid.gross == gross_before + after[receipt_id]
    assert window.kpis.card("gross").value_text() != ""


def test_invalid_input_blocks_saving(window: MainWindow) -> None:
    panel = window.review_panel
    assert panel.detail is not None
    panel.form.set_field_text("issuer_rut", "11.111.111-2")
    assert panel.form.field_error_text("issuer_rut")
    assert not panel.save_button.isEnabled()
    panel.form.set_field_text("issuer_rut", panel.detail.form_values["issuer_rut"])
    assert not panel.form.is_dirty()


def test_approve_discard_and_restore(window: MainWindow) -> None:
    panel = window.review_panel
    old = _queued_with(window, "PROGRAM_CONFLICT")
    assert panel.select_receipt(old)
    assert panel.approve_button.isEnabled()
    queue_before = len(panel.queue_rows())
    panel.approve()
    assert window.services.review.detail(old).record.status is ReceiptStatus.APPROVED
    assert old not in {row.id for row in panel.queue_rows()}
    assert len(panel.queue_rows()) == queue_before - 1

    wrong_rate = _queued_with(window, "RATE_MISMATCH")
    assert panel.select_receipt(wrong_rate)
    assert not panel.approve_button.isEnabled()
    assert "corrija" in panel.approve_button.toolTip().lower()
    assert panel.discard_with_reason(wrong_rate, "Boleta anulada por el emisor")
    assert window.services.review.detail(wrong_rate).record.status is ReceiptStatus.DISCARDED
    assert wrong_rate not in {row.id for row in panel.queue_rows()}
    assert panel.select_receipt(wrong_rate)
    assert panel.form.is_read_only()
    assert panel.restore_button.isVisibleTo(panel)
    panel.restore()
    assert window.services.review.detail(wrong_rate).record.status is ReceiptStatus.PENDING


def test_record_opens_in_review_and_preview_loads(window: MainWindow) -> None:
    approved_row = next(
        index for index, row in enumerate(window.records_model.rows()) if row.status is ReceiptStatus.APPROVED
    )
    receipt_id = window.records_model.row(approved_row).id
    window.records_view.selectRow(approved_row)
    window.open_selected_in_review()
    panel = window.review_panel
    assert panel.detail is not None and panel.detail.record.id == receipt_id
    assert panel.scope_combo.currentData() == "all"
    window.wait_idle()
    assert panel.preview.has_image()
    panel.preview.step_zoom(1)
    assert panel.preview.zoom() is not None
    panel.preview.fit_width()
    assert panel.preview.zoom() is None


def test_processing_runs_in_background_and_refreshes(qapp: QApplication, data_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "entrada"
    pages = write_documents(
        [document("2026-03/110 Atención Comunitaria/Nueva boleta B 900.pdf", [synthetic_receipt(folio=900)])],
        root,
        dpi=100,
        seed=21,
    )
    services = AppServices.create(AppPaths(data_dir), ocr=SyntheticOcrEngine(pages))
    main = MainWindow(services)
    main.set_unattended(True)
    try:
        before = main.records_model.rowCount()
        main.folder_edit.setText(str(root))
        main.process_folder()
        assert not main.process_button.isEnabled()
        main.wait_idle()
        assert main.process_button.isEnabled()
        assert main.records_model.rowCount() == before + 1
        assert main.progress.value() == 100
        assert main.progress.format() == "%p %"  # sin estimación de tiempo una vez terminado
        assert "1 de 1 archivo procesado" in main.progress_label.full_text()
    finally:
        main.close()
        qapp.processEvents()


def test_worker_reports_back_on_the_interface_thread(qapp: QApplication) -> None:
    # B30: la tarea corre en un hilo del pool; el avance y el resultado llegan por señales al
    # hilo de la interfaz, que es el único que toca los widgets.
    threads: dict[str, threading.Thread] = {}
    results: list[object] = []

    def task(progress: Callable[[int, str], None], cancel: threading.Event) -> str:
        threads["task"] = threading.current_thread()
        progress(50, "Mitad del trabajo")
        return "cancelada" if cancel.is_set() else "lista"

    def on_progress(_percent: int, _message: str) -> None:
        threads["progress"] = threading.current_thread()

    def on_finished(result: object) -> None:
        threads["finished"] = threading.current_thread()
        results.append(result)

    runner = TaskRunner(max_threads=1)
    runner.start(Worker(task, with_progress=True), on_finished, results.append, on_progress)
    deadline = time.monotonic() + 10
    while runner.is_busy() and time.monotonic() < deadline:
        qapp.processEvents()
    assert results == ["lista"]
    assert threads["task"] is not threading.main_thread()
    assert threads["progress"] is threading.main_thread()
    assert threads["finished"] is threading.main_thread()


def test_export_and_capture_from_the_window(window: MainWindow, tmp_path: Path) -> None:
    target = tmp_path / "informe.xlsx"
    window.start_export(target)
    window.wait_idle()
    assert target.exists()
    saved = capture_tabs(window, window.tabs, tmp_path / "capturas", size=(1366, 860), settle=window.wait_idle)
    assert [path.name for path in saved] == ["01_revision.png", "02_registro.png", "03_graficos.png", "04_resumen.png"]
    assert all(path.stat().st_size > 0 for path in saved)
    assert window.services.reports.overview(ReceiptFilter()).total_receipts > 0


def test_window_handles_an_empty_database(qapp: QApplication, catalog_db: Path, tmp_path: Path) -> None:
    folder = tmp_path / "vacio"
    folder.mkdir()
    shutil.copyfile(catalog_db, folder / "receipt_reader.db")
    main = MainWindow(AppServices.create(AppPaths(folder)))
    main.set_unattended(True)
    try:
        main.draw_charts()
        assert main.monthly_chart.message is not None
        assert main.status_chart.message is not None
        assert main.hourly_chart.message is not None
        assert main.kpis.card("receipts").value_text() == "0"
        assert main.review_panel.detail is None
        assert not main.summary_view.isVisibleTo(main)
        problems = main.autotest(check_ocr=False)
        assert "La cola de revisión está vacía." in problems
    finally:
        main.close()
        qapp.processEvents()
