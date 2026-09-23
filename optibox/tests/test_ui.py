"""Pruebas de humo de la ventana y de la lógica de presentación de la interfaz.

Corren sin ventana visible (QT_QPA_PLATFORM=offscreen). Los diálogos modales
se reemplazan por registros para que ninguna prueba quede esperando un clic.
"""

from __future__ import annotations

import itertools
import os
import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from factories import PlannedDatabase
from optibox.domain.agenda import AgendaEntry, AgendaKind
from optibox.domain.diagnosis import UnmetCause, UnmetDemand
from optibox.domain.instance import PlanningDay
from optibox.domain.run import RunDetail
from optibox.domain.timegrid import DEFAULT_HOURS
from optibox.errors import AppError, OperationCancelledError
from optibox.services.excel_service import ExcelService, ImportReport, RejectedRow
from optibox.services.master_data_service import DemandRow, MasterDataService
from optibox.services.planning_service import PlanningService
from optibox.ui.agenda_tab import ROOM_MODE, STAFF_MODE
from optibox.ui.charts import (
    OCCUPANCY_FILLS,
    Region,
    draw_agenda,
    draw_service_coverage,
    draw_staff_load,
    draw_unmet,
    legend_columns,
    occupancy_colors,
)
from optibox.ui.coverage_tab import block_detail_rows, keep_block
from optibox.ui.demand_tab import PRIORITY_COL, SESSIONS_COL, DemandTab, DemandTableModel, demand_value_error
from optibox.ui.formatting import format_count, format_hours, format_pct
from optibox.ui.main_window import MainWindow
from optibox.ui.master_dialogs import AbsenceFormDialog, ContractDialog, ImportReportDialog
from optibox.ui.presentation import (
    CAUSE_ORDER,
    band_of,
    cause_label,
    coverage_grid,
    coverage_tone,
    day_label,
    demand_grid,
    layout_agenda,
    occupancy_bands,
    progress_text,
    staff_label,
    text_color_on,
    unmet_by_block,
    week_range_text,
)
from optibox.ui.style import apply_style
from optibox.ui.workers import Worker

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    apply_style(app)
    return app


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Registra los mensajes en vez de abrir cuadros de diálogo modales."""
    shown: list[str] = []

    def record(_parent: object, _title: str, message: str, *_args: object) -> QMessageBox.StandardButton:
        shown.append(message)
        return QMessageBox.StandardButton.Yes

    for name in ("warning", "information", "question", "critical"):
        monkeypatch.setattr(QMessageBox, name, record)
    return shown


@pytest.fixture(scope="module")
def detail(planned_db: PlannedDatabase) -> RunDetail:
    return PlanningService(planned_db.path).load_run(planned_db.run_id)


@pytest.fixture(scope="module")
def window(qapp: QApplication, planned_db: PlannedDatabase) -> Iterator[MainWindow]:
    """Ventana sobre la base compartida; las pruebas que la usan no escriben en la base."""
    path = planned_db.path
    main = MainWindow(PlanningService(path), MasterDataService(path), ExcelService(path), path.parent)
    main.show()
    qapp.processEvents()
    yield main
    main.hide()
    main.deleteLater()


@pytest.fixture
def db_copy(tmp_path: Path, planned_db: PlannedDatabase) -> Path:
    """Copia editable de la base con una corrida."""
    target = tmp_path / "optibox.db"
    shutil.copy(planned_db.path, target)
    return target


def _entry(day: int, start: int, end: int, lane: int = 0) -> AgendaEntry:
    return AgendaEntry(day, start, end, AgendaKind.SESSION, "Sesión", "#2A78D6", "TO-01", "B01", "AIN", lane)


def _unmet(day: int, block: int, service: str, required: int, covered: int, cause: UnmetCause) -> UnmetDemand:
    return UnmetDemand(day, block, service, required, covered, cause, "detalle")


def test_day_label_and_week_range_use_dates() -> None:
    assert day_label(0, date(2026, 9, 28)) == "Lun 28-09"
    assert day_label(4) == "Vie"
    assert week_range_text(date(2026, 9, 28)) == "lunes 28-09-2026 a viernes 02-10-2026"


@pytest.mark.parametrize(
    ("pct", "tone"), [(None, "muted"), (1.0, "good"), (0.95, "good"), (0.9, "warning"), (0.85, "warning"), (0.5, "bad")]
)
def test_coverage_tone_thresholds(pct: float | None, tone: str) -> None:
    assert coverage_tone(pct) == tone


def test_staff_label_keeps_code_and_shortens_second_surname() -> None:
    assert staff_label("TO-01", "Catalina Vergara Valenzuela") == "TO-01 Catalina Vergara V."
    assert staff_label("AD-01", "Ana Soto") == "AD-01 Ana Soto"


def test_text_color_on_picks_the_most_readable_ink() -> None:
    assert text_color_on("#1F4E79") == "#FFFFFF"
    assert text_color_on("#FDF6F1") == "#1F2933"


def test_unmet_by_block_groups_by_block_with_causes_in_fixed_order() -> None:
    items = [
        _unmet(1, 540, "AIN", 3, 1, UnmetCause.STAFF_BUSY),
        _unmet(1, 540, "EIN", 1, 0, UnmetCause.NO_CANDIDATES),
        _unmet(0, 600, "AFA", 2, 1, UnmetCause.CAP),
        _unmet(2, 480, "AIN", 1, 1, UnmetCause.ROOM_BUSY),
    ]
    bars = unmet_by_block(items)
    assert [(bar.day, bar.block) for bar in bars] == [(0, 600), (1, 540)]
    tuesday = bars[1]
    assert tuesday.total == 3
    assert tuesday.by_cause == ((UnmetCause.NO_CANDIDATES, 1), (UnmetCause.STAFF_BUSY, 2))
    assert [cause for cause, _ in tuesday.by_cause] == sorted(
        (cause for cause, _ in tuesday.by_cause), key=CAUSE_ORDER.index
    )


def test_layout_agenda_splits_only_the_entries_that_overlap() -> None:
    boxes = {
        (box.entry.start, box.entry.lane): box
        for box in layout_agenda([_entry(0, 480, 525), _entry(0, 500, 545, lane=1), _entry(0, 600, 645)])
    }
    first, second, alone = boxes[(480, 0)], boxes[(500, 1)], boxes[(600, 0)]
    assert first.lanes == second.lanes == 2
    assert first.width == pytest.approx(second.width)
    assert second.x == pytest.approx(first.x + first.width)
    assert alone.lanes == 1
    assert alone.width == pytest.approx(2 * first.width)


def test_demand_grid_sums_by_block_and_marks_closed_blocks() -> None:
    rows = [(0, 480, "AIN", 2), (0, 480, "EVI", 1), (4, 900, "AIN", 3), (4, 900, "EVI", 0)]
    open_blocks = {0: [480, 900, 960], 4: [480, 900]}
    grid = demand_grid(rows, open_blocks)
    assert grid.cell(0, 480) is not None
    assert grid.cell(0, 480).required == 3
    assert grid.cell(4, 900).details == (("AIN", 3, 0),)
    assert grid.cell(4, 960).is_open is False
    assert grid.total_required == 6
    only_evi = demand_grid(rows, open_blocks, service="EVI")
    assert only_evi.total_required == 1


def test_occupancy_bands_split_up_to_the_recommended_capacity() -> None:
    bands = occupancy_bands(8, 10)
    assert [(b.low, b.high, b.over) for b in bands] == [(1, 3, False), (4, 5, False), (6, 8, False), (9, 10, True)]
    assert bands[0].label == "1 a 3 personas"
    assert bands[-1].label == "9 a 10 personas (sobre la recomendada)"
    assert band_of(bands, 7) == bands[2]
    assert band_of(bands, 0) is None


def test_occupancy_bands_handle_small_and_equal_capacities() -> None:
    single = occupancy_bands(1, 1)
    assert [(b.low, b.high) for b in single] == [(1, 1)]
    assert single[0].label == "1 persona"
    assert not any(band.over for band in occupancy_bands(10, 10))
    small = occupancy_bands(2, 5)
    assert [(b.low, b.high, b.over) for b in small] == [(1, 1, False), (2, 2, False), (3, 5, True)]


def test_occupancy_colors_keep_the_darkest_steps_and_warn_above_the_recommended() -> None:
    assert occupancy_colors(occupancy_bands(8, 10)) == OCCUPANCY_FILLS
    assert occupancy_colors(occupancy_bands(2, 5)) == OCCUPANCY_FILLS[1:]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Fase B: solución con valor 186700", "Fase B: solución con valor 186.700"),
        ("3888 de 117936 evaluados.", "3.888 de 117.936 evaluados."),
        ("valor 383", "valor 383"),
        ("semana 28-09-2026", "semana 28-09-2026"),
        ("tiempo 1234,5 s", "tiempo 1234,5 s"),
        ("ya formateado 12.345", "ya formateado 12.345"),
    ],
)
def test_progress_text_formats_long_integers_only(message: str, expected: str) -> None:
    assert progress_text(message) == expected


def test_coverage_filter_moves_the_selection_to_a_block_of_the_chosen_type(detail: RunDetail) -> None:
    by_block: dict[tuple[int, int], set[str]] = {}
    for row in detail.metrics.coverage_by_block_service:
        if row.service is not None and row.required:
            by_block.setdefault((row.day, row.block), set()).add(row.service)
    rare = min((s for s in detail.metrics.services if s.required), key=lambda s: (s.required, s.code)).code
    elsewhere = next(block for block, services in sorted(by_block.items()) if rare not in services)
    grid = coverage_grid(detail, rare)
    kept = keep_block(grid, elsewhere)
    assert kept is not None and kept != elsewhere
    assert rare in by_block[kept]
    assert keep_block(grid, kept) == kept
    rows = block_detail_rows(detail, *kept, service=rare)
    name = next(s.name for s in detail.instance.service_types if s.code == rare)
    assert rows and all(row.name == name for row in rows)
    assert len(block_detail_rows(detail, *kept)) >= len(rows)


def test_service_chart_highlights_the_filtered_type(detail: RunDetail) -> None:
    code = next(service.code for service in detail.metrics.services if service.required)
    figure = Figure()
    regions = draw_service_coverage(figure, detail.metrics.services, highlight=code)
    bold = [label.get_text() for label in figure.axes[0].get_yticklabels() if label.get_fontweight() == "bold"]
    assert len(bold) == 1
    assert {region.key for region in regions} >= {code}


def test_coverage_grid_matches_the_run_metrics(detail: RunDetail) -> None:
    grid = coverage_grid(detail)
    totals = detail.metrics.totals
    assert grid.total_required == totals.demand_sessions
    assert sum(cell.covered for cell in grid.iter_cells()) == totals.covered_sessions
    for cell in grid.iter_cells():
        assert sum(required for _, required, _ in cell.details) == cell.required
    friday = detail.instance.days[4]
    closed = [block for block in grid.blocks if block not in friday.hours.blocks()]
    assert all(grid.cell(4, block).is_open is False for block in closed)


@pytest.mark.parametrize(
    ("column", "value", "valid"),
    [
        (SESSIONS_COL, 0, True),
        (SESSIONS_COL, 50, True),
        (SESSIONS_COL, 51, False),
        (SESSIONS_COL, -1, False),
        (SESSIONS_COL, "abc", False),
        (SESSIONS_COL, 2.5, False),
        (PRIORITY_COL, 3, True),
        (PRIORITY_COL, 4, False),
    ],
)
def test_demand_value_error(column: int, value: object, valid: bool) -> None:
    assert (demand_value_error(column, value) is None) is valid


def test_demand_model_keeps_edits_pending_and_rejects_invalid_values() -> None:
    model = DemandTableModel()
    model.load([DemandRow(0, "Lunes", 480, "08:00-09:00", "AIN", "Atención individual", 2, 2)])
    errors: list[str] = []
    model.validation_failed.connect(errors.append)
    sessions = model.index(0, SESSIONS_COL)
    assert model.setData(sessions, 99) is False
    assert errors
    assert model.pending_items() == []
    assert model.setData(sessions, 5) is True
    assert model.setData(model.index(0, PRIORITY_COL), 1) is True
    (item,) = model.pending_items()
    assert (item.sessions, item.priority) == (5, 1)
    assert model.data(sessions, Qt.ItemDataRole.BackgroundRole) is not None
    model.revert()
    assert model.pending_items() == []


def test_chart_functions_return_one_region_per_mark(detail: RunDetail) -> None:
    regions = draw_staff_load(Figure(), detail.metrics.staff_load)
    assert len(regions) == len(detail.metrics.staff_load)
    bars = unmet_by_block(detail.unmet)
    names = {service.code: service.name for service in detail.instance.service_types}
    assert len(draw_unmet(Figure(), bars, detail.instance.days, names)) == len(bars)
    staff = PlanningService.staff_agenda(detail, detail.plan.sessions[0].staff)
    agenda = draw_agenda(Figure(), staff, detail.instance.days, title="Agenda", names=names, show_staff=False)
    assert len(agenda) == len(staff)
    region = agenda[0]
    assert region.contains(region.axes, (region.x0 + region.x1) / 2, (region.y0 + region.y1) / 2)
    assert not region.contains(None, region.x0, region.y0)


def test_window_autotest_visits_every_tab_without_problems(window: MainWindow) -> None:
    assert window.autotest() == []
    assert window.tabs.count() == 8


def test_window_shows_the_run_totals(window: MainWindow, detail: RunDetail) -> None:
    totals = detail.metrics.totals
    assert window.kpis.card("coverage").value_text() == format_pct(totals.coverage_pct)
    assert window.kpis.card("demand").value_text() == str(totals.demand_sessions)
    assert f"Corrida {detail.summary.id}" in window.run_info.text()
    assert window.run_list.count() >= 1
    assert window.kpis.card("staff_idle").value_text() == format_hours(totals.staff_idle_min)
    assert window.kpis.card("room_idle").value_text() == format_hours(totals.room_idle_min)


def test_hours_tab_shows_staff_and_room_rows(window: MainWindow, detail: RunDetail) -> None:
    tab = window.hours_tab
    assert tab.staff_model.rowCount() == len(detail.metrics.staff_hours)
    assert tab.room_model.rowCount() == len(detail.metrics.rooms)
    assert not tab.staff_chart.has_message
    assert not tab.room_chart.has_message


def test_agenda_of_the_admin_room_shows_occupancy(window: MainWindow) -> None:
    tab = window.agenda_tab
    tab.set_mode(ROOM_MODE)
    tab.target_combo.setCurrentIndex(tab.target_combo.findData("ADM"))
    assert tab.entries
    assert all(entry.label.endswith("personas") for entry in tab.entries if entry.kind is AgendaKind.ADMIN)
    tab.set_mode(STAFF_MODE)
    assert tab.model.rowCount() > 0


def test_admin_room_agenda_is_shaded_by_occupancy(window: MainWindow, detail: RunDetail) -> None:
    tab = window.agenda_tab
    tab.set_mode(ROOM_MODE)
    fill_of, legend = tab._occupancy_colors(detail, "ADM")
    assert fill_of is not None and legend is not None
    assert [color for color, _ in legend] == list(OCCUPANCY_FILLS)
    entries = PlanningService.room_agenda(detail, "ADM")
    assert entries and all(fill_of(entry) in OCCUPANCY_FILLS for entry in entries)
    service_room = next(room.code for room in detail.instance.rooms if not room.is_admin_room)
    assert tab._occupancy_colors(detail, service_room) == (None, None)
    tab.set_mode(STAFF_MODE)


def test_coverage_filter_limits_the_block_detail_to_the_chosen_type(window: MainWindow, detail: RunDetail) -> None:
    tab = window.coverage_tab
    service = next(s for s in detail.instance.service_types if any(u.service == s.code for u in detail.unmet))
    tab.service_combo.setCurrentIndex(tab.service_combo.findData(service.code))
    try:
        assert tab.selected is not None
        assert tab.block_model.rowCount() == 1
        assert tab.block_model.row(0).name == service.name
    finally:
        tab.service_combo.setCurrentIndex(0)
    assert tab.block_model.rowCount() >= 1


def test_window_without_runs_shows_empty_states(seeded_db: Path) -> None:
    main = MainWindow(
        PlanningService(seeded_db), MasterDataService(seeded_db), ExcelService(seeded_db), seeded_db.parent
    )
    try:
        assert main.detail is None
        assert not main.export_plan_button.isEnabled()
        assert main.kpis.card("coverage").value_text() == "-"
        assert main.coverage_tab.heatmap.has_message
        assert main.hours_tab.staff_chart.has_message
        assert main.demand_tab.model.rowCount() > 0
        problems = main.autotest()
        assert "No hay ninguna corrida cargada." in problems
        assert not any(problem.startswith("Demanda") for problem in problems)
    finally:
        main.deleteLater()


def test_clicking_a_heatmap_cell_shows_its_detail(window: MainWindow, detail: RunDetail) -> None:
    tab = window.coverage_tab
    region = next(r for r in tab.heatmap.regions if r.key == (detail.unmet[0].day, detail.unmet[0].block))
    tab._on_cell_clicked(region)
    assert tab.selected == region.key
    assert tab.block_model.rowCount() > 0
    assert day_label(region.key[0]) in tab.block_title.text()


def test_optimize_button_flow_updates_the_window(
    window: MainWindow, detail: RunDetail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El flujo Worker -> progreso -> resultado, con el optimizador reemplazado por la corrida guardada."""
    progress: list[int] = []

    def fake_optimize(*_args: Any, progress: Any = None, cancel: Any = None, **_kwargs: Any) -> RunDetail:
        assert cancel is not None
        progress(50, "Fase A")
        return detail

    monkeypatch.setattr(window.planning, "optimize", fake_optimize)
    window.progress.valueChanged.connect(progress.append)
    window.start_optimization()
    assert window.tasks.wait_all(10_000)
    for _ in range(5):
        QApplication.processEvents()
    assert 50 in progress
    assert window.run_button.isEnabled()
    assert not window.cancel_button.isEnabled()
    assert window.detail is detail


def test_cancelled_optimization_is_reported_without_error_dialog(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, no_modal_dialogs: list[str]
) -> None:
    def cancelled(*_args: Any, **_kwargs: Any) -> RunDetail:
        raise OperationCancelledError("La optimización fue cancelada.")

    monkeypatch.setattr(window.planning, "optimize", cancelled)
    window.start_optimization()
    assert window.tasks.wait_all(10_000)
    for _ in range(5):
        QApplication.processEvents()
    assert window.progress_label.text() == "Optimización cancelada"
    assert no_modal_dialogs == []


def test_worker_routes_errors_to_the_right_signal() -> None:
    received: dict[str, list[object]] = {"finished": [], "failed": [], "cancelled": []}

    def run(fn: Any) -> None:
        worker = Worker(fn)
        worker.signals.finished.connect(received["finished"].append)
        worker.signals.failed.connect(received["failed"].append)
        worker.signals.cancelled.connect(received["cancelled"].append)
        worker.run()

    def cancelled() -> None:
        raise OperationCancelledError("cancelada")

    def expected() -> None:
        raise AppError("mensaje para el usuario")

    def unexpected() -> None:
        raise RuntimeError("detalle técnico")

    run(lambda: 42)
    run(cancelled)
    run(expected)
    run(unexpected)
    assert received["finished"] == [42]
    assert received["cancelled"] == ["cancelada"]
    assert received["failed"][0] == "mensaje para el usuario"
    assert "detalle técnico" not in str(received["failed"][1])


def test_demand_tab_saves_only_the_edited_rows(db_copy: Path) -> None:
    service = MasterDataService(db_copy)
    tab = DemandTab(service)
    tab.only_positive.setChecked(False)
    proxy = tab.proxy
    index = proxy.index(0, SESSIONS_COL)
    source = proxy.mapToSource(index)
    cell = tab.model.cell(source.row())
    assert proxy.setData(index, cell.sessions + 3)
    assert tab.has_changes
    assert tab.save_changes() is True
    assert not tab.has_changes
    saved = next(
        row
        for row in service.demand_grid()
        if (row.weekday, row.block_start, row.service_code)
        == (cell.row.weekday, cell.row.block_start, cell.row.service_code)
    )
    assert saved.sessions == cell.row.sessions + 3


def test_absence_dialog_keeps_open_and_explains_invalid_data(db_copy: Path) -> None:
    service = MasterDataService(db_copy)
    before = len(service.absences())
    dialog = AbsenceFormDialog(service, date(2026, 9, 28))
    dialog.kind.setText("  ")
    assert dialog.save() is False
    assert "motivo" in dialog.error.text()
    dialog.kind.setText("Permiso administrativo")
    dialog.full_day.setChecked(False)
    dialog.start.setCurrentIndex(dialog.start.findData(14 * 60))
    dialog.end.setCurrentIndex(dialog.end.findData(10 * 60))
    assert dialog.save() is False
    assert len(service.absences()) == before
    dialog.end.setCurrentIndex(dialog.end.findData(16 * 60))
    assert dialog.save() is True
    assert len(service.absences()) == before + 1


def test_contract_dialog_rejects_hours_that_are_not_quarter_hours(db_copy: Path) -> None:
    service = MasterDataService(db_copy)
    dialog = ContractDialog(service, date(2026, 10, 5))
    dialog.hours.setValue(10.1)
    assert dialog.save() is False
    assert dialog.error.isVisibleTo(dialog)
    dialog.hours.setValue(30.0)
    assert dialog.save() is True
    code = str(dialog.staff.currentData())
    person = next(p for p in service.staff() if p.code == code)
    assert max(person.contracts, key=lambda c: c.valid_from).weekly_minutes == 30 * 60


def test_import_report_dialog_lists_every_rejected_row() -> None:
    report = ImportReport(
        applied=False,
        rows_read={"Personal": 3},
        rejected=(RejectedRow("Personal", 4, "Falta el código"), RejectedRow("Salas", 0, "Hoja vacía")),
    )
    dialog = ImportReportDialog(report)
    assert dialog.model.rowCount() == 2
    assert dialog.model.data(dialog.model.index(1, 1)) == "Toda la hoja"


def test_import_summary_agrees_in_number() -> None:
    one_row = ImportReport(applied=False, rejected=(RejectedRow("Personal", 4, "Falta el código"),))
    assert one_row.summary.startswith("No se importó nada: 1 fila rechazada.")
    applied = ImportReport(applied=True, rows_read={"Personal": 12, "Feriados": 1})
    assert applied.summary == "Se importaron 13 filas en 2 hojas."


def test_hours_format_uses_decimal_comma() -> None:
    assert format_hours(450) == "7,5 h"


def test_counts_use_singular_for_one() -> None:
    assert format_count(1, "fila", "filas") == "1 fila"
    assert format_count(0, "fila", "filas") == "0 filas"
    assert format_count(1200, "sesión", "sesiones") == "1.200 sesiones"


def test_regions_ignore_other_axes() -> None:
    figure = Figure()
    first, second = figure.subplots(1, 2)
    region = Region(first, 0, 1, 0, 1, "texto")
    assert region.contains(first, 0.5, 0.5)
    assert not region.contains(second, 0.5, 0.5)


def _unmet_figure(count: int, causes: list[UnmetCause]) -> Figure:
    """Gráfico de turnos sin cubrir del tamaño que tiene a 1366 x 860 (unos 520 x 380 píxeles)."""
    figure = Figure(figsize=(5.2, 3.8), dpi=100, layout="constrained")
    FigureCanvasAgg(figure)
    blocks = (480, 540, 600, 660, 720, 840, 900, 960)
    slots = [(day, block) for day in range(5) for block in blocks][:count]
    items = [_unmet(day, block, "AIN", 2, 0, causes[index % len(causes)]) for index, (day, block) in enumerate(slots)]
    days = [PlanningDay(index, date(2026, 10, 5 + index), DEFAULT_HOURS[index], None, 0) for index in range(5)]
    draw_unmet(figure, unmet_by_block(items), days, {"AIN": "Atención individual"})
    figure.canvas.draw()
    return figure


def _label_overlaps(figure: Figure) -> int:
    ax = figure.axes[0]
    renderer = figure.canvas.get_renderer()  # type: ignore[attr-defined]
    boxes = [label.get_window_extent(renderer) for label in ax.get_xticklabels() if label.get_text()]
    return sum(1 for left, right in itertools.pairwise(boxes) if left.x1 > right.x0)


def test_unmet_chart_keeps_its_width_with_every_cause() -> None:
    """Con todas las causas la leyenda pasa a varias filas en vez de achicar el gráfico."""
    figure = _unmet_figure(12, list(UnmetCause))
    ax = figure.axes[0]
    assert ax.get_position().width >= 0.7
    legend = ax.get_legend()
    assert legend is not None
    assert legend.get_window_extent().x1 <= figure.bbox.width
    assert _label_overlaps(figure) == 0


@pytest.mark.parametrize("count", [24, 40])
def test_unmet_chart_labels_do_not_overlap_with_many_bars(count: int) -> None:
    figure = _unmet_figure(count, [UnmetCause.STAFF_BUSY, UnmetCause.ROOM_BUSY])
    assert _label_overlaps(figure) == 0
    labels = [label.get_text() for label in figure.axes[0].get_xticklabels() if label.get_text()]
    assert labels[0] == "Lun 08:00"


def test_legend_columns_fit_the_available_width() -> None:
    labels = [cause_label(cause) for cause in UnmetCause]
    assert legend_columns(labels, 2000, 4) == 4
    assert legend_columns(labels, 330, 4) == 2
    assert legend_columns(labels, 100, 4) == 1
