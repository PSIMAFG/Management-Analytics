"""Causas de los turnos sin cubrir, sobre planes armados a mano."""

from __future__ import annotations

from factories import admin, contract_minutes, make_instance, meeting, person, service, session, small_master
from optibox.domain.diagnosis import UnmetCause, diagnose_unmet
from optibox.domain.models import (
    Audience,
    AvailabilityWindow,
    DemandItem,
    Role,
    Room,
    RoomKind,
    Scenario,
)
from optibox.domain.plan import Plan
from optibox.domain.rules import generate_candidates
from optibox.domain.validator import validate_plan


def test_no_candidates_names_the_dominant_rule() -> None:
    master = small_master(
        blockings=(meeting(2, 480, 780, Audience.SERVICE_STAFF),),
        demand=(DemandItem(2, 540, "IND", 2, 2),),
    )
    instance = make_instance(master)
    unmet = diagnose_unmet(instance, Plan(), generate_candidates(instance))
    assert len(unmet) == 1
    assert unmet[0].cause is UnmetCause.NO_CANDIDATES
    assert unmet[0].rule_code == "bloqueo"
    assert unmet[0].shortfall == 2
    assert "bloqueos" in unmet[0].detail


def test_too_few_candidates_all_assigned() -> None:
    """Si todos los candidatos quedaron asignados, la causa explica cuántos había y qué regla excluyó más."""
    windows = (AvailabilityWindow(0, 540, 585), AvailabilityWindow(0, 600, 615))
    master = small_master(
        staff=(person("A", availability=windows),),
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 5, simultaneous_hard=5),
        ),
        service_types=(service("IND", rooms=frozenset({"R1"})),),
        demand=(DemandItem(0, 540, "IND", 3, 2),),
    )
    instance = make_instance(master)
    stage1 = generate_candidates(instance)
    assert len(stage1.candidates) == 1
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    unmet = diagnose_unmet(instance, plan, stage1)
    assert [u.cause for u in unmet] == [UnmetCause.NO_CANDIDATES]
    assert unmet[0].rule_code == "disponibilidad"
    assert unmet[0].detail.startswith("Solo hay 1 candidato y quedó asignado.")
    assert unmet[0].shortfall == 2


def test_busy_staff_is_the_cause() -> None:
    master = small_master(staff=(person("A"),), demand=(DemandItem(0, 540, "IND", 5, 2),))
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "A", room="R2", start=585)),
        admin=(admin("A", 0, 660, 690),),
    )
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.STAFF_BUSY]
    assert unmet[0].shortfall == 3


def test_busy_rooms_are_the_cause() -> None:
    master = small_master(
        staff=(person("A"), person("B"), person("D")),
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("R2", "Sala de usos múltiples", RoomKind.MULTIPURPOSE, True, True, 4, active=False),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, simultaneous_hard=5),
        ),
        demand=(DemandItem(0, 540, "IND", 3, 2),),
    )
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "B", start=585)),
        admin=(admin("A", 0, 660, 675), admin("B", 0, 660, 675)),
    )
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.ROOM_BUSY]


def test_cap_is_the_cause() -> None:
    master = small_master(
        service_types=(service("IND", max_week_total=1),),
        demand=(DemandItem(0, 540, "IND", 2, 2),),
    )
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.CAP]
    assert unmet[0].cause_label == "Tope alcanzado"


def test_exhausted_contract_is_the_cause() -> None:
    master = small_master(
        staff=(person("A", contracts=contract_minutes(60)),),
        demand=(DemandItem(0, 540, "IND", 2, 2),),
    )
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.CONTRACT]


def test_full_admin_room_is_the_cause() -> None:
    master = small_master(
        roles=(
            Role("CL", "Clínico", True, frozenset({"IND"})),
            Role("OT", "Otro cargo asistencial", True, frozenset({"FAM"})),
            Role("AD", "Apoyo administrativo", False),
        ),
        staff=(
            person("A", availability=(AvailabilityWindow(0, 540, 600),)),
            person("B", role="OT", availability=(AvailabilityWindow(0, 540, 690),)),
            person("D", role="OT", availability=(AvailabilityWindow(0, 540, 690),)),
        ),
        demand=(DemandItem(0, 540, "IND", 1, 2),),
    )
    instance = make_instance(master)
    # B y D tienen dos sesiones cada uno y su administrativo exigido llena la sala administrativa (capacidad 2)
    # en los únicos slots libres que le quedarían a A.
    plan = Plan(
        sessions=(
            session(instance, "B", "FAM", "R1", 0, 600),
            session(instance, "B", "FAM", "R1", 0, 645),
            session(instance, "D", "FAM", "R2", 0, 600),
            session(instance, "D", "FAM", "R2", 0, 645),
        ),
        admin=(admin("B", 0, 540, 555), admin("D", 0, 540, 555), admin("B", 0, 585, 600), admin("D", 0, 585, 600)),
    )
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.ADMIN]


def test_addable_candidate_points_to_the_search_limit() -> None:
    instance = make_instance()
    unmet = diagnose_unmet(instance, Plan(), generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.SEARCH_LIMIT]
    assert unmet[0].cause_label == "La búsqueda no alcanzó el óptimo"


def test_one_free_person_is_enough_to_blame_the_search() -> None:
    """Si alguien podía tomar la sesión, la causa no es la capacidad aunque la mayoría de los candidatos choque."""
    instance = make_instance()
    stage1 = generate_candidates(instance)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 585, 600),))
    unmet = diagnose_unmet(instance, plan, stage1)
    assert [u.cause for u in unmet] == [UnmetCause.SEARCH_LIMIT]
    assert "cabían en el plan" in unmet[0].detail
    assert unmet[0].shortfall == 1


def test_optional_extra_admin_does_not_hide_a_free_slot() -> None:
    """El administrativo adicional es opcional: si solo él ocupa el horario, la sesión cabía."""
    master = small_master(staff=(person("A"),), demand=(DemandItem(0, 540, "IND", 2, 2),))
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540),),
        admin=(admin("A", 0, 480, 495), admin("A", 0, 585, 615)),
    )
    assert validate_plan(instance, plan) == ()
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.SEARCH_LIMIT]


def test_required_admin_can_move_within_the_day() -> None:
    """El administrativo exigido puede cambiar de hora: una sesión que lo pisa igual cabía."""
    master = small_master(staff=(person("A"),), demand=(DemandItem(0, 540, "IND", 2, 2),))
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 585, 600),))
    unmet = diagnose_unmet(instance, plan, generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.SEARCH_LIMIT]


def test_addable_candidate_under_a_lower_floor_points_to_the_scenario() -> None:
    master = small_master(scenarios=(Scenario("Calidad", "Acepta menos cobertura", coverage_floor_pct=95, id=1),))
    instance = make_instance(master)
    unmet = diagnose_unmet(instance, Plan(), generate_candidates(instance))
    assert [u.cause for u in unmet] == [UnmetCause.COVERAGE_FLOOR]
    assert "5 % menos de cobertura" in unmet[0].detail


def test_no_unmet_when_demand_is_covered() -> None:
    instance = make_instance()
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "B", room="R2", start=540)),
        admin=(admin("A", 0, 600, 615), admin("B", 0, 615, 630)),
    )
    assert diagnose_unmet(instance, plan, generate_candidates(instance)) == ()
