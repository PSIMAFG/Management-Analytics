"""Índice ponderado: tope por indicador, cobertura de peso y exclusiones."""

from __future__ import annotations

import pytest

from helpers import result
from kpi_monitor.domain.enums import Status
from kpi_monitor.domain.index import weighted_index


def test_each_indicator_is_capped_at_its_weight() -> None:
    """Regla: cada indicador aporta como máximo su peso completo; el sobrecumplimiento se topa en 100 % dentro del
    índice."""
    rows = [result(Status.GREEN, 1.4, 0.5, code="A"), result(Status.YELLOW, 0.9, 0.5, code="B")]
    index = weighted_index(rows, "P1", None)
    assert index.value == pytest.approx(0.5 * 1.0 + 0.5 * 0.9)
    assert index.indicators_counted == 2


def test_indicators_without_data_are_excluded_and_reported() -> None:
    """Regla: un indicador sin dato se excluye del denominador del índice ponderado y su peso se informa aparte."""
    rows = [
        result(Status.RED, 0.8, 0.5, code="A"),
        result(Status.NO_DATA, None, 0.3, code="B"),
        result(Status.UNDETERMINED, None, 0.2, code="C"),
    ]
    index = weighted_index(rows, "P1", None)
    assert index.value == pytest.approx(0.8)
    assert index.weight_with_data == pytest.approx(0.5)
    assert index.weight_without_data == pytest.approx(0.3)
    assert index.weight_undetermined == pytest.approx(0.2)
    assert index.pct_weight_without_data == pytest.approx(0.5)
    assert index.coverage == pytest.approx(0.5)


def test_baseline_and_not_applicable_are_outside_the_evaluable_weight() -> None:
    """Regla: los indicadores de línea base y los que no aplican quedan fuera del peso evaluable y no cuentan como
    peso sin datos."""
    rows = [
        result(Status.GREEN, 1.0, 0.6, code="A"),
        result(Status.BASELINE, None, 0.1, code="B"),
        result(Status.NOT_APPLICABLE, None, 0.3, code="C"),
    ]
    index = weighted_index(rows, "P1", None)
    assert index.value == pytest.approx(1.0)
    assert index.weight_evaluable == pytest.approx(0.6)
    assert index.pct_weight_without_data == pytest.approx(0.0)


def test_projected_index_uses_projected_compliance() -> None:
    """El índice proyectado usa el cumplimiento proyectado al cierre de cada indicador, con el mismo tope de 100 %."""
    rows = [
        result(Status.GREEN, 1.0, 0.5, projected=1.2, code="A"),
        result(Status.YELLOW, 0.9, 0.5, projected=0.7, code="B"),
    ]
    index = weighted_index(rows, "P1", None)
    assert index.projected == pytest.approx(0.5 * 1.0 + 0.5 * 0.7)


def test_index_filters_program_and_level() -> None:
    """El índice ponderado se calcula por programa y por nivel (red o sede) sin mezclar resultados."""
    rows = [
        result(Status.GREEN, 1.0, 1.0, program="P1", code="A"),
        result(Status.RED, 0.5, 1.0, program="P2", code="B"),
        result(Status.RED, 0.2, 1.0, program="P1", site="S1", code="A"),
    ]
    assert weighted_index(rows, "P1", None).value == pytest.approx(1.0)
    assert weighted_index(rows, "P2", None).value == pytest.approx(0.5)
    assert weighted_index(rows, "P1", "S1").value == pytest.approx(0.2)


def test_index_without_evaluated_indicators_is_none() -> None:
    """Sin indicadores evaluados el índice queda sin valor y todo el peso se informa como sin datos."""
    index = weighted_index([result(Status.NO_DATA, None, 1.0)], "P1", None)
    assert index.value is None
    assert index.pct_weight_without_data == pytest.approx(1.0)
