"""Interfaz sin ventana visible: lógica de presentación, gráficos, diálogos validados y ventana principal."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from matplotlib.figure import Figure
from openpyxl import Workbook
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from kpi_monitor.domain.engine import PeriodEvaluation
from kpi_monitor.domain.enums import (
    AlertKind,
    DenominatorType,
    GoalRuleType,
    Scale,
    Severity,
    Status,
)
from kpi_monitor.domain.models import Period, Thresholds
from kpi_monitor.errors import OperationCancelledError, ValidationError
from kpi_monitor.services import Services, build_services
from kpi_monitor.ui import main_window as main_window_module
from kpi_monitor.ui.charts import (
    HeatmapLayout,
    bar_height,
    draw_compliance,
    draw_heatmap,
    draw_progress,
    draw_site_comparison,
    row_at,
    spread_labels,
)
from kpi_monitor.ui.editors import ImportPreviewDialog, ObservationTarget
from kpi_monitor.ui.main_window import MainWindow
from kpi_monitor.ui.presenters import (
    NETWORK_KEY,
    Cell,
    ObservationFilter,
    alerts_summary,
    axis_value,
    cell_text,
    compliance_text,
    denominator_field,
    edit_text,
    filter_alerts,
    filter_observations,
    format_number,
    goal_text,
    kpi_values,
    last_data_text,
    matrix_rows,
    monthly_rows,
    order_observations,
    result_details,
    scope_detail,
    sheet_html,
)
from kpi_monitor.ui.views import KEEP_SITE, ComparisonTab, DataTab, ProgressTab, cell_column
from kpi_monitor.ui.widgets import RecordTableModel, ViewSwitch, distribute_widths, make_table_view
from kpi_monitor.ui.workers import TaskRunner, Worker

AUG = Period(2026, 8)
THRESHOLDS = Thresholds()


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


@pytest.fixture(autouse=True)
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Reemplaza los mensajes modales de la ventana por un registro, para que ninguna prueba se bloquee."""
    shown: list[tuple[str, str]] = []

    def record(kind: str) -> Callable[..., None]:
        def show(_parent: object, message: object, *_args: object) -> None:
            shown.append((kind, str(message)))

        return show

    monkeypatch.setattr(main_window_module, "show_error", record("error"))
    monkeypatch.setattr(main_window_module, "show_info", record("info"))
    monkeypatch.setattr(main_window_module, "show_saved_file", record("saved"))
    return shown


@pytest.fixture(scope="module")
def shared_services(template_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Services:
    """Servicios sobre una copia propia de la base, para las pruebas que solo leen."""
    target = tmp_path_factory.mktemp("ui") / "monitor.db"
    shutil.copy(template_db, target)
    return build_services(target)


@pytest.fixture(scope="module")
def window(shared_services: Services, tmp_path_factory: pytest.TempPathFactory) -> Iterator[MainWindow]:
    win = MainWindow(shared_services, tmp_path_factory.mktemp("exportaciones"))
    assert win.wait_until_idle()
    yield win
    win.close()


@pytest.fixture(scope="module")
def editable_window(template_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[MainWindow]:
    """Ventana sobre otra copia de la base para las pruebas que escriben.

    Cada prueba toca datos distintos (otra sede, mes o parámetro) para no depender del orden.
    """
    folder = tmp_path_factory.mktemp("ui_escritura")
    shutil.copy(template_db, folder / "monitor.db")
    win = MainWindow(build_services(folder / "monitor.db"), folder)
    assert win.wait_until_idle()
    yield win
    win.close()


# Lógica de presentación


def test_numbers_use_chilean_format_without_losing_decimals() -> None:
    """Los números de la interfaz usan formato chileno; al editar se conservan los decimales y los ejes muestran la
    unidad."""
    assert format_number(1234) == "1.234"
    assert format_number(1234.56) == "1.234,6"
    assert format_number(None) == "-"
    assert edit_text(1234.5) == "1.234,5"
    assert edit_text(12.3456) == "12,3456"
    assert edit_text(700.0) == "700"
    assert edit_text(None) == ""
    assert axis_value(0.85, Scale.PROPORTION) == "85 %"
    assert axis_value(20.0, Scale.DAYS) == "20"
    assert axis_value(7.25, Scale.RATE) == "7,3"


def test_texts_for_states_without_traffic_light(evaluation: PeriodEvaluation) -> None:
    """Los estados sin semáforo (línea base, no determinable, no aplica) muestran un texto explicativo en vez de un
    porcentaje."""
    baseline = evaluation.result("PC01", None)
    undetermined = evaluation.result("CG07", "PON")
    not_applicable = evaluation.result("CG04", "PON")
    assert baseline.status is Status.BASELINE
    assert compliance_text(baseline) == "Línea base"
    assert goal_text(baseline) == "Línea base (sin meta)"
    assert undetermined.status is Status.UNDETERMINED
    assert cell_text(undetermined) == "No determinable"
    assert goal_text(undetermined) == "No determinable"
    assert cell_text(not_applicable) == "No aplica"
    evaluated = evaluation.result("CG01", None)
    assert compliance_text(evaluated) == "96,6 %"
    assert cell_text(evaluated) == "96,6 %"


def test_goal_text_explains_bands_and_increases(evaluation: PeriodEvaluation) -> None:
    """El texto de la meta explica los tramos en días y, en las metas interanuales, la referencia al año anterior."""
    bands = evaluation.result("CG06", None)
    assert bands.goal.rule_type is GoalRuleType.BANDS
    assert goal_text(bands) == "20,0 días o menos (tramos)"
    increases = [
        r for r in evaluation.all_results() if r.goal.rule_type is not None and r.goal.rule_type.needs_reference
    ]
    determinable = next(r for r in increases if r.goal.determinable)
    assert "año anterior" in goal_text(determinable)


def test_kpi_cards_follow_the_summary(shared_services: Services) -> None:
    """Las tarjetas de totales muestran el índice, la proyección, los conteos por estado y las alertas del resumen,
    con los umbrales vigentes."""
    summary = shared_services.monitor.summary(AUG)
    cards = {c.key: c for c in kpi_values(summary, THRESHOLDS, {"CG": "Compromisos de gestión"})}
    assert list(cards) == ["index", "projected", "green", "yellow", "red", "no_data", "weight", "alerts"]
    assert cards["index"].value == "94,2 %"
    assert cards["index"].caption == "Compromisos de gestión"
    assert cards["index"].tone == "warning"
    assert cards["projected"].caption == "0,0 pp frente a hoy"
    assert (cards["green"].value, cards["yellow"].value, cards["red"].value) == ("7", "13", "3")
    assert cards["green"].caption == "desde 100 %"
    assert cards["red"].caption == "bajo 85 %"
    assert cards["alerts"].value == str(summary.alerts)
    assert cards["alerts"].caption == f"{summary.critical_alerts} críticas, {summary.new_alerts} nuevas"
    assert cards["alerts"].tone == "bad"


def test_scope_detail_does_not_repeat_the_program_word(shared_services: Services) -> None:
    """El texto del alcance explica la red y el filtro de programa sin repetir la palabra programa."""
    programs = {p.code: p for p in shared_services.catalog.programs()}
    text = scope_detail(None, programs["PC"], programs["CG"])
    assert text == "Red: suma de todas las sedes. Filtro de programa: Programa complementario."
    assert "corresponde a Compromisos de gestión" in scope_detail(None, None, programs["CG"])


def test_last_data_hint_warns_when_the_cut_is_later() -> None:
    """El panel lateral avisa cuando el mes de corte es posterior al último mes con datos (faltante no es cero)."""
    assert last_data_text(Period(2026, 8), 8) == "Último mes con datos: agosto 2026."
    assert last_data_text(Period(2026, 12), 8).endswith("figuran como no informados.")
    assert last_data_text(Period(2027, 3), None).startswith("Sin datos informados en 2027")


def test_monthly_rows_mark_pending_and_missing_months(evaluation: PeriodEvaluation) -> None:
    """La tabla mensual marca con color los meses no informados y como pendientes los posteriores al corte."""
    result = evaluation.result("CG01", "ORI")
    rows = monthly_rows(result)
    assert len(rows) == 12
    reported = [row["reported"] for row in rows]
    assert all(cell.text == "Pendiente" for cell in reported[8:])
    assert reported[7].text != "Sí"
    assert reported[7].fill is not None
    assert all(cell.fill is None for cell in reported[8:])


def test_result_details_list_the_projection(evaluation: PeriodEvaluation) -> None:
    """El panel de detalle del resultado lista el nivel, el cumplimiento, los meses informados, el método de
    proyección y los cortes."""
    details = dict(result_details(evaluation.result("CG01", None)))
    assert details["Nivel"] == "Red"
    assert details["Cumplimiento a la fecha"] == "96,6 %"
    assert details["Meses informados"] == "8 de 8 esperados"
    assert "simulaciones" in details["Método"]
    assert details["Cortes"].startswith("abril")


def test_matrix_rows_add_one_index_row_per_program(shared_services: Services) -> None:
    """La matriz de semáforo agrega al final una fila de índice ponderado por programa, en negrita."""
    monitor = shared_services.monitor
    matrix = monitor.status_matrix(AUG)
    levels = [code for code, _name in matrix.levels]
    indexes = [i for code in levels for i in monitor.indexes(AUG, code)]
    rows = matrix_rows(matrix, indexes, shared_services.catalog.programs(), THRESHOLDS)
    assert len(rows) == len(matrix.indicators) + 3
    assert [row["bold"] for row in rows[-3:]] == [True, True, True]
    first = rows[0]
    assert first["code"] == "CG01"
    assert first[NETWORK_KEY].text == "96,6 %"
    assert rows[-3]["indicator"].text == "Compromisos de gestión (índice ponderado)"
    assert rows[-3][NETWORK_KEY].text == "94,2 %"


def test_alert_filters_and_summary(evaluation: PeriodEvaluation) -> None:
    """Las alertas se filtran por gravedad, tipo e indicador, y el resumen cuenta las alertas filtradas."""
    alerts = list(evaluation.alerts)
    critical = filter_alerts(alerts, severity=Severity.CRITICAL)
    assert critical
    assert all(a.severity is Severity.CRITICAL for a in critical)
    no_report = filter_alerts(alerts, kind=AlertKind.NO_REPORT)
    assert all(a.kind is AlertKind.NO_REPORT for a in no_report)
    only_cg06 = filter_alerts(alerts, indicator_code="CG06")
    assert only_cg06
    assert {a.indicator_code for a in only_cg06} == {"CG06"}
    assert alerts_summary([]) == "Sin alertas para los filtros elegidos."
    assert alerts_summary(critical).startswith(f"{len(critical)} alertas: {len(critical)} críticas")


def test_observation_filters(shared_services: Services) -> None:
    """La tabla de datos filtra por mes, mes de corte, indicador y meses no reportados."""
    rows = shared_services.data.observations(2026)
    up_to_june = filter_observations(rows, ObservationFilter(up_to_month=6))
    assert up_to_june
    assert max(r.month for r in up_to_june) == 6
    only_march = filter_observations(rows, ObservationFilter(month=3, up_to_month=6))
    assert {r.month for r in only_march} == {3}
    missing = filter_observations(rows, ObservationFilter(only_missing=True))
    assert missing
    assert not any(r.reported for r in missing)
    cg02 = filter_observations(rows, ObservationFilter(indicator_code="CG02"))
    assert {r.indicator_code for r in cg02} == {"CG02"}


def test_observations_follow_the_catalog_order(shared_services: Services) -> None:
    """La tabla de datos lista cada mes con los indicadores y las sedes en el orden del catálogo, no alfabético."""
    catalog = shared_services.catalog
    indicator_order = {ind.code: position for position, ind in enumerate(catalog.indicators())}
    site_order = {site.code: position for position, site in enumerate(catalog.sites())}
    rows = order_observations(shared_services.data.observations(2026, 1), indicator_order, site_order)
    assert [r.site_code for r in rows if r.indicator_code == "CG02"] == ["NOR", "SUR", "CEN", "ORI", "PON"]
    programs = [r.indicator_code[:2] for r in rows]
    assert programs.index("PC") < programs.index("IA")
    keys = [(r.month, indicator_order[r.indicator_code], site_order[r.site_code]) for r in rows]
    assert keys == sorted(keys)


def test_denominator_capture_follows_the_indicator_type(shared_services: Services) -> None:
    """Regla: el denominador solo se pide cuando el indicador lo necesita; la meta fija y la población se calculan
    solas."""
    catalog = shared_services.catalog
    by_type = {ind.denominator_type: ind for ind in catalog.indicators()}
    fixed = denominator_field(by_type[DenominatorType.FIXED_SITE])
    assert (fixed.enabled, fixed.required) == (False, False)
    flow = denominator_field(by_type[DenominatorType.FLOW])
    assert (flow.enabled, flow.required) == (True, True)
    stock = denominator_field(by_type[DenominatorType.STOCK])
    assert (stock.enabled, stock.required) == (True, False)
    assert "stock" in stock.label.lower()
    population = denominator_field(by_type[DenominatorType.POPULATION])
    assert population.enabled is False


def test_sheet_html_includes_every_year(shared_services: Services) -> None:
    """La ficha del indicador en HTML incluye la meta y el peso de cada año y no contiene scripts."""
    catalog = shared_services.catalog
    views = [catalog.sheet("CG06", year) for year in catalog.years()]
    html = sheet_html(views, 2026)
    assert "CG06" in html
    for year in catalog.years():
        assert f"<td class='k'>{year}</td>" in html
    assert "<script" not in html


# Gráficos


def test_spread_labels_separates_close_values_and_keeps_order() -> None:
    """Las etiquetas de valores cercanos se separan en el gráfico sin cambiar su orden."""
    adjusted = spread_labels([0.94, 0.937, 0.975], 0.01)
    assert adjusted[1] < adjusted[0] < adjusted[2]
    ordered = sorted(adjusted)
    assert all(b - a >= 0.01 - 1e-9 for a, b in pairwise(ordered))
    assert spread_labels([0.5, 0.9], 0.01) == pytest.approx([0.5, 0.9])


def test_click_mapping_helpers() -> None:
    """Los clics en los gráficos se traducen a la fila o celda correcta; fuera de ellas no seleccionan nada, y las
    barras se adelgazan cuando hay pocas filas."""
    assert row_at(2.2, 5) == 2
    assert row_at(-0.6, 5) is None
    assert row_at(None, 5) is None
    layout = HeatmapLayout(("CG01", "CG02"), (None, "NOR"))
    assert layout.cell_at(1.1, 0.9) == ("CG02", "NOR")
    assert layout.cell_at(3.0, 0.0) is None
    assert bar_height(24) > bar_height(3)
    # En un gráfico alto con pocas filas, las barras no pasan de 24 px de grosor.
    assert bar_height(6, 600) * 600 / 6 == pytest.approx(24)
    assert bar_height(24, 640) == bar_height(24)


def test_compliance_chart_draws_one_bar_per_evaluated_indicator(evaluation: PeriodEvaluation) -> None:
    """El gráfico de cumplimiento dibuja una barra por indicador evaluado, rotula la línea base y marca el 100 %."""
    results = evaluation.results(None)
    figure = Figure()
    codes = draw_compliance(figure, results, THRESHOLDS, "Red, agosto 2026")
    figure.canvas.draw()
    ax = figure.axes[0]
    assert codes == tuple(r.indicator_code for r in results)
    assert len(ax.patches) == sum(1 for r in results if r.status.is_evaluated)
    labels = [t.get_text() for t in ax.get_yticklabels()]
    assert labels[0] == "CG01  Evaluaciones preventivas"
    assert "Línea base" in [t.get_text() for t in ax.texts]
    assert "100 %" in [t.get_text() for t in ax.get_xticklabels()]


def test_progress_chart_handles_bands_stock_and_missing_months(evaluation: PeriodEvaluation) -> None:
    """El gráfico de avance mensual funciona con tramos, stock, línea base y meses no informados, que aparecen en la
    leyenda."""
    for code, site in (("CG06", "SUR"), ("CG10", None), ("CG01", "ORI"), ("PC01", None), ("IA01", None)):
        figure = Figure()
        draw_progress(figure, evaluation.result(code, site))
        figure.canvas.draw()
        assert figure.axes[0].get_title(loc="left").startswith(code)
    missing = Figure()
    draw_progress(missing, evaluation.result("CG01", "ORI"))
    legend = missing.legends[0]
    assert "Mes no informado" in [t.get_text() for t in legend.get_texts()]


def test_site_comparison_and_heatmap(shared_services: Services) -> None:
    """La comparación incluye la red y todas las sedes y rotula las que no aplican; el mapa de calor cubre los 24
    indicadores."""
    monitor = shared_services.monitor
    figure = Figure()
    levels = draw_site_comparison(figure, monitor.site_comparison(AUG, "CG04"), THRESHOLDS, "SUR")
    figure.canvas.draw()
    assert levels == (None, "NOR", "SUR", "CEN", "ORI", "PON")
    assert "No aplica" in [t.get_text() for t in figure.axes[0].texts]
    heat = Figure()
    layout = draw_heatmap(heat, monitor.status_matrix(AUG), "CG01", None)
    heat.canvas.draw()
    assert layout.indicator_codes[0] == "CG01"
    assert layout.site_codes[0] is None
    assert len(layout.indicator_codes) == 24


# Componentes Qt


def test_record_table_model_uses_cells_for_text_sort_and_color() -> None:
    """El modelo de tabla usa cada celda para el texto, el valor de orden, la ayuda, el color y la negrita."""
    model = RecordTableModel([cell_column("value", "Valor", numeric=True)], row_bold=lambda row: row["bold"])
    model.set_rows(
        [{"value": Cell("1.234", 1234.0, "#DCEFDC", "ayuda"), "bold": True}, {"value": Cell("-", None), "bold": False}]
    )
    first = model.index(0, 0)
    assert model.data(first) == "1.234"
    assert model.data(first, Qt.ItemDataRole.UserRole) == 1234.0
    assert model.data(first, Qt.ItemDataRole.ToolTipRole) == "ayuda"
    assert model.data(first, Qt.ItemDataRole.BackgroundRole).color().name().upper() == "#DCEFDC"
    assert model.data(first, Qt.ItemDataRole.FontRole).bold()
    assert model.data(model.index(1, 0), Qt.ItemDataRole.UserRole) == "-"
    assert model.data(model.index(1, 0), Qt.ItemDataRole.FontRole) is None


def test_column_widths_fill_the_table_or_keep_their_content() -> None:
    """Si sobra espacio, se reparte en proporción al ancho de cada columna; si falta, cada columna conserva el ancho
    de su contenido (nunca se recorta una columna para que quepa)."""
    assert distribute_widths([100, 50, 50], 400) == [200, 100, 100]
    assert sum(distribute_widths([97, 53, 61], 500)) == 500
    assert distribute_widths([300, 200], 400) == [300, 200]
    assert distribute_widths([], 400) == []


def test_table_fills_its_width_and_aligns_headers_with_their_column() -> None:
    """Una tabla ancha llena la vista sin barra horizontal y los encabezados numéricos se alinean a la derecha."""
    model = RecordTableModel([cell_column("name", "Indicador"), cell_column("value", "Valor", numeric=True)])
    model.set_rows([{"name": Cell("CG01  Evaluaciones"), "value": Cell("94,2 %", 0.942)}])
    view = make_table_view(model)
    view.resize(600, 200)
    view.show()
    QApplication.processEvents()
    header = view.horizontalHeader()
    assert header.length() == view.viewport().width()
    assert header.sectionSize(0) > header.sectionSize(1)
    right = model.headerData(1, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole)
    left = model.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole)
    assert right & int(Qt.AlignmentFlag.AlignRight)
    assert left & int(Qt.AlignmentFlag.AlignLeft)
    view.close()


def test_view_switch_alternates_pages() -> None:
    """El selector entre gráficos y tabla alterna entre las dos vistas."""
    chart, table = QWidget(), QWidget()
    switch = ViewSwitch([("Gráfico", chart), ("Tabla", table)])
    assert switch.current_page() == 0
    switch.buttons[1].click()
    assert switch.stack.currentWidget() is table
    switch.set_page(0)
    assert switch.buttons[0].isChecked()
    assert switch.stack.currentWidget() is chart


def _run_worker(fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    runner = TaskRunner()
    outcome: dict[str, Any] = {}
    worker = Worker(fn, **kwargs)
    runner.start(
        worker,
        lambda result: outcome.setdefault("finished", result),
        lambda message: outcome.setdefault("failed", message),
        on_cancelled=lambda message: outcome.setdefault("cancelled", message),
    )
    assert runner.wait_idle(10_000)
    return outcome


def test_worker_reports_result_error_and_cancellation() -> None:
    """Las tareas en segundo plano informan el resultado, los errores esperados y la cancelación; un error
    inesperado se muestra con un mensaje genérico."""
    assert _run_worker(lambda: 42) == {"finished": 42}

    def invalid() -> None:
        raise ValidationError("Dato inválido.")

    assert _run_worker(invalid) == {"failed": "Dato inválido."}

    def cancelled() -> None:
        raise OperationCancelledError("La exportación fue cancelada.")

    assert _run_worker(cancelled) == {"cancelled": "La exportación fue cancelada."}

    def unexpected() -> None:
        raise RuntimeError("detalle técnico")

    assert "error inesperado" in _run_worker(unexpected)["failed"]


def test_worker_passes_progress_and_cancel_event() -> None:
    """Las tareas en segundo plano reciben la función de avance y el evento de cancelación."""
    seen: list[tuple[int, str]] = []

    def job(progress: Callable[[int, str], None], cancel: threading.Event) -> bool:
        progress(50, "mitad")
        return cancel.is_set()

    runner = TaskRunner()
    result: dict[str, Any] = {}
    worker = Worker(job, with_progress=True)
    runner.start(
        worker, lambda value: result.setdefault("value", value), lambda _m: None, lambda p, m: seen.append((p, m))
    )
    assert runner.wait_idle(10_000)
    assert result == {"value": False}
    assert seen == [(50, "mitad")]


# Ventana principal


def test_window_shows_totals_and_every_tab_has_data(window: MainWindow) -> None:
    """La ventana principal muestra los totales del período y cada una de las siete pestañas tiene datos sin
    problemas."""
    assert window.kpis.card("index").value_text == "94,2 %"
    assert window.kpis.card("green").value_text == "7"
    assert window.scope_title_label.text() == "Red, agosto 2026"
    assert window.quality_label.text().startswith("Datos recibidos: 98,3 %")
    assert window.tabs.count() == 7
    for index, view in enumerate(window.views):
        window.tabs.setCurrentIndex(index)
        QApplication.processEvents()
        assert view.problems() == [], window.tabs.tabText(index)
    window.tabs.setCurrentIndex(0)


def test_navigation_from_a_chart_opens_the_monthly_progress(window: MainWindow) -> None:
    """Navegar desde un gráfico selecciona el indicador y la sede, y puede abrir el avance mensual."""
    window._navigate("CG06", "SUR", True)
    assert window.wait_until_idle()
    assert window.tabs.currentWidget() is window.progress_tab
    assert window.selected_site() == "SUR"
    assert window.indicator_combo.currentData() == "CG06"
    progress = window.progress_tab
    assert isinstance(progress, ProgressTab)
    assert dict(progress.info.pairs)["Nivel"] == "Sede Sur"
    assert progress.chart.has_data
    window._navigate("CG01", KEEP_SITE, False)
    assert window.selected_site() == "SUR"
    window._navigate("CG01", None, False)
    window.tabs.setCurrentIndex(0)
    assert window.selected_site() is None


def test_navigation_to_an_indicator_outside_the_program_filter(window: MainWindow) -> None:
    """Navegar a un indicador de otro programa quita el filtro de programa para poder mostrarlo."""
    window.program_combo.setCurrentIndex(window.program_combo.findData("IA"))
    assert window.wait_until_idle()
    assert window.indicator_combo.count() == 3
    window._navigate("CG02", KEEP_SITE, False)
    assert window.selected_program() is None
    assert window.indicator_combo.currentData() == "CG02"


def test_changing_the_month_recalculates_in_the_background(window: MainWindow) -> None:
    """Cambiar el mes de corte recalcula la evaluación en segundo plano y actualiza el encabezado."""
    window.month_combo.setCurrentIndex(0)
    assert window.wait_until_idle()
    assert window.scope_title_label.text() == "Red, enero 2026"
    assert window.services.monitor.is_evaluated(Period(2026, 1))
    window.month_combo.setCurrentIndex(7)
    assert window.wait_until_idle()
    assert window.scope_title_label.text() == "Red, agosto 2026"


def test_comparison_and_data_tabs_follow_the_selection(window: MainWindow) -> None:
    """Las pestañas de comparación y de datos siguen el indicador y la sede elegidos, y el filtro de no reportados
    funciona."""
    window._navigate("CG04", "PON", False)
    comparison = next(v for v in window.views if isinstance(v, ComparisonTab))
    window.tabs.setCurrentWidget(comparison)
    QApplication.processEvents()
    assert comparison.model.rowCount() == 6
    data = next(v for v in window.views if isinstance(v, DataTab))
    window.tabs.setCurrentWidget(data)
    QApplication.processEvents()
    assert data.model.rowCount() > 0
    assert {row["record"].site_code for row in data.model.rows()} == {"PON"}
    data.missing_check.setChecked(True)
    assert all(not row["record"].reported for row in data.model.rows())
    data.missing_check.setChecked(False)
    window._navigate("CG01", None, False)
    window.tabs.setCurrentIndex(0)


def test_observation_dialog_validates_and_saves(editable_window: MainWindow) -> None:
    """El diálogo de registro muestra los errores dentro del diálogo, guarda el dato como carga manual y obliga a
    recalcular."""
    dialog = editable_window.observation_dialog(None)
    target = ObservationTarget("NOR", "CG02", 2026, 10)
    dialog.site_combo.setCurrentIndex(dialog.site_combo.findData(target.site_code))
    dialog.indicator_combo.setCurrentIndex(dialog.indicator_combo.findData(target.indicator_code))
    dialog.month_combo.setCurrentIndex(target.month - 1)
    assert dialog.target() == target
    assert dialog.denominator_edit.isEnabled()
    dialog.numerator_edit.setText("abc")
    dialog.denominator_edit.setText("120")
    assert dialog.try_save() is False
    assert "no es un número válido" in dialog.error_label.text()
    dialog.numerator_edit.setText("1.050")
    dialog.denominator_edit.setText("1.200")
    assert dialog.try_save() is True
    rows = editable_window.services.data.observations(2026, 10, "NOR", "CG02")
    assert [(r.numerator, r.denominator, r.origin) for r in rows] == [(1050.0, 1200.0, "Carga manual")]
    assert not editable_window.services.monitor.is_evaluated(AUG)


def test_observation_dialog_disables_fixed_denominators(editable_window: MainWindow) -> None:
    """El diálogo de registro desactiva el denominador en metas fijas y el numerador en meses no reportados."""
    dialog = editable_window.observation_dialog(None)
    dialog.indicator_combo.setCurrentIndex(dialog.indicator_combo.findData("CG01"))
    assert not dialog.denominator_edit.isEnabled()
    dialog.reported_check.setChecked(False)
    assert not dialog.numerator_edit.isEnabled()
    assert dialog.try_save() is True
    target = dialog.target()
    rows = editable_window.services.data.observations(target.year, target.month, target.site_code, "CG01")
    assert rows[0].reported is False


def test_settings_dialog_rejects_inverted_thresholds(editable_window: MainWindow) -> None:
    """El diálogo de umbrales rechaza un amarillo que no es menor que el verde y, al guardar, actualiza la leyenda y
    las tarjetas."""
    dialog = editable_window.settings_dialog()
    dialog.yellow_spin.setValue(100.0)
    assert dialog.try_save() is False
    assert dialog.error_label.isVisibleTo(dialog)
    dialog.yellow_spin.setValue(80.0)
    assert dialog.try_save() is True
    editable_window.settings_saved()
    assert editable_window.wait_until_idle()
    assert editable_window.services.catalog.settings().thresholds.yellow == pytest.approx(0.8)
    assert editable_window.threshold_labels[1].text() == "Amarillo: desde 80 %"
    assert editable_window.kpis.card("red").caption_text == "bajo 80 %"


def test_import_from_the_window_with_review(
    editable_window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    quiet_dialogs: list[tuple[str, str]],
) -> None:
    """La importación desde la ventana muestra las filas rechazadas y permite importar solo las válidas."""
    path = tmp_path / "carga.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["sede", "indicador", "año", "mes", "numerador", "denominador", "reportado"])
    sheet.append(["NOR", "CG02", 2026, 9, 100, 120, "Sí"])
    sheet.append(["XXX", "CG02", 2026, 9, 100, 120, "Sí"])
    workbook.save(path)
    reviewed: list[ImportPreviewDialog] = []

    def accept_valid_rows(dialog: ImportPreviewDialog) -> bool:
        reviewed.append(dialog)
        dialog.choice = ImportPreviewDialog.IMPORT_VALID
        return True

    monkeypatch.setattr(main_window_module, "run_modal", accept_valid_rows)
    editable_window.start_import(path)
    assert editable_window.wait_until_idle()
    assert reviewed
    assert reviewed[0].model.rowCount() == 1
    assert reviewed[0].valid_button.isVisibleTo(reviewed[0])
    assert not reviewed[0].import_button.isEnabled()
    rows = editable_window.services.data.observations(2026, 9)
    assert [(r.site_code, r.indicator_code, r.origin) for r in rows] == [("NOR", "CG02", "Importado")]
    assert quiet_dialogs[-1][0] == "info"
    assert "Se importaron 1 fila" in quiet_dialogs[-1][1]


def test_report_export_runs_in_the_background(
    editable_window: MainWindow, tmp_path: Path, quiet_dialogs: list[tuple[str, str]]
) -> None:
    """La exportación del reporte corre en segundo plano, desactiva el botón mientras trabaja y avisa dónde quedó el
    archivo."""
    target = tmp_path / "reporte.xlsx"
    editable_window.run_report(AUG, target, "xlsx")
    assert not editable_window.excel_button.isEnabled()
    assert editable_window.wait_until_idle()
    assert target.exists()
    assert editable_window.excel_button.isEnabled()
    assert quiet_dialogs[-1] == ("saved", str(target))


def test_cancelled_export_restores_the_window(editable_window: MainWindow, tmp_path: Path) -> None:
    """Una exportación cancelada deja la ventana utilizable e informa el resultado en la barra de estado."""
    worker = editable_window.run_report(AUG, tmp_path / "reporte.pdf", "pdf")
    worker.cancel()
    assert editable_window.wait_until_idle()
    assert editable_window.pdf_button.isEnabled()
    assert editable_window.statusBar().currentMessage() in (
        "La exportación fue cancelada.",
        f"Archivo guardado en {tmp_path / 'reporte.pdf'}",
    )


def test_chart_hover_and_click_select_the_indicator(window: MainWindow) -> None:
    """Pasar el puntero sobre una barra muestra su detalle y hacer clic selecciona el indicador sin cambiar de
    pestaña."""
    compliance = window.views[0]
    window.tabs.setCurrentWidget(compliance)
    QApplication.processEvents()
    axes = compliance.chart.figure.axes[0]
    compliance._on_chart_hover(SimpleNamespace(inaxes=axes, ydata=5.1))
    assert compliance.chart._tooltip is not None
    assert compliance.chart._tooltip.startswith("CG06 Tiempo de espera (días), Red")
    compliance._on_chart_hover(SimpleNamespace(inaxes=None, ydata=None))
    assert compliance.chart._tooltip is None
    compliance._on_chart_click(SimpleNamespace(inaxes=axes, ydata=2.0, dblclick=False))
    assert window.indicator_combo.currentData() == "CG03"
    assert window.tabs.currentWidget() is compliance
    window._navigate("CG01", KEEP_SITE, False)


def test_other_periods_render_without_problems(window: MainWindow) -> None:
    """Otros períodos, como un año cerrado o enero, se dibujan sin problemas."""
    for year, month in ((2024, 12), (2026, 1)):
        window.year_combo.setCurrentIndex(window.year_combo.findData(year))
        window.month_combo.setCurrentIndex(month - 1)
        assert window.wait_until_idle()
        assert window.scope_title_label.text().endswith(f"{year}")
        for index, view in enumerate(window.views[:3]):
            window.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            for _name, chart in view.charts():
                chart.draw_now()
    window.year_combo.setCurrentIndex(window.year_combo.findData(2026))
    window.month_combo.setCurrentIndex(7)
    assert window.wait_until_idle()
    window.tabs.setCurrentIndex(0)
