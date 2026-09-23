"""Métricas: definiciones explícitas calculadas desde las sesiones."""

from __future__ import annotations

from datetime import timedelta

import pytest

from factories import MONDAY, admin, make_instance, meeting, person, session, small_master
from optibox.domain.metrics import StaffLoad, compute_metrics, ratio
from optibox.domain.models import (
    Absence,
    AbsenceStatus,
    Audience,
    AvailabilityWindow,
    Blocking,
    DemandItem,
    Holiday,
    MasterData,
    Scenario,
)
from optibox.domain.plan import Plan


def test_attentions_are_sessions_times_participants() -> None:
    """Regresión B15: un taller de cupo 4 entrega 4 atenciones; no se cuentan slots."""
    master = small_master(demand=(DemandItem(0, 540, "IND", 2, 2), DemandItem(1, 900, "GRP", 1, 2)))
    instance = make_instance(master)
    plan = Plan(
        sessions=(
            session(instance, "A", start=540),
            session(instance, "B", room="R2", start=540),
            session(instance, "A", "GRP", "R2", 1, 900),
        ),
        admin=(admin("A", 0, 600, 615), admin("B", 0, 600, 615), admin("A", 1, 600, 630)),
    )
    totals = compute_metrics(instance, plan).totals
    assert totals.sessions == 3
    assert totals.attentions == 1 + 1 + 4
    assert plan.attentions == 6


def test_session_totals_exclude_admin_and_blockings() -> None:
    """Regresión B16: las horas de atención solo cuentan sesiones, no bloqueos ni administrativo."""
    master = small_master(blockings=(meeting(2, 540, 660, Audience.SERVICE_STAFF),))
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 645),))
    totals = compute_metrics(instance, plan).totals
    assert totals.session_min == 45
    assert totals.assigned_hours == pytest.approx(0.75)
    assert totals.admin_min == 45
    assert totals.admin_required_min == 15


def test_coverage_by_block_and_service() -> None:
    master = small_master(demand=(DemandItem(0, 540, "IND", 2, 1), DemandItem(0, 540, "FAM", 1, 2)))
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=585), session(instance, "B", start=600)),
        admin=(admin("A", 0, 660, 675), admin("B", 0, 660, 675)),
    )
    metrics = compute_metrics(instance, plan)
    by_key = {(r.day, r.block, r.service): r for r in metrics.coverage_by_block_service}
    assert by_key[(0, 540, "IND")].covered == 1
    assert by_key[(0, 540, "IND")].pct == pytest.approx(0.5)
    assert by_key[(0, 540, "FAM")].shortfall == 1
    block = next(r for r in metrics.coverage_by_block if (r.day, r.block) == (0, 540))
    assert (block.required, block.covered) == (3, 1)
    empty = next(r for r in metrics.coverage_by_block if (r.day, r.block) == (1, 540))
    assert empty.required == 0 and empty.pct is None
    totals = metrics.totals
    assert totals.coverage_pct == pytest.approx(1 / 3)
    assert totals.weighted_demand == 2 * 4 + 1 * 2
    assert totals.weighted_covered == 4
    assert (totals.unmet_sessions, totals.unmet_items) == (2, 2)


def test_room_utilization_daily_and_cumulative() -> None:
    """Ambigüedad A20: minutos de sesión / minutos abiertos (sin almuerzo, feriados ni cierres)."""
    master = small_master(
        blockings=(Blocking("Preparación", None, 480, 495, Audience.ALL),),
        holidays=(Holiday(MONDAY.replace(day=MONDAY.day + 2), "Feriado"),),
        demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(1, 540, "GRP", 1, 2)),
    )
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "A", "GRP", "R2", 1, 540)),
        admin=(admin("A", 0, 600, 615), admin("A", 1, 660, 690)),
    )
    metrics = compute_metrics(instance, plan)
    daily = metrics.daily
    assert (daily[0].session_min, daily[0].capacity_min) == (45, 2 * 465)
    assert daily[1].pct == pytest.approx(90 / 930)
    assert daily[1].cumulative_pct == pytest.approx(135 / 1860)
    assert daily[2].capacity_min == 0 and daily[2].pct is None
    assert daily[2].cumulative_capacity_min == 1860
    assert daily[4].capacity_min == 2 * 405
    assert daily[4].cumulative_pct == pytest.approx(135 / (3 * 930 + 810))
    rooms = {room.code: room for room in metrics.rooms}
    assert set(rooms) == {"R1", "R2"}
    assert rooms["R2"].session_min == 90
    assert metrics.totals.room_utilization_pct == pytest.approx(135 / (3 * 930 + 810))
    assert metrics.totals.room_capacity_min == 3 * 930 + 810


def test_staff_load_splits_admin_meetings_and_non_service_time() -> None:
    """Regresión M2 y A10: el tiempo del personal no asistencial y de los bloqueos queda visible."""
    master = small_master(
        blockings=(
            meeting(2, 540, 600, Audience.SERVICE_STAFF),
            Blocking("Coordinación", 3, 840, 870, Audience.STAFF, "A", counts_as_admin=True),
            Blocking("Preparación", None, 480, 495, Audience.ALL),
        ),
        staff=(person("A", hours=10), person("C", role="AD", hours=20)),
    )
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    loads = {load.code: load for load in compute_metrics(instance, plan).staff_load}
    a = loads["A"]
    assert (a.session_min, a.admin_min, a.meeting_min) == (45, 15 + 30, 60 + 5 * 15)
    assert a.admin_required_min == 15
    assert a.free_min == 600 - a.assigned_min
    assert a.usage_pct == pytest.approx(a.assigned_min / 600)
    c = loads["C"]
    assert not c.delivers_services
    assert c.meeting_min == 5 * 15
    assert c.non_service_min == 1200 - 5 * 15
    assert c.free_min == 0


def test_non_service_time_is_bounded_by_presence() -> None:
    """Las tareas no asistenciales no superan el tiempo presente: ausencias, feriados y disponibilidad cuentan."""
    absent_week = tuple(
        Absence("C", MONDAY + timedelta(days=day), None, None, "Vacaciones", AbsenceStatus.APPROVED) for day in range(5)
    )
    loads = _loads(small_master(absences=absent_week))
    assert loads["C"].non_service_min == 0
    assert loads["C"].usage_pct == 0

    # Contrato de 44 horas con 39 horas presentes (el almuerzo no es tiempo de trabajo).
    loads = _loads(small_master())
    assert loads["C"].contract_min == 44 * 60
    assert loads["C"].non_service_min == 4 * 480 + 420
    assert loads["C"].free_min == 44 * 60 - (4 * 480 + 420)

    holiday = (Holiday(MONDAY + timedelta(days=4), "Feriado"),)
    meetings = (Blocking("Preparación", None, 480, 495, Audience.ALL),)
    loads = _loads(small_master(holidays=holiday, blockings=meetings))
    assert loads["C"].meeting_min == 4 * 15
    assert loads["C"].non_service_min == 4 * 480 - 4 * 15

    half_days = tuple(AvailabilityWindow(day, 480, 780) for day in range(5))
    loads = _loads(small_master(staff=(person("A"), person("C", role="AD", availability=half_days))))
    assert loads["C"].non_service_min == 5 * 300


def test_admin_totals_split_placed_admin_and_admin_blockings() -> None:
    master = small_master(blockings=(Blocking("Coordinación", 3, 840, 870, Audience.STAFF, "A", counts_as_admin=True),))
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    metrics = compute_metrics(instance, plan)
    assert (metrics.totals.admin_min, metrics.totals.blocking_admin_min) == (15, 30)
    assert sum(load.admin_min for load in metrics.staff_load) == 15 + 30


def test_staff_hours_splits_productive_and_idle_time() -> None:
    """Nueva métrica: ociosas = tiempo contratado disponible - permisos - productivas, nunca negativa."""
    master = small_master(
        blockings=(
            meeting(2, 540, 600, Audience.SERVICE_STAFF),
            Blocking("Coordinación", 3, 840, 870, Audience.STAFF, "A", counts_as_admin=True),
        ),
        staff=(person("A", hours=10), person("C", role="AD", hours=20)),
    )
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    rows = {row.code: row for row in compute_metrics(instance, plan).staff_hours}
    a = rows["A"]
    assert a.contracted_min == 600  # 10 h de contrato, menor que la jornada programada de la semana completa.
    assert (a.session_min, a.admin_min, a.meeting_min) == (45, 15 + 30, 60)
    assert a.leave_min == 0
    assert a.idle_min == 600 - (45 + 45 + 60)
    assert a.available_min == 600
    assert a.productive_pct == pytest.approx((45 + 45 + 60) / 600)
    assert a.clinical_pct == pytest.approx(45 / 600)
    c = rows["C"]
    assert not c.delivers_services
    assert c.idle_min == 0  # todo el tiempo presente del personal no asistencial cuenta como administrativo.


def test_staff_hours_separates_leave_from_idle_and_never_goes_negative() -> None:
    """Los permisos (ausencias aprobadas) se descuentan aparte de las horas ociosas, sin dejarlas negativas."""
    absent_week = tuple(
        Absence("A", MONDAY + timedelta(days=day), None, None, "Licencia", AbsenceStatus.APPROVED) for day in range(5)
    )
    instance = make_instance(small_master(absences=absent_week))
    row = next(r for r in compute_metrics(instance, Plan()).staff_hours if r.code == "A")
    assert row.leave_min == row.contracted_min
    assert row.idle_min == 0
    assert row.available_min == 0
    assert row.productive_pct is None


def test_room_idle_minutes_is_capacity_minus_sessions() -> None:
    """Horas ociosas de una sala = horas abiertas menos horas ocupadas por atenciones."""
    master = small_master(demand=(DemandItem(0, 540, "IND", 1, 2),))
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    metrics = compute_metrics(instance, plan)
    r1 = next(r for r in metrics.rooms if r.code == "R1")
    assert r1.idle_min == r1.capacity_min - r1.session_min > 0
    assert metrics.totals.room_idle_min == sum(r.idle_min for r in metrics.rooms)
    assert metrics.totals.staff_idle_min == sum(r.idle_min for r in metrics.staff_hours)


def _loads(master: MasterData) -> dict[str, StaffLoad]:
    instance = make_instance(master)
    return {load.code: load for load in compute_metrics(instance, Plan()).staff_load}


def test_mix_rows_report_band_deviation() -> None:
    scenario = Scenario("Mezcla", "", mix_min={"IND": 0.8}, mix_max={"FAM": 0.3}, id=1)
    master = small_master(demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(0, 540, "FAM", 1, 2)))
    instance = make_instance(master, scenario)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "B", "FAM", "R2", 0, 540)),
        admin=(admin("A", 0, 600, 615), admin("B", 0, 600, 615)),
    )
    rows = {row.service: row for row in compute_metrics(instance, plan).mix}
    assert rows["IND"].share == pytest.approx(0.5)
    assert rows["IND"].deviation_min == 27
    assert rows["FAM"].deviation_min == 18
    assert not rows["FAM"].within_band


def test_room_switches_count_distinct_rooms_per_day() -> None:
    master = small_master(demand=(DemandItem(0, 540, "IND", 2, 2),))
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "A", room="R2", start=585)),
        admin=(admin("A", 0, 660, 690),),
    )
    assert compute_metrics(instance, plan).totals.room_switches == 1


def test_admin_room_load_per_slot() -> None:
    instance = make_instance()
    plan = Plan(admin=(admin("A", 0, 600, 630), admin("B", 0, 615, 630)))
    slots = {(s.day, s.start): s for s in compute_metrics(instance, plan).admin_room}
    assert slots[(0, 600)].people == 1
    assert slots[(0, 615)].people == 2
    assert slots[(0, 615)].hard_capacity == 2
    assert (0, 780) not in slots


def test_ratio_handles_zero_denominator() -> None:
    assert ratio(1, 0) is None
    assert ratio(1, 4) == 0.25
