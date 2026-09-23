"""Armado de la instancia semanal: feriados, ausencias, bloqueos y contratos."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from factories import MONDAY, contract_minutes, make_instance, meeting, person, small_master
from optibox.domain.instance import build_instance
from optibox.domain.models import (
    Absence,
    AbsenceStatus,
    Audience,
    AvailabilityWindow,
    Blocking,
    Contract,
    DemandItem,
    Holiday,
    Room,
    RoomKind,
)
from optibox.domain.timegrid import mask_minutes, mask_slots
from optibox.errors import ValidationError


def test_week_starts_on_the_monday_of_the_chosen_date() -> None:
    master = small_master()
    instance = build_instance(master, MONDAY + timedelta(days=3), master.scenarios[0])
    assert instance.week_start == MONDAY
    assert [day.date for day in instance.days] == [MONDAY + timedelta(days=i) for i in range(5)]


def test_holiday_closes_the_day_and_drops_its_demand() -> None:
    master = small_master(
        holidays=(Holiday(MONDAY, "Feriado"),),
        demand=(DemandItem(0, 540, "IND", 2, 2), DemandItem(1, 540, "IND", 1, 2)),
    )
    instance = make_instance(master)
    assert not instance.days[0].is_open
    assert instance.days[0].open_mask == 0
    assert instance.days[0].room_capacity_minutes == 0
    assert instance.holiday_demand_dropped == 2
    assert [item.weekday for item in instance.demand] == [1]
    assert instance.staff_by_code["A"].days[0].workable == 0


def test_only_approved_absences_block_time() -> None:
    absences = (
        Absence("A", MONDAY, None, None, "Vacaciones", AbsenceStatus.APPROVED),
        Absence("B", MONDAY, None, None, "Vacaciones", AbsenceStatus.PENDING),
        Absence("B", MONDAY + timedelta(days=1), None, None, "Permiso", AbsenceStatus.REJECTED),
    )
    instance = make_instance(small_master(absences=absences))
    assert instance.staff_by_code["A"].days[0].workable == 0
    assert instance.staff_by_code["B"].days[0].workable == instance.days[0].open_mask
    assert instance.staff_by_code["B"].days[1].workable == instance.days[1].open_mask
    assert len(instance.absences) == 3


def test_partial_absence_rounds_outward_to_the_grid() -> None:
    absence = Absence("A", MONDAY, 550, 560, "Trámite", AbsenceStatus.APPROVED)
    instance = make_instance(small_master(absences=(absence,)))
    assert mask_slots(instance.staff_by_code["A"].days[0].absent) == (540, 555)


def test_absences_outside_the_week_are_ignored() -> None:
    absence = Absence("A", MONDAY - timedelta(days=7), None, None, "Vacaciones", AbsenceStatus.APPROVED)
    instance = make_instance(small_master(absences=(absence,)))
    assert instance.staff_by_code["A"].days[0].absent == 0
    assert instance.absences == ()


def test_blockings_are_resolved_per_person() -> None:
    """Regresión B14: la reunión del personal asistencial no bloquea al personal de apoyo."""
    master = small_master(blockings=(meeting(2, 540, 660, Audience.SERVICE_STAFF),))
    instance = make_instance(master)
    assert mask_minutes(instance.staff_by_code["A"].days[2].blocked) == 120
    assert instance.staff_by_code["C"].days[2].blocked == 0


def test_misaligned_blocking_is_rounded_outward() -> None:
    """Regresión B14: un bloqueo desalineado ocupa los slots que toca en vez de ignorarse."""
    master = small_master(blockings=(Blocking("Llamada", 0, 550, 560, Audience.STAFF, "A"),))
    instance = make_instance(master)
    assert mask_slots(instance.staff_by_code["A"].days[0].blocked) == (540, 555)


def test_blockings_consume_contract_only_inside_availability() -> None:
    master = small_master(
        staff=(
            person("A", hours=10),
            person("B", hours=10, availability=(AvailabilityWindow(0, 480, 780),)),
        ),
        blockings=(
            meeting(2, 540, 660, Audience.SERVICE_STAFF),
            Blocking("Coordinación", 2, 840, 900, Audience.STAFF, "A", counts_as_admin=True),
        ),
    )
    instance = make_instance(master)
    a = instance.staff_by_code["A"]
    b = instance.staff_by_code["B"]
    assert a.blocking_minutes() == 180
    assert a.blocking_admin_minutes() == 60
    assert a.assignable_minutes == 600 - 180
    assert b.blocking_minutes() == 0
    assert b.assignable_minutes == 600


def test_contract_version_in_force_is_used() -> None:
    """Regresión B22: la semana usa el contrato vigente, no el último guardado."""
    contracts = (
        Contract(date(2026, 1, 1), MONDAY - timedelta(days=1), 44 * 60),
        Contract(MONDAY, None, 22 * 60),
    )
    master = small_master(staff=(person("A", contracts=contracts),))
    assert make_instance(master).staff_by_code["A"].contract_minutes == 22 * 60
    earlier = build_instance(master, MONDAY - timedelta(days=7), master.scenarios[0])
    assert earlier.staff_by_code["A"].contract_minutes == 44 * 60


def test_contract_ending_midweek_leaves_the_rest_of_the_week_unavailable() -> None:
    """Un contrato que termina el martes solo aporta lunes y martes, y sus minutos se prorratean."""
    contracts = (Contract(date(2026, 1, 1), MONDAY + timedelta(days=1), 44 * 60),)
    staff = make_instance(small_master(staff=(person("A", contracts=contracts),))).staff_by_code["A"]
    assert [day.in_contract for day in staff.days] == [True, True, False, False, False]
    assert [day.workable != 0 for day in staff.days] == [True, True, False, False, False]
    assert staff.contract_minutes == 44 * 60 * 2 // 5


def test_contract_starting_midweek_only_covers_its_days() -> None:
    contracts = (Contract(MONDAY + timedelta(days=3), None, 44 * 60),)
    staff = make_instance(small_master(staff=(person("A", contracts=contracts),))).staff_by_code["A"]
    assert [day.in_contract for day in staff.days] == [False, False, False, True, True]
    assert staff.days[0].available == 0
    assert staff.contract_minutes == 44 * 60 * 2 // 5


def test_midweek_contract_change_prorates_each_contract_by_its_days() -> None:
    """44 horas hasta el martes y 4 horas desde el miércoles: 2/5 de 44 h más 3/5 de 4 h."""
    contracts = (
        Contract(date(2026, 1, 1), MONDAY + timedelta(days=1), 44 * 60),
        Contract(MONDAY + timedelta(days=2), None, 4 * 60),
    )
    staff = make_instance(small_master(staff=(person("A", contracts=contracts),))).staff_by_code["A"]
    assert all(day.in_contract for day in staff.days)
    assert staff.contract_minutes == (2 * 44 * 60 + 3 * 4 * 60) // 5
    assert staff.contract is not None and staff.contract.weekly_minutes == 4 * 60


def test_blockings_outside_the_contract_do_not_consume_minutes() -> None:
    contracts = (Contract(MONDAY + timedelta(days=3), None, 10 * 60),)
    master = small_master(
        staff=(person("A", contracts=contracts),),
        blockings=(meeting(2, 540, 660, Audience.SERVICE_STAFF),),
    )
    staff = make_instance(master).staff_by_code["A"]
    assert staff.blocking_minutes() == 0


def test_missing_admin_room_is_rejected_when_sessions_need_admin() -> None:
    master = small_master(
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("R2", "Sala de usos múltiples", RoomKind.MULTIPURPOSE, True, True, 4),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, active=False, simultaneous_hard=2),
        ),
    )
    with pytest.raises(ValidationError, match="sala administrativa activa"):
        make_instance(master)


def test_person_without_contract_has_no_minutes() -> None:
    master = small_master(staff=(person("A", contracts=()),))
    staff = make_instance(master).staff_by_code["A"]
    assert staff.contract is None
    assert staff.assignable_minutes == 0


def test_inactive_staff_is_left_out() -> None:
    master = small_master(staff=(person("A"), person("B", active=False)))
    assert [sw.code for sw in make_instance(master).staff] == ["A"]


def test_center_closures_reduce_room_capacity() -> None:
    master = small_master(blockings=(Blocking("Preparación", None, 480, 495, Audience.ALL),))
    instance = make_instance(master)
    assert instance.days[0].room_capacity_minutes == 480 - 15
    assert instance.days[4].room_capacity_minutes == 420 - 15


def test_weighted_demand_uses_priority_weights() -> None:
    master = small_master(demand=(DemandItem(0, 540, "IND", 2, 1), DemandItem(0, 600, "FAM", 3, 3)))
    assert make_instance(master).weighted_demand == 2 * 4 + 3 * 1


def test_contract_minutes_factory() -> None:
    master = small_master(staff=(person("A", contracts=contract_minutes(90)),))
    assert make_instance(master).staff_by_code["A"].contract_minutes == 90
