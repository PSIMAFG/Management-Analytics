"""Acumulación dentro del año: razón de sumas, stocks, multiplicador, población y faltantes."""

from __future__ import annotations

import pytest

from helpers import indicator, monthly, series
from kpi_monitor.domain.aggregation import Aggregate, aggregate_network, aggregate_site, denominator_stock
from kpi_monitor.domain.enums import Aggregation, DenominatorType

PREVALENCE = 0.22


def test_cumulative_is_ratio_of_sums_not_average_of_ratios() -> None:
    """Regla: el acumulado del año es la suma de numeradores sobre la suma de denominadores, nunca el promedio de
    los porcentajes mensuales."""
    data = series(rows=monthly(2026, {1: (1, 2), 2: (9, 10)}))
    agg = aggregate_site(indicator(), data, 2026, 2, PREVALENCE)
    assert agg.value == pytest.approx(10 / 12)
    assert agg.value != pytest.approx((1 / 2 + 9 / 10) / 2)


def test_fixed_denominator_uses_site_target() -> None:
    """Regla: el denominador de meta fija por sede viene siempre de la configuración, nunca se infiere de la serie."""
    ind = indicator(denominator=DenominatorType.FIXED_SITE)
    data = series(rows=monthly(2026, {1: (100, None), 2: (150, None)}), targets={2026: 1200})
    agg = aggregate_site(ind, data, 2026, 2, PREVALENCE)
    assert agg.numerator == 250
    assert agg.denominator == 1200


def test_stock_numerator_uses_latest_cut_and_is_never_summed() -> None:
    """Regla: un numerador de stock (personas en seguimiento) usa el último corte del año, nunca la suma de meses."""
    ind = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.FIXED_SITE)
    data = series(rows=monthly(2026, {3: (30, None), 6: (32, None)}), targets={2026: 40})
    agg = aggregate_site(ind, data, 2026, 8, PREVALENCE)
    assert agg.numerator == 32
    assert agg.stock.month == 6
    assert agg.value == pytest.approx(0.8)
    early = aggregate_site(ind, data, 2026, 4, PREVALENCE)
    assert early.numerator == 30


def test_stock_numerator_is_not_carried_from_previous_year() -> None:
    """Regla: un numerador de stock no se arrastra desde el año anterior; sin corte en el año queda sin dato."""
    ind = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.FIXED_SITE)
    data = series(rows=monthly(2025, {12: (30, None)}), targets={2026: 40, 2025: 40})
    agg = aggregate_site(ind, data, 2026, 2, PREVALENCE)
    assert agg.numerator is None


def test_stock_denominator_uses_latest_cut_up_to_the_month() -> None:
    """Regla: un denominador de stock usa el último corte disponible hasta el mes evaluado, mientras el numerador de
    flujo se suma."""
    ind = indicator(denominator=DenominatorType.STOCK)
    rows = monthly(2026, dict.fromkeys(range(1, 9), (10, None)))
    rows[(2026, 3)] = (10, 50)
    rows[(2026, 6)] = (10, 60)
    data = series(rows=rows)
    assert aggregate_site(ind, data, 2026, 5, PREVALENCE).denominator == 50
    assert aggregate_site(ind, data, 2026, 8, PREVALENCE).denominator == 60
    assert aggregate_site(ind, data, 2026, 8, PREVALENCE).numerator == 80


def test_stock_denominator_carries_last_cut_of_previous_year() -> None:
    """Regla: antes del primer corte del año, el denominador de stock arrastra el último corte del año anterior y lo
    marca como arrastrado."""
    ind = indicator(denominator=DenominatorType.STOCK)
    rows = {(2025, 12): (10, 45), (2026, 1): (8, None), (2026, 2): (7, None), (2026, 3): (9, 50)}
    data = series(rows=rows)
    agg = aggregate_site(ind, data, 2026, 2, PREVALENCE)
    assert agg.denominator == 45
    assert agg.stock.carried
    assert agg.stock.month == 12


def test_stock_denominator_is_never_filled_backwards() -> None:
    """Regla: un stock solo se arrastra hacia adelante; los meses previos al primer corte quedan sin denominador."""
    ind = indicator(denominator=DenominatorType.STOCK)
    data = series(rows={(2026, 1): (8, None), (2026, 2): (7, None), (2026, 3): (9, 50)})
    reading = denominator_stock(data, 2026, 2)
    assert reading.value is None
    assert aggregate_site(ind, data, 2026, 2, PREVALENCE).value is None


def test_multiplier_is_applied_once_on_raw_people() -> None:
    """Regla: el multiplicador k se aplica una sola vez sobre el stock crudo de personas, no en cascada."""
    ind = indicator(denominator=DenominatorType.K_STOCK, multiplier=7.0)
    data = series(rows={(2026, 3): (70, 20)})
    agg = aggregate_site(ind, data, 2026, 3, PREVALENCE)
    assert agg.denominator == pytest.approx(140)
    assert agg.value == pytest.approx(0.5)


def test_population_denominator_uses_prevalence() -> None:
    """Regla: el denominador poblacional es la población de referencia de la sede multiplicada por la prevalencia."""
    ind = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.POPULATION)
    data = series(rows={(2026, 6): (550, None)}, populations={2026: 10000})
    agg = aggregate_site(ind, data, 2026, 8, PREVALENCE)
    assert agg.denominator == pytest.approx(2200)
    assert agg.value == pytest.approx(0.25)


def test_missing_month_is_not_zero() -> None:
    """Regla: un mes sin bandera de reportado se excluye del acumulado, no cuenta como cero."""
    data = series(rows=monthly(2026, {1: (8, 10), 2: (9, 10), 3: None}))
    agg = aggregate_site(indicator(), data, 2026, 3, PREVALENCE)
    assert agg.months_reported == 2
    assert agg.value == pytest.approx(17 / 20)


def test_reported_zero_counts_as_data() -> None:
    """Regla: un cero informado es un dato válido y cuenta como mes reportado, a diferencia de un mes faltante."""
    data = series(rows=monthly(2026, {1: (0, 10), 2: (0, 12)}))
    agg = aggregate_site(indicator(), data, 2026, 2, PREVALENCE)
    assert agg.months_reported == 2
    assert agg.value == 0.0


def test_site_without_reports_has_no_numerator() -> None:
    """Una sede sin ningún mes reportado queda sin numerador ni dato, no con valor cero."""
    agg = aggregate_site(indicator(), series(rows={}), 2026, 5, PREVALENCE)
    assert agg.numerator is None
    assert not agg.has_data


def test_network_is_sum_of_sites_with_data() -> None:
    """Invariante: la red suma numeradores y denominadores solo de las sedes con dato completo e informa cuáles
    incluyó."""
    parts = [Aggregate(9, 10, 3), Aggregate(1, 10, 3), Aggregate(None, None, 0), Aggregate(5, None, 2)]
    network, included = aggregate_network(parts)
    assert included == (0, 1)
    assert network.numerator == 10
    assert network.denominator == 20
    assert network.value == pytest.approx(0.5)


def test_network_without_data() -> None:
    """Si ninguna sede tiene dato, la red queda sin valor y sin sedes incluidas."""
    network, included = aggregate_network([Aggregate(None, None, 0)])
    assert included == ()
    assert network.value is None
