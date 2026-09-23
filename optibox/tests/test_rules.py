"""Etapa 1: cada regla pasa y falla, y el generador masivo respeta la semántica de la cadena."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from factories import (
    MONDAY,
    candidate,
    contract_minutes,
    make_instance,
    meeting,
    person,
    service,
    small_master,
)
from optibox.domain.models import (
    Absence,
    AbsenceStatus,
    Audience,
    AvailabilityWindow,
    Blocking,
    DemandItem,
    Holiday,
    Role,
    Room,
    RoomKind,
    RoomRule,
    RoomRuleKind,
)
from optibox.domain.plan import Candidate
from optibox.domain.rules import (
    AbsenceRule,
    AvailabilityRule,
    BlockingRule,
    CompetenceRule,
    ContractRule,
    DemandRule,
    RoomCompatibilityRule,
    RoomPermissionRule,
    RuleChain,
    ScheduleFitRule,
    generate_candidates,
)


def test_contract_rule() -> None:
    passing = make_instance()
    assert ContractRule().check(passing, candidate()) is None
    without = make_instance(small_master(staff=(person("A", contracts=()),)))
    assert ContractRule().check(without, candidate()) is not None
    master = small_master(
        staff=(person("A", contracts=contract_minutes(60)),),
        blockings=(meeting(2, 540, 600, Audience.SERVICE_STAFF),),
    )
    assert "bloqueos" in (ContractRule().check(make_instance(master), candidate()) or "")


def test_competence_rule_role_and_whitelist() -> None:
    """Regresión B07 y B23: la lista blanca individual restringe; vacía significa todo lo del cargo."""
    master = small_master(
        roles=(Role("CL", "Clínico", True, frozenset({"IND", "FAM"})), Role("AD", "Apoyo", False)),
        staff=(person("A", skills=frozenset({"IND"})), person("B"), person("C", role="AD")),
    )
    instance = make_instance(master)
    rule = CompetenceRule()
    assert rule.check(instance, candidate("A", "IND")) is None
    assert rule.check(instance, candidate("A", "FAM")) is not None
    assert rule.check(instance, candidate("B", "FAM")) is None
    assert rule.check(instance, candidate("B", "EVA")) is not None
    assert rule.check(instance, candidate("C", "IND")) == "Su cargo no realiza atenciones."


def test_availability_rule_needs_the_whole_interval() -> None:
    master = small_master(staff=(person("A", availability=(AvailabilityWindow(0, 480, 600),)),))
    instance = make_instance(master)
    assert AvailabilityRule().check(instance, candidate(start=540)) is None
    assert AvailabilityRule().check(instance, candidate(start=570)) is not None
    assert AvailabilityRule().check(instance, candidate(day=1)) is not None


def test_absence_rule_blocks_holidays_and_approved_absences() -> None:
    master = small_master(
        holidays=(Holiday(MONDAY, "Feriado"),),
        absences=(
            Absence("A", MONDAY.replace(day=MONDAY.day + 1), 480, 600, "Permiso", AbsenceStatus.APPROVED),
            Absence("B", MONDAY.replace(day=MONDAY.day + 1), None, None, "Permiso", AbsenceStatus.PENDING),
        ),
    )
    instance = make_instance(master)
    rule = AbsenceRule()
    assert rule.check(instance, candidate(day=0)) == "El día es feriado."
    assert rule.check(instance, candidate(day=1, start=540)) is not None
    assert rule.check(instance, candidate(day=1, start=600)) is None
    assert rule.check(instance, candidate("B", day=1)) is None


def test_blocking_rule() -> None:
    instance = make_instance(small_master(blockings=(meeting(2, 540, 660, Audience.SERVICE_STAFF),)))
    assert BlockingRule().check(instance, candidate(day=2, start=600)) is not None
    assert BlockingRule().check(instance, candidate(day=2, start=660)) is None
    assert BlockingRule().check(instance, candidate(day=0)) is None


def test_room_compatibility_rule() -> None:
    """Regresión B06: la sala debe estar habilitada para el tipo y cumplir sus requisitos."""
    master = small_master(
        service_types=(
            service("IND", rooms=frozenset({"R1", "R2", "R3", "R4"})),
            service("GRX", requires_group_room=True),
            service("EVX", requires_evaluation_room=True),
            service("EVA", rooms=frozenset({"R2"})),
        ),
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("R2", "Sala de usos múltiples", RoomKind.MULTIPURPOSE, True, True, 4),
            Room("R3", "Box 3", RoomKind.BOX, False, False, 1, active=False),
            Room("R4", "Box 4", RoomKind.BOX, False, False, 1, reserved_role="OT"),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 5, simultaneous_hard=5),
        ),
    )
    instance = make_instance(master)
    rule = RoomCompatibilityRule()
    assert rule.check(instance, candidate(room="R1")) is None
    assert rule.check(instance, candidate(service_code="EVA", room="R1")) is not None
    assert rule.check(instance, candidate(service_code="EVA", room="R2")) is None
    assert "grupos" in (rule.check(instance, candidate(service_code="GRX", room="R1")) or "")
    assert "evaluaciones" in (rule.check(instance, candidate(service_code="EVX", room="R1")) or "")
    assert rule.check(instance, candidate(service_code="GRX", room="R2")) is None
    assert rule.check(instance, candidate(room="R3")) == "La sala no está activa."
    assert rule.check(instance, candidate(room="ADM")) is not None
    assert rule.check(instance, candidate(room="R4")) == "La sala está reservada para otro cargo."


def test_room_reserved_for_the_role_is_allowed_to_it() -> None:
    master = small_master(
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1, reserved_role="CL"),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 5, simultaneous_hard=5),
        ),
        service_types=(service("IND", rooms=frozenset({"R1"})),),
    )
    assert RoomCompatibilityRule().check(make_instance(master), candidate()) is None


def test_room_permission_rule_matrix_and_forbidden() -> None:
    master = small_master(
        staff=(
            person("A", room_rules=(RoomRule("R1", RoomRuleKind.FORBIDDEN),)),
            person("B", room_rules=(RoomRule("R2", RoomRuleKind.ALLOWED),)),
            person("D", room_rules=(RoomRule("R2", RoomRuleKind.PREFERRED, 1),)),
        )
    )
    instance = make_instance(master)
    rule = RoomPermissionRule()
    assert rule.check(instance, candidate("A", room="R1")) is not None
    assert rule.check(instance, candidate("A", room="R2")) is None
    assert rule.check(instance, candidate("B", room="R1")) is not None
    assert rule.check(instance, candidate("B", room="R2")) is None
    assert rule.check(instance, candidate("D", room="R1")) is None


def test_schedule_fit_rule_rejects_lunch_and_closing() -> None:
    """Regresión B17: ninguna sesión cruza el almuerzo ni el cierre."""
    instance = make_instance()
    rule = ScheduleFitRule()
    assert rule.check(instance, candidate(start=735)) is None
    assert rule.check(instance, candidate(start=750)) is not None
    assert rule.check(instance, candidate(day=4, start=915)) is None
    assert rule.check(instance, candidate(day=4, start=930)) is not None
    assert rule.check(instance, candidate(start=545)) is not None


def test_demand_rule() -> None:
    instance = make_instance()
    assert DemandRule().check(instance, candidate(start=585)) is None
    assert DemandRule().check(instance, candidate(start=600)) is not None
    assert DemandRule().check(instance, candidate(service_code="FAM")) is not None


def test_rule_chain_requires_person_rules_first() -> None:
    with pytest.raises(ValueError, match="primero"):
        RuleChain([AvailabilityRule(), ContractRule()])
    with pytest.raises(ValueError, match="únicos"):
        RuleChain([ContractRule(), ContractRule()])


def test_first_failure_reports_the_first_rule_in_order() -> None:
    master = small_master(staff=(person("A", contracts=()),))
    failure = RuleChain().first_failure(make_instance(master), candidate(room="ADM", start=750))
    assert failure is not None
    assert failure[0].code == "contrato"


def _varied_master() -> object:
    return small_master(
        staff=(
            person("A", room_rules=(RoomRule("R1", RoomRuleKind.FORBIDDEN),)),
            person("B", hours=22, availability=(AvailabilityWindow(0, 480, 780), AvailabilityWindow(2, 840, 1020))),
            person("C", role="AD"),
            person("D", skills=frozenset({"IND"}), contracts=()),
        ),
        blockings=(
            meeting(2, 540, 660, Audience.SERVICE_STAFF),
            Blocking("Preparación", None, 480, 495, Audience.ALL),
        ),
        absences=(Absence("A", MONDAY, 600, 720, "Permiso", AbsenceStatus.APPROVED),),
        demand=(
            DemandItem(0, 540, "IND", 2, 2),
            DemandItem(0, 600, "EVA", 1, 1),
            DemandItem(2, 540, "IND", 1, 2),
            DemandItem(2, 900, "GRP", 1, 3),
            DemandItem(4, 900, "FAM", 1, 2),
        ),
    )


def test_generator_matches_brute_force_first_failure() -> None:
    """El generador por niveles da los mismos candidatos y conteos que evaluar la cadena uno por uno."""
    instance = make_instance(_varied_master())  # type: ignore[arg-type]
    chain = RuleChain()
    expected: list[Candidate] = []
    counts: Counter[str] = Counter()
    total = 0
    for staff in instance.staff:
        for kind in instance.service_types:
            for room in instance.rooms:
                for day in instance.days:
                    for start in day.hours.slot_starts():
                        cand = Candidate(staff.code, kind.code, room.code, day.index, start, kind.duration_min)
                        total += 1
                        failure = chain.first_failure(instance, cand)
                        if failure is None:
                            expected.append(cand)
                        else:
                            counts[failure[0].code] += 1
    result = generate_candidates(instance, chain)
    assert result.evaluated == total
    assert len(result.candidates) == len(expected)
    assert {sc.candidate for sc in result.candidates} == set(expected)
    assert {code: n for code, n in result.exclusions.items() if n} == dict(counts)
    assert sum(result.exclusions.values()) + len(result.candidates) == total


def test_candidates_are_sorted_by_priority_score() -> None:
    master = small_master(
        demand=(DemandItem(0, 540, "IND", 1, 3), DemandItem(0, 600, "IND", 1, 1)),
        staff=(person("A", room_rules=(RoomRule("R2", RoomRuleKind.PREFERRED, 1),)),),
    )
    result = generate_candidates(make_instance(master))
    scores = [sc.score for sc in result.candidates]
    assert scores == sorted(scores, reverse=True)
    best = result.candidates[0].candidate
    assert (best.start // 60 * 60, best.room) == (600, "R2")
    by_room = {sc.candidate.room: sc.score for sc in result.candidates if sc.candidate.start == 600}
    assert by_room["R2"] > by_room["R1"]


def test_demand_without_candidates_reports_dominant_rule() -> None:
    master = small_master(
        blockings=(meeting(2, 480, 780, Audience.SERVICE_STAFF),),
        demand=(DemandItem(2, 540, "IND", 1, 2), DemandItem(0, 540, "IND", 1, 2)),
    )
    result = generate_candidates(make_instance(master))
    blocked = result.diagnosis[(2, 540, "IND")]
    assert blocked.candidates == 0
    assert blocked.dominant_rule == "bloqueo"
    assert result.diagnosis[(0, 540, "IND")].candidates > 0


def test_demand_nobody_can_serve_reports_competence() -> None:
    master = small_master(
        roles=(Role("CL", "Clínico", True, frozenset({"IND"})), Role("AD", "Apoyo", False)),
        demand=(DemandItem(0, 540, "FAM", 1, 2),),
    )
    diagnosis = generate_candidates(make_instance(master)).diagnosis[(0, 540, "FAM")]
    assert diagnosis.candidates == 0
    assert diagnosis.dominant_rule == "competencia"


def test_diagnosis_counts_the_candidates_of_its_key() -> None:
    result = generate_candidates(make_instance())
    selected = [sc.candidate for sc in result.candidates if (sc.candidate.day, sc.candidate.service) == (0, "IND")]
    assert selected
    assert all(540 <= c.start < 600 for c in selected)
    assert result.diagnosis[(0, 540, "IND")].candidates == len(selected)


def test_replace_keeps_candidate_hashable() -> None:
    cand = candidate()
    assert replace(cand, start=585) != cand
    assert len({cand, candidate()}) == 1
