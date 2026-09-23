"""Grilla de tiempo: minutos, slots, bloques de demanda y horario del centro."""

from __future__ import annotations

from datetime import date

import pytest

from optibox.domain.timegrid import (
    DEFAULT_HOURS,
    DayHours,
    block_of,
    format_minute,
    format_range,
    mask_intervals,
    mask_minutes,
    mask_slots,
    parse_hhmm,
    require_aligned,
    slot_mask,
    slot_mask_outward,
    week_dates,
    week_monday,
)
from optibox.errors import ValidationError


def test_afternoon_hours_are_derived_from_minutes() -> None:
    """Regresión B19: la tarde no se corre una hora; la hora sale del minuto, no de un índice."""
    assert format_minute(840) == "14:00"
    assert format_minute(885) == "14:45"
    assert format_range(885, 930) == "14:45-15:30"
    assert format_minute(480) == "08:00"


@pytest.mark.parametrize(("text", "minute"), [("8:00", 480), ("08:15", 495), (" 14:00 ", 840), ("24:00", 1440)])
def test_parse_hhmm_accepts_valid_hours(text: str, minute: int) -> None:
    assert parse_hhmm(text) == minute


@pytest.mark.parametrize("text", ["8", "25:00", "08:60", "abc", "", "24:01", "8.30"])
def test_parse_hhmm_rejects_invalid_hours(text: str) -> None:
    """Regresión B24: una hora mal escrita se rechaza con un mensaje claro en vez de truncar el día."""
    with pytest.raises(ValidationError):
        parse_hhmm(text, "inicio")


def test_require_aligned() -> None:
    assert require_aligned(495, "inicio") == 495
    with pytest.raises(ValidationError):
        require_aligned(500, "inicio")


def test_slot_masks_round_inward_and_outward() -> None:
    assert mask_slots(slot_mask(540, 585)) == (540, 555, 570)
    assert slot_mask(550, 560) == 0
    assert mask_slots(slot_mask_outward(545, 553)) == (540,)
    assert mask_slots(slot_mask_outward(550, 560)) == (540, 555)
    assert mask_slots(slot_mask_outward(550, 580)) == (540, 555, 570)
    assert mask_minutes(slot_mask(540, 600)) == 60


def test_mask_intervals_groups_contiguous_slots() -> None:
    mask = slot_mask(480, 540) | slot_mask(600, 630)
    assert mask_intervals(mask) == ((480, 540), (600, 630))


def test_lunch_is_a_gap_that_sessions_cannot_cross() -> None:
    """Regresión B17: el almuerzo es un hueco real y nada lo cruza."""
    monday = DEFAULT_HOURS[0]
    assert monday.intervals == ((480, 780), (840, 1020))
    assert monday.open_minutes == 480
    assert monday.contains(735, 780)
    assert not monday.contains(750, 795)
    assert not monday.contains(780, 825)
    assert 765 in monday.slot_starts()
    assert 780 not in monday.slot_starts()
    assert 840 in monday.slot_starts()


def test_demand_blocks_follow_center_hours() -> None:
    assert DEFAULT_HOURS[0].blocks() == (480, 540, 600, 660, 720, 840, 900, 960)
    assert DEFAULT_HOURS[4].blocks() == (480, 540, 600, 660, 720, 840, 900)
    assert block_of(885) == 840


def test_day_hours_validation() -> None:
    with pytest.raises(ValidationError):
        DayHours(0, 480, 1020, 460, 520)
    with pytest.raises(ValidationError):
        DayHours(5, 480, 1020, 780, 840)
    with pytest.raises(ValidationError):
        DayHours(0, 485, 1020, 780, 840)
    no_lunch = DayHours(0, 480, 720, 720, 720)
    assert no_lunch.intervals == ((480, 720),)


def test_day_without_lunch_accepts_sessions_across_midday() -> None:
    """Si el almuerzo empieza y termina a la misma hora el día es un solo tramo y nada queda partido."""
    no_lunch = DayHours(0, 480, 1020, 780, 780)
    assert not no_lunch.has_lunch
    assert no_lunch.intervals == ((480, 1020),)
    assert no_lunch.contains(750, 795)
    assert no_lunch.open_minutes == 540
    assert 780 in no_lunch.slot_starts()
    assert 780 in no_lunch.blocks()


def test_week_helpers() -> None:
    assert week_monday(date(2026, 10, 8)) == date(2026, 10, 5)
    assert week_monday(date(2026, 10, 5)) == date(2026, 10, 5)
    dates = week_dates(date(2026, 10, 5))
    assert len(dates) == 5
    assert dates[-1] == date(2026, 10, 9)
