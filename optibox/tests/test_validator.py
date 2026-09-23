"""Validador independiente: detecta cada tipo de incumplimiento en planes armados a mano."""

from __future__ import annotations

from datetime import date, timedelta

from factories import MONDAY, admin, contract_minutes, make_instance, meeting, person, service, session, small_master
from optibox.domain.instance import PlanningInstance
from optibox.domain.models import Absence, AbsenceStatus, Audience, Contract, DemandItem, Room, RoomKind
from optibox.domain.plan import Plan, Session
from optibox.domain.validator import validate_plan


def _codes(instance: PlanningInstance, plan: Plan) -> set[str]:
    return {violation.code for violation in validate_plan(instance, plan)}


def test_valid_plan_has_no_violations() -> None:
    instance = make_instance()
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "B", room="R2", start=585)),
        admin=(admin("A", 0, 600, 615), admin("B", 0, 480, 495)),
    )
    assert validate_plan(instance, plan) == ()


def test_detects_staff_overlap() -> None:
    instance = make_instance()
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "A", room="R2", start=570)),
        admin=(admin("A", 0, 660, 690),),
    )
    assert "solape_persona" in _codes(instance, plan)


def test_detects_room_overlap() -> None:
    instance = make_instance()
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "B", start=555)),
        admin=(admin("A", 0, 660, 675), admin("B", 0, 660, 675)),
    )
    assert _codes(instance, plan) == {"solape_sala"}


def test_detects_admin_on_top_of_a_session() -> None:
    """Regresión B03: el administrativo también ocupa a la persona."""
    instance = make_instance()
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 570, 585),))
    assert _codes(instance, plan) == {"solape_persona"}


def test_detects_admin_room_over_capacity() -> None:
    instance = make_instance(small_master(staff=(person("A"), person("B"), person("D"))))
    plan = Plan(admin=(admin("A", 0, 600, 615), admin("B", 0, 600, 615), admin("D", 0, 600, 615)))
    assert "sala_administrativa_capacidad" in _codes(instance, plan)


def test_detects_contract_excess_including_admin_and_blockings() -> None:
    """Regresión B02: sesiones, administrativo y bloqueos cuentan contra el contrato."""
    master = small_master(
        staff=(person("A", contracts=contract_minutes(90)),),
        blockings=(meeting(2, 540, 600, Audience.SERVICE_STAFF),),
    )
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    assert "contrato" in _codes(instance, plan)


def test_detects_a_session_after_the_contract_ends() -> None:
    """Regresión: un contrato que termina el martes no admite sesiones el miércoles."""
    contracts = (Contract(date(2026, 1, 1), MONDAY + timedelta(days=1), 44 * 60),)
    master = small_master(staff=(person("A", contracts=contracts),), demand=(DemandItem(2, 540, "IND", 1, 2),))
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", day=2, start=540),), admin=(admin("A", 2, 600, 615),))
    violations = validate_plan(instance, plan)
    assert {v.code for v in violations} == {"regla:disponibilidad", "administrativo_horario"}
    assert any("contrato vigente ese día" in v.message for v in violations)


def test_detects_a_session_before_the_contract_starts() -> None:
    contracts = (Contract(MONDAY + timedelta(days=3), None, 44 * 60),)
    master = small_master(staff=(person("A", contracts=contracts),))
    instance = make_instance(master)
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    assert "regla:disponibilidad" in _codes(instance, plan)


def test_midweek_contract_change_uses_the_prorated_minutes() -> None:
    """10 horas hasta el martes y 1 hora desde el miércoles dan 276 minutos en la semana, no 600."""
    contracts = (
        Contract(date(2026, 1, 1), MONDAY + timedelta(days=1), 600),
        Contract(MONDAY + timedelta(days=2), None, 60),
    )
    master = small_master(
        staff=(person("A", contracts=contracts),),
        demand=(DemandItem(0, 540, "IND", 2, 2), DemandItem(0, 660, "IND", 2, 2), DemandItem(1, 540, "IND", 2, 2)),
    )
    instance = make_instance(master)
    assert instance.staff_by_code["A"].contract_minutes == 276
    starts = ((0, 540), (0, 585), (0, 660), (0, 705), (1, 540), (1, 585))
    plan = Plan(
        sessions=tuple(session(instance, "A", day=day, start=start) for day, start in starts),
        admin=(admin("A", 0, 630, 660), admin("A", 0, 750, 780), admin("A", 1, 630, 660)),
    )
    assert _codes(instance, plan) == {"contrato"}


def test_detects_admin_without_an_admin_room() -> None:
    master = small_master(
        service_types=(service("IND", admin_minutes=0),),
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, active=False, simultaneous_hard=2),
        ),
    )
    instance = make_instance(master)
    assert instance.admin_room is None
    plan = Plan(sessions=(session(instance, "A", start=540),), admin=(admin("A", 0, 600, 615),))
    assert "sala_administrativa" in _codes(instance, plan)


def test_detects_caps() -> None:
    master = small_master(demand=tuple(DemandItem(day, 900, "GRP", 1, 2) for day in range(3)))
    instance = make_instance(master)
    plan = Plan(
        sessions=tuple(session(instance, "A", "GRP", "R2", day, 900) for day in range(3)),
        admin=tuple(admin("A", day, 600, 630) for day in range(3)),
    )
    assert "tope_semanal" in _codes(instance, plan)


def test_detects_per_staff_and_daily_caps() -> None:
    master = small_master(
        service_types=(service("IND", max_week_per_staff=1, max_day_total=1, max_day_per_staff=1),),
        demand=(DemandItem(0, 540, "IND", 2, 2),),
    )
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "A", room="R2", start=585)),
        admin=(admin("A", 0, 660, 690),),
    )
    assert {"tope_semanal_persona", "tope_diario", "tope_diario_persona"} <= _codes(instance, plan)


def test_detects_coverage_above_demand() -> None:
    instance = make_instance()
    plan = Plan(
        sessions=(
            session(instance, "A", start=540),
            session(instance, "B", room="R2", start=540),
            session(instance, "A", start=585),
        ),
        admin=(admin("A", 0, 660, 690), admin("B", 0, 660, 675)),
    )
    assert _codes(instance, plan) == {"cobertura"}


def test_detects_missing_associated_admin() -> None:
    """Regresión B04: una sesión sin su administrativo asociado es un plan inválido."""
    instance = make_instance()
    plan = Plan(sessions=(session(instance, "A", start=540),))
    assert _codes(instance, plan) == {"administrativo_asociado"}


def test_detects_admin_above_the_daily_extra_cap() -> None:
    """Regresión B05: no hay administrativo 'asociado' sin sesiones más allá del adicional permitido."""
    instance = make_instance()
    plan = Plan(admin=(admin("A", 1, 600, 660),))
    assert _codes(instance, plan) == {"administrativo_tope"}


def test_detects_admin_outside_availability_and_for_non_service_staff() -> None:
    instance = make_instance()
    plan = Plan(admin=(admin("A", 0, 780, 795), admin("C", 0, 600, 615)))
    assert {"administrativo_horario", "administrativo_cargo"} <= _codes(instance, plan)


def test_detects_incompatible_room_and_lunch_crossing() -> None:
    """Regresión B06 y B17: la validación reaplica la cadena de reglas a cada sesión."""
    master = small_master(demand=(DemandItem(0, 540, "EVA", 1, 1), DemandItem(0, 720, "IND", 1, 2)))
    instance = make_instance(master)
    wrong_room = session(instance, "A", "EVA", "R1", 0, 540)
    lunch = session(instance, "B", "IND", "R2", 0, 750)
    plan = Plan(sessions=(wrong_room, lunch), admin=(admin("A", 0, 900, 960), admin("B", 0, 900, 915)))
    codes = _codes(instance, plan)
    assert "regla:sala_compatible" in codes
    assert "regla:horario" in codes


def test_detects_sessions_during_blockings_and_absences() -> None:
    master = small_master(
        blockings=(meeting(0, 540, 600, Audience.STAFF, "A"),),
        absences=(Absence("B", MONDAY, None, None, "Permiso", AbsenceStatus.APPROVED),),
    )
    instance = make_instance(master)
    plan = Plan(
        sessions=(session(instance, "A", start=540), session(instance, "B", room="R2", start=540)),
        admin=(admin("A", 0, 660, 675),),
    )
    codes = _codes(instance, plan)
    assert "regla:bloqueo" in codes
    assert "regla:ausencia" in codes


def test_detects_wrong_duration_and_participants() -> None:
    instance = make_instance()
    plan = Plan(
        sessions=(Session("A", "IND", "R1", 0, 540, 60, 1), Session("B", "IND", "R2", 0, 540, 45, 3)),
        admin=(admin("A", 0, 660, 675), admin("B", 0, 660, 675)),
    )
    codes = _codes(instance, plan)
    assert "duracion" in codes
    assert "participantes" in codes


def test_detects_unknown_references() -> None:
    instance = make_instance()
    plan = Plan(sessions=(Session("Z", "IND", "R1", 0, 540, 45, 1),), admin=(admin("Y", 0, 600, 615),))
    assert _codes(instance, plan) == {"desconocido"}
