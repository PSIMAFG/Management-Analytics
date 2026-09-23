"""Catálogo: búsquedas por código, validación de pesos y metas, y efecto de un catálogo incoherente."""

from __future__ import annotations

from dataclasses import replace

import pytest

from helpers import result
from kpi_monitor.data.catalog_seed import WEIGHTS_2026
from kpi_monitor.domain.catalog import Catalog, validate_catalog
from kpi_monitor.domain.engine import EngineConfig, PeriodEvaluation, evaluate_period
from kpi_monitor.domain.enums import Status
from kpi_monitor.domain.index import weighted_index
from kpi_monitor.domain.models import Observation, Period
from kpi_monitor.domain.results import StatusCounts
from kpi_monitor.domain.summary import MonitorSummary
from kpi_monitor.errors import OperationCancelledError, ValidationError


def test_lookups_by_code(catalog: Catalog) -> None:
    """Las búsquedas por código devuelven sedes, programas, indicadores y metas fijas; un código inexistente da un
    error legible."""
    assert catalog.site("NOR").name == "Sede Norte"
    assert catalog.site_name(None) == "Red"
    assert catalog.program("IA").name == "Índice de actividad"
    assert catalog.indicator("CG06").short_name.startswith("Tiempo de espera")
    assert catalog.target("CG04", 2026, "SUR") is None
    assert catalog.target("CG04", 2026, "NOR") is not None
    for lookup in (catalog.site, catalog.program, catalog.indicator):
        with pytest.raises(ValidationError, match="no existe"):
            lookup("XX")


def test_active_indicators_follow_program_order(catalog: Catalog) -> None:
    """Los indicadores vigentes de un año siguen el orden de programas e indicadores; en 2024 aún no existían todos."""
    codes = [ind.code for ind in catalog.active_indicators(2026)]
    assert codes[0] == "CG01"
    assert codes[15] == "CG16"
    assert codes[16:21] == ["PC01", "PC02", "PC03", "PC04", "PC05"]
    assert codes[-3:] == ["IA01", "IA02", "IA03"]
    assert len(catalog.active_indicators(2024)) == 18


def test_weights_that_do_not_add_up_are_reported(catalog: Catalog) -> None:
    """Regla: los pesos de un programa se validan contra una única tabla por año y deben sumar 100 %."""
    rules = tuple(replace(r, weight=0.5) if (r.indicator_code, r.year) == ("CG01", 2026) else r for r in catalog.rules)
    broken = replace(catalog, rules=rules)
    problems = validate_catalog(broken, 2026)
    assert len(problems) == 1
    assert "CG" in problems[0]
    assert "100 %" in problems[0]
    assert validate_catalog(broken, 2025) == []


def test_fixed_goal_indicator_without_site_goals_is_reported(catalog: Catalog) -> None:
    """Regla: un indicador de meta fija por sede sin metas asignadas en el año se informa como problema del
    catálogo."""
    goals = tuple(g for g in catalog.site_goals if not (g.indicator_code == "CG04" and g.year == 2026))
    problems = validate_catalog(replace(catalog, site_goals=goals), 2026)
    assert any("CG04" in p for p in problems)


def test_evaluation_refuses_an_incoherent_catalog(
    catalog: Catalog, observations: list[Observation], engine_config: EngineConfig
) -> None:
    """Regla: la evaluación se niega a correr con pesos que no suman 100 % en vez de producir un índice engañoso."""
    rules = tuple(replace(r, weight=0.5) if (r.indicator_code, r.year) == ("CG01", 2026) else r for r in catalog.rules)
    with pytest.raises(ValidationError, match="pesos"):
        evaluate_period(replace(catalog, rules=rules), observations, Period(2026, 8), engine_config)


def test_year_without_indicators_is_rejected(
    catalog: Catalog, observations: list[Observation], engine_config: EngineConfig
) -> None:
    """Evaluar un año sin indicadores configurados da un error de validación legible."""
    with pytest.raises(ValidationError, match="No hay indicadores"):
        evaluate_period(catalog, observations, Period(2030, 1), engine_config)


def test_cancellation_is_honoured(
    catalog: Catalog, observations: list[Observation], engine_config: EngineConfig
) -> None:
    """La evaluación de un período se detiene cuando se pide cancelarla."""
    with pytest.raises(OperationCancelledError):
        evaluate_period(catalog, observations, Period(2026, 8), engine_config, should_cancel=lambda: True)


def test_share_of_weight_without_data_is_never_negative() -> None:
    """Regresión: con un indicador que no aplica, el ruido binario de los pesos daba "-0,0 %" sin datos."""
    weights = {k: v for k, v in WEIGHTS_2026.items() if k.startswith("CG")}
    rows = [
        result(Status.NOT_APPLICABLE if code == "CG04" else Status.GREEN, None if code == "CG04" else 1.0, w, code=code)
        for code, w in weights.items()
    ]
    index = weighted_index(rows, "P1", None)
    assert index.weight_with_data / index.weight_evaluable >= 1.0
    assert index.pct_weight_without_data == 0.0
    assert index.coverage == 1.0


def test_monitor_summary_properties(evaluation: PeriodEvaluation) -> None:
    """El resumen de la franja de totales expone el índice, la proyección y el peso sin datos, y también funciona
    sin índice."""
    index = evaluation.index("CG", None)
    counts = evaluation.status_counts(None, "CG")
    summary = MonitorSummary(evaluation.period, None, "CG", index, counts, 10, 4, 2)
    assert summary.index_value == index.value
    assert summary.projected_index == index.projected
    assert summary.pct_weight_without_data == index.pct_weight_without_data
    empty = MonitorSummary(evaluation.period, None, None, None, StatusCounts(), 0, 0, 0)
    assert empty.index_value is None
    assert empty.counts.total == 0


def test_status_counts_group_undetermined_with_no_data() -> None:
    """El conteo de sin datos agrupa los indicadores sin observaciones y los de meta no determinable."""
    counts = StatusCounts({Status.NO_DATA: 2, Status.UNDETERMINED: 1, Status.GREEN: 3})
    assert counts.no_data == 3
    assert counts.green == 3
    assert counts.total == 6
