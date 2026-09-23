"""Validaciones de los modelos del dominio y formato de textos."""

from __future__ import annotations

from decimal import Decimal

import pytest

from helpers import SHEET, indicator
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Direction, GoalRuleType, Scale
from kpi_monitor.domain.models import (
    GoalBand,
    GoalRule,
    Indicator,
    MonitorSettings,
    Observation,
    Period,
    SiteGoal,
    SitePopulation,
    Thresholds,
)
from kpi_monitor.domain.text import format_decimal, format_int, format_pct, format_value, join_months
from kpi_monitor.errors import ValidationError


def test_period_validates_month_and_moves_back() -> None:
    """El período de corte valida el mes y el año, retrocede cruzando el cambio de año y se rotula en español."""
    assert Period(2026, 1).previous() == Period(2025, 12)
    assert Period(2026, 8).previous() == Period(2026, 7)
    assert Period(2026, 8).label == "agosto 2026"
    with pytest.raises(ValidationError):
        Period(2026, 13)
    with pytest.raises(ValidationError):
        Period(1999, 5)


def test_indicator_requires_explicit_direction() -> None:
    """Regla: la dirección (mayor o menor es mejor) es un campo obligatorio del indicador, sin valor neutro."""
    with pytest.raises(ValidationError, match="dirección"):
        Indicator(
            code="X",
            program_code="P1",
            name="X",
            short_name="X",
            aggregation=Aggregation.FLOW,
            denominator_type=DenominatorType.FLOW,
            direction="neutro",  # type: ignore[arg-type]
            scale=Scale.PROPORTION,
            sheet=SHEET,
        )


def test_indicator_rejects_inconsistent_definitions() -> None:
    """Regla: la definición declarativa de un indicador se valida: multiplicador solo con k × stock, combinaciones
    de stock coherentes y cortes ordenados."""
    with pytest.raises(ValidationError, match="multiplicador"):
        indicator(denominator=DenominatorType.FLOW, multiplier=7.0)
    with pytest.raises(ValidationError, match="stock"):
        Indicator(
            code="X",
            program_code="P1",
            name="X",
            short_name="X",
            aggregation=Aggregation.STOCK,
            denominator_type=DenominatorType.FIXED_SITE,
            direction=Direction.HIGHER_IS_BETTER,
            scale=Scale.PROPORTION,
            sheet=SHEET,
        )
    with pytest.raises(ValidationError):
        indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.STOCK)
    with pytest.raises(ValidationError):
        indicator(cut_months=(7, 5))


def test_indicator_expected_months() -> None:
    """Un indicador de stock solo espera datos en sus meses de corte; uno de flujo, en todos los meses."""
    stock = indicator(aggregation=Aggregation.STOCK, denominator=DenominatorType.FIXED_SITE, stock_months=(6, 12))
    assert stock.expects_data_in(6)
    assert not stock.expects_data_in(7)
    assert indicator().expects_data_in(7)


def test_goal_rule_validation() -> None:
    """Cada tipo de regla de meta exige solo sus campos (valor, factor, techo o tramos) y el peso debe estar entre 0
    y 1."""
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.ABSOLUTE, 0.5)
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.RELATIVE_INCREASE, 0.5, factor=0.0)
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.CAPPED_INCREASE, 0.5, factor=0.2)
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.ABSOLUTE, 1.5, value=0.8)
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.ABSOLUTE, 0.5, value=0.8, bands=(GoalBand(None, 1.0),))


def test_bands_must_be_increasing_and_end_open() -> None:
    """Regla: los tramos deben tener cotas crecientes, terminar en un tramo abierto y usar porcentajes entre 0 y 100
    %."""
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.BANDS, 0.5, bands=(GoalBand(30.0, 1.0), GoalBand(20.0, 0.5)))
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.BANDS, 0.5, bands=(GoalBand(20.0, 1.0), GoalBand(30.0, 0.5)))
    with pytest.raises(ValidationError):
        GoalRule("X", 2026, GoalRuleType.BANDS, 0.5, bands=(GoalBand(20.0, 1.5), GoalBand(None, 0.0)))


def test_observation_validation() -> None:
    """Una observación no admite valores negativos ni valores en un mes no reportado, y el mes debe ser válido."""
    with pytest.raises(ValidationError):
        Observation("X", "A", 2026, 1, -1.0, 5.0)
    with pytest.raises(ValidationError):
        Observation("X", "A", 2026, 1, 1.0, None, reported=False)
    with pytest.raises(ValidationError):
        Observation("X", "A", 2026, 0, 1.0, 1.0)
    assert Observation("X", "A", 2026, 1, None, None, reported=False).key == ("X", "A", 2026, 1)


def test_positive_targets_populations_and_settings() -> None:
    """Las metas fijas y las poblaciones deben ser positivas, y los parámetros del semáforo, la prevalencia y el
    bootstrap se validan."""
    with pytest.raises(ValidationError):
        SiteGoal("X", 2026, "A", 0)
    with pytest.raises(ValidationError):
        SitePopulation("A", 2026, 0)
    with pytest.raises(ValidationError):
        Thresholds(green=0.8, yellow=0.9)
    with pytest.raises(ValidationError):
        MonitorSettings(prevalence=0.0)
    with pytest.raises(ValidationError):
        MonitorSettings(bootstrap_draws=10)


def test_chilean_number_formats() -> None:
    """Los textos usan formato chileno: punto de miles, coma decimal, porcentajes con espacio y meses en español."""
    assert format_int(12345678) == "12.345.678"
    assert format_decimal(1234.56, 1) == "1.234,6"
    assert format_decimal(Decimal("2.25"), 1) == "2,3"
    assert format_pct(0.853) == "85,3 %"
    assert format_pct(None) == "-"
    assert format_value(0.5, Scale.PROPORTION) == "50,0 %"
    assert format_value(8.642, Scale.RATE) == "8,64"
    assert format_value(23.47, Scale.DAYS) == "23,5 días"
    assert format_value(None, Scale.RATE) == "-"
    assert join_months([2, 5, 7]) == "febrero, mayo y julio"
    assert join_months([12]) == "diciembre"
