"""Modelos del dominio: validaciones al construir y reglas de pertenencia."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from factories import person
from optibox.domain.models import (
    Absence,
    AbsenceStatus,
    Audience,
    AvailabilityWindow,
    Blocking,
    Contract,
    DemandItem,
    Role,
    Room,
    RoomKind,
    RoomRule,
    RoomRuleKind,
    Scenario,
    ServiceTarget,
    ServiceType,
)
from optibox.domain.timegrid import week_dates
from optibox.errors import ValidationError

CLINICAL = Role("CL", "Clínico", True, frozenset({"IND"}))
SUPPORT = Role("AD", "Apoyo administrativo", False)


def test_contracts_of_a_person_cannot_overlap() -> None:
    """Ambigüedad A15: los contratos solapados se prohíben al validar."""
    with pytest.raises(ValidationError):
        person(
            "A",
            contracts=(
                Contract(date(2026, 1, 1), None, 2640),
                Contract(date(2026, 6, 1), None, 1320),
            ),
        )


def test_week_uses_the_contract_in_force() -> None:
    """Regresión B22: los contratos se versionan y se usa el vigente en la semana."""
    staff = person(
        "A",
        contracts=(
            Contract(date(2026, 1, 1), date(2026, 9, 30), 44 * 60),
            Contract(date(2026, 10, 1), None, 22 * 60),
        ),
    )
    before = staff.contract_for_week(week_dates(date(2026, 9, 21)))
    after = staff.contract_for_week(week_dates(date(2026, 10, 5)))
    assert before is not None and before.weekly_minutes == 44 * 60
    assert after is not None and after.weekly_minutes == 22 * 60


def test_week_minutes_are_prorated_by_the_days_each_contract_covers() -> None:
    week = week_dates(date(2026, 10, 5))
    staff = person(
        "A",
        contracts=(
            Contract(date(2026, 1, 1), date(2026, 10, 6), 44 * 60),
            Contract(date(2026, 10, 7), None, 4 * 60),
        ),
    )
    assert staff.contract_on(date(2026, 10, 6)) == staff.contracts[0]
    assert staff.contract_on(date(2026, 10, 7)) == staff.contracts[1]
    assert staff.week_contract_minutes(week) == (2 * 44 * 60 + 3 * 4 * 60) // 5
    assert staff.contract_for_week(week) == staff.contracts[1]
    full = person("B", contracts=(Contract(date(2026, 1, 1), None, 22 * 60),))
    assert full.week_contract_minutes(week) == 22 * 60
    leaving = person("C", contracts=(Contract(date(2026, 1, 1), date(2026, 10, 5), 44 * 60),))
    assert leaving.week_contract_minutes(week) == 44 * 60 // 5
    assert leaving.contract_on(date(2026, 10, 6)) is None


def test_person_without_contract_in_the_week() -> None:
    staff = person("A", contracts=(Contract(date(2026, 1, 1), date(2026, 6, 30), 2640),))
    assert staff.contract_for_week(week_dates(date(2026, 10, 5))) is None


def test_contract_validation() -> None:
    with pytest.raises(ValidationError):
        Contract(date(2026, 5, 1), date(2026, 4, 30), 2640)
    with pytest.raises(ValidationError):
        Contract(date(2026, 5, 1), None, 0)
    assert Contract(date(2026, 5, 1), None, 60).covers(date(2030, 1, 1))


def test_availability_windows_cannot_overlap() -> None:
    with pytest.raises(ValidationError):
        person("A", availability=(AvailabilityWindow(0, 480, 780), AvailabilityWindow(0, 720, 1020)))
    with pytest.raises(ValidationError):
        AvailabilityWindow(0, 480, 485)


def test_room_rules() -> None:
    with pytest.raises(ValidationError):
        RoomRule("R1", RoomRuleKind.PREFERRED)
    with pytest.raises(ValidationError):
        person("A", room_rules=(RoomRule("R1", RoomRuleKind.ALLOWED), RoomRule("R1", RoomRuleKind.FORBIDDEN)))
    staff = person(
        "A",
        room_rules=(
            RoomRule("R2", RoomRuleKind.PREFERRED, 1),
            RoomRule("R1", RoomRuleKind.ALLOWED),
            RoomRule("R3", RoomRuleKind.FORBIDDEN),
        ),
    )
    assert staff.has_room_matrix
    assert staff.allowed_rooms == {"R1", "R2"}
    assert staff.forbidden_rooms == {"R3"}
    assert staff.preference_rank("R2") == 1
    assert staff.preference_rank("R1") is None


def test_admin_room_needs_simultaneous_capacity() -> None:
    with pytest.raises(ValidationError):
        Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10)
    with pytest.raises(ValidationError):
        Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, simultaneous_hard=8, simultaneous_soft=9)
    room = Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, simultaneous_hard=10)
    assert room.soft_capacity == 10


def test_blocking_audience_is_resolved_by_attributes() -> None:
    """Regresión B14: 'personal asistencial' no alcanza al personal de apoyo; el cargo y la persona se respetan."""
    clinician = person("A")
    support = person("C", role="AD")
    service_staff = Blocking("Reunión técnica", 2, 540, 660, Audience.SERVICE_STAFF)
    assert service_staff.applies_to(clinician, CLINICAL)
    assert not service_staff.applies_to(support, SUPPORT)
    by_role = Blocking("Coordinación", 1, 600, 660, Audience.ROLE, "AD")
    assert by_role.applies_to(support, SUPPORT)
    assert not by_role.applies_to(clinician, CLINICAL)
    by_person = Blocking("Supervisión", 3, 600, 660, Audience.STAFF, "A")
    assert by_person.applies_to(clinician, CLINICAL)
    assert not by_person.applies_to(person("B"), CLINICAL)
    everyone = Blocking("Preparación", None, 480, 495, Audience.ALL)
    assert everyone.applies_to(support, SUPPORT)
    assert everyone.applies_on(4)
    assert not service_staff.applies_on(1)


def test_blocking_target_must_match_audience() -> None:
    with pytest.raises(ValidationError):
        Blocking("Coordinación", 1, 600, 660, Audience.ROLE)
    with pytest.raises(ValidationError):
        Blocking("Preparación", None, 480, 495, Audience.ALL, "A")
    with pytest.raises(ValidationError):
        Blocking("Reunión", 1, 660, 600, Audience.ALL)


def test_absences() -> None:
    with pytest.raises(ValidationError):
        Absence("A", date(2026, 10, 5), 480, None, "Permiso", AbsenceStatus.APPROVED)
    partial = Absence("A", date(2026, 10, 5), 480, 780, "Permiso", AbsenceStatus.APPROVED)
    assert partial.blocks and not partial.full_day
    pending = Absence("A", date(2026, 10, 5), None, None, "Permiso", AbsenceStatus.PENDING)
    assert pending.full_day and not pending.blocks


def test_service_type_validation() -> None:
    base = {
        "code": "IND",
        "name": "Individual",
        "duration_min": 45,
        "participants": 1,
        "requires_group_room": False,
        "requires_evaluation_room": False,
        "admin_minutes": 15,
        "priority": 2,
        "color": "#000000",
    }
    ServiceType(**base)  # type: ignore[arg-type]
    for field, value in (("duration_min", 50), ("participants", 0), ("priority", 4), ("admin_minutes", 10)):
        with pytest.raises(ValidationError):
            ServiceType(**(base | {field: value}))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ServiceType(**base, max_week_total=-1)  # type: ignore[arg-type]


def test_role_without_services_cannot_list_service_types() -> None:
    with pytest.raises(ValidationError):
        Role("AD", "Apoyo", False, frozenset({"IND"}))


def test_demand_item_validation() -> None:
    with pytest.raises(ValidationError):
        DemandItem(0, 570, "IND", 1, 2)
    with pytest.raises(ValidationError):
        DemandItem(5, 540, "IND", 1, 2)
    with pytest.raises(ValidationError):
        DemandItem(0, 540, "IND", -1, 2)
    with pytest.raises(ValidationError):
        DemandItem(0, 540, "IND", 1, 0)


def test_scenario_validation() -> None:
    with pytest.raises(ValidationError):
        Scenario("", "")
    with pytest.raises(ValidationError):
        Scenario("S", "", mix_min={"IND": 0.6}, mix_max={"IND": 0.4})
    with pytest.raises(ValidationError):
        Scenario("S", "", coverage_floor_pct=40)
    with pytest.raises(ValidationError):
        Scenario("S", "", service_weights={"IND": -1.0})
    with pytest.raises(ValidationError):
        Scenario("S", "", room_continuity_weight=-1)


def test_scenario_mappings_are_read_only_copies() -> None:
    """El escenario es inmutable: sus pesos y cotas no se pueden cambiar después de construirlo."""
    weights = {"IND": 2.0}
    scenario = Scenario("S", "", service_weights=weights, mix_min={"IND": 0.2})
    weights["IND"] = 9.0
    assert scenario.service_weights == {"IND": 2.0}
    with pytest.raises(TypeError):
        scenario.mix_min["IND"] = 0.9  # type: ignore[index]
    assert replace(scenario, name="Otro").mix_min == {"IND": 0.2}


def test_service_target_band() -> None:
    with pytest.raises(ValidationError):
        ServiceTarget("IND", None, None)
    with pytest.raises(ValidationError):
        ServiceTarget("IND", 600, 300)
    assert ServiceTarget("IND", None, 300).max_minutes == 300
