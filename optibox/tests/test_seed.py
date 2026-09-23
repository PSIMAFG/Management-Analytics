"""Generador sintético: determinismo y calibración de la estructura."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from factories import MONDAY, REFERENCE
from optibox.data import db
from optibox.data.seed import FIRST_NAMES, SURNAMES, demo_week, generate_master_data, seed_demo_data
from optibox.domain.instance import build_instance
from optibox.domain.models import AbsenceStatus, Audience

MASTER_TABLES = (
    "role",
    "staff",
    "contract",
    "service_type",
    "role_service",
    "service_room",
    "staff_skill",
    "staff_target",
    "staff_room",
    "room",
    "availability",
    "absence",
    "holiday",
    "center_hours",
    "blocking",
    "demand",
    "scenario",
    "scenario_weight",
    "setting",
)


def _dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() for table in MASTER_TABLES}
    finally:
        conn.close()


def _seed(path: Path) -> Path:
    with db.session(path) as conn:
        db.initialize(conn)
        seed_demo_data(conn, REFERENCE)
    return path


def test_generator_is_deterministic() -> None:
    assert generate_master_data(REFERENCE) == generate_master_data(REFERENCE)


def test_seeded_databases_are_identical(tmp_path: Path) -> None:
    first = _dump(_seed(tmp_path / "a.db"))
    second = _dump(_seed(tmp_path / "b.db"))
    assert first == second
    assert first["demand"]


def test_another_seed_changes_the_data() -> None:
    base = generate_master_data(REFERENCE)
    other = generate_master_data(REFERENCE, seed=7)
    assert base.demand != other.demand
    assert [s.code for s in base.staff] == [s.code for s in other.staff]


def test_demo_week_is_the_following_monday() -> None:
    assert demo_week(REFERENCE) == MONDAY
    assert demo_week(date(2026, 10, 5)) == date(2026, 10, 12)


def test_staff_calibration() -> None:
    master = generate_master_data(REFERENCE)
    roles = Counter(person.role_code for person in master.staff)
    assert roles == Counter({"TO": 4, "FO": 3, "KI": 1, "PS": 2, "TS": 1, "AD": 1})
    hours = {contract.weekly_minutes // 60 for person in master.staff for contract in person.contracts}
    assert hours <= {18, 22, 30, 32, 44}
    assert sum(1 for person in master.staff if len(person.contracts) == 2) == 2
    for person in master.staff:
        first, *rest = person.name.split(" ")
        assert first in FIRST_NAMES
        assert all(part in SURNAMES for part in rest)
    assert len({person.name for person in master.staff}) == len(master.staff)
    non_service = [role for role in master.roles if not role.delivers_services]
    assert [role.code for role in non_service] == ["AD"]


def test_rooms_services_and_scenarios_calibration() -> None:
    master = generate_master_data(REFERENCE)
    assert len(master.rooms) == 9
    admin_rooms = [room for room in master.rooms if room.is_admin_room]
    assert len(admin_rooms) == 1
    assert (admin_rooms[0].simultaneous_hard, admin_rooms[0].soft_capacity) == (10, 8)
    services = {service.code: service for service in master.service_types}
    assert services["AIN"].duration_min == 45
    assert services["EIN"].duration_min == 120 and services["EIN"].requires_evaluation_room
    assert services["EIN"].admin_minutes == 60
    assert services["TGR"].duration_min == 90 and services["TGR"].requires_group_room
    assert services["TGR"].participants > 1
    assert services["TGR"].max_week_total == 2
    assert len(master.scenarios) == 3
    assert len({scenario.name for scenario in master.scenarios}) == 3


def test_blockings_absences_and_holiday_calibration() -> None:
    master = generate_master_data(REFERENCE)
    audiences = {blocking.audience for blocking in master.blockings}
    assert audiences == {Audience.ALL, Audience.SERVICE_STAFF, Audience.ROLE, Audience.STAFF}
    preparation = [b for b in master.blockings if b.audience is Audience.ALL and b.start == 480]
    assert preparation and preparation[0].end == 495
    statuses = {absence.status for absence in master.absences}
    assert statuses == {AbsenceStatus.APPROVED, AbsenceStatus.PENDING, AbsenceStatus.REJECTED}
    assert any(not absence.full_day for absence in master.absences)
    assert all(MONDAY <= absence.day < MONDAY + timedelta(days=5) for absence in master.absences)
    assert len(master.holidays) == 1
    assert MONDAY + timedelta(days=7) <= master.holidays[0].day < MONDAY + timedelta(days=12)


def test_demand_magnitude_and_patterns() -> None:
    master = generate_master_data(REFERENCE)
    instance = build_instance(master, MONDAY, master.scenarios[0])
    per_day: Counter[int] = Counter()
    for item in instance.demand:
        per_day[item.weekday] += item.sessions
    assert 120 <= instance.total_demand <= 260
    assert all(15 <= per_day[day] <= 60 for day in range(5))
    assert any(item.service_code == "TGR" for item in instance.demand)
    assert {item.priority for item in instance.demand} == {1, 2, 3}
    assert instance.holiday_demand_dropped == 0
    holiday_week = build_instance(master, MONDAY + timedelta(days=7), master.scenarios[0])
    assert holiday_week.holiday_demand_dropped > 0
