"""Etapa 2: modelo CP-SAT en instancias pequeñas con óptimo conocido."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta

import pytest

from factories import MONDAY, contract_minutes, make_instance, person, service, small_master, solve
from optibox.domain.metrics import compute_metrics
from optibox.domain.models import (
    Audience,
    AvailabilityWindow,
    Blocking,
    Contract,
    DemandItem,
    PlanningSettings,
    Role,
    Room,
    RoomKind,
    RoomRule,
    RoomRuleKind,
    Scenario,
)
from optibox.domain.optimizer import optimize
from optibox.domain.rules import generate_candidates
from optibox.domain.validator import validate_plan
from optibox.errors import OperationCancelledError


def test_small_instance_reaches_known_optimum() -> None:
    instance = make_instance()
    result = solve(instance)
    assert result.status == "óptimo"
    assert result.coverage_value == 4
    assert len(result.plan.sessions) == 2
    assert result.phase_a.objective == 4
    assert validate_plan(instance, result.plan) == ()


def test_capacity_shortfall_optimum() -> None:
    """Dos personas pueden iniciar a lo más dos sesiones cada una en el bloque de 09:00: óptimo 4 de 5."""
    instance = make_instance(small_master(demand=(DemandItem(0, 540, "IND", 5, 2),)))
    result = solve(instance)
    assert len(result.plan.sessions) == 4
    assert result.coverage_value == 8
    assert result.phase_a.bound == 8
    assert validate_plan(instance, result.plan) == ()


def test_optimized_is_never_worse_than_greedy() -> None:
    demand = tuple(
        DemandItem(day, block, code, 2, 2) for day in range(5) for block in (540, 840) for code in ("IND", "FAM")
    )
    instance = make_instance(small_master(demand=demand))
    result = solve(instance)
    assert result.coverage_value >= result.greedy_value
    assert validate_plan(instance, result.plan) == ()
    assert validate_plan(instance, result.greedy) == ()


def test_group_sessions_are_scheduled_when_demanded() -> None:
    """Regresión B08: los talleres grupales se programan si hay demanda y capacidad."""
    master = small_master(demand=(DemandItem(1, 900, "GRP", 1, 2), DemandItem(1, 900, "IND", 1, 2)))
    instance = make_instance(master)
    result = solve(instance)
    groups = [s for s in result.plan.sessions if s.service == "GRP"]
    assert len(groups) == 1
    assert groups[0].room == "R2"
    assert groups[0].participants == 4
    assert validate_plan(instance, result.plan) == ()


def test_scenarios_change_the_plan_when_there_is_slack() -> None:
    """Regresión B01: con la misma cobertura posible, los pesos del escenario deciden el plan."""
    master = small_master(
        staff=(person("A", contracts=contract_minutes(60)),),
        demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(0, 540, "FAM", 1, 2)),
    )
    individual = Scenario("Individual", "", service_weights={"IND": 5.0, "FAM": 1.0}, id=1)
    family = Scenario("Familiar", "", service_weights={"IND": 1.0, "FAM": 5.0}, id=2)
    first = solve(make_instance(master, individual))
    second = solve(make_instance(master, family))
    assert first.coverage_value == second.coverage_value == 2
    assert [s.service for s in first.plan.sessions] == ["IND"]
    assert [s.service for s in second.plan.sessions] == ["FAM"]


def test_coverage_floor_lets_a_scenario_trade_coverage_for_quality() -> None:
    """El piso de cobertura del escenario acota cuánto puede ceder la fase B."""
    master = small_master(
        staff=(person("A", contracts=contract_minutes(180)),),
        demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(0, 540, "EVA", 1, 3)),
    )
    strict = Scenario("Estricto", "", service_weights={"EVA": 10.0, "IND": 1.0}, coverage_floor_pct=100, id=1)
    relaxed = Scenario("Flexible", "", service_weights={"EVA": 10.0, "IND": 1.0}, coverage_floor_pct=50, id=2)
    assert [s.service for s in solve(make_instance(master, strict)).plan.sessions] == ["IND"]
    assert [s.service for s in solve(make_instance(master, relaxed)).plan.sessions] == ["EVA"]


def test_contract_includes_admin_and_blockings() -> None:
    """Regresión B02: sesiones + administrativo + bloqueos <= contrato."""
    master = small_master(
        staff=(person("A", contracts=contract_minutes(120)),),
        blockings=(Blocking("Coordinación", 2, 600, 660, Audience.STAFF, "A"),),
        demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(1, 540, "IND", 1, 2), DemandItem(3, 540, "IND", 1, 2)),
    )
    instance = make_instance(master)
    result = solve(instance)
    assert len(result.plan.sessions) == 1
    used = result.plan.session_minutes("A") + result.plan.admin_minutes("A") + 60
    assert used <= 120
    assert validate_plan(instance, result.plan) == ()


def test_sessions_stay_inside_the_contract_validity() -> None:
    """Regresión: con un contrato que termina el martes solo se asignan sesiones el lunes y el martes."""
    contracts = (Contract(date(2026, 1, 1), MONDAY + timedelta(days=1), 44 * 60),)
    master = small_master(
        staff=(person("A", contracts=contracts),),
        demand=tuple(DemandItem(day, 540, "IND", 1, 2) for day in range(5)),
    )
    instance = make_instance(master)
    result = solve(instance)
    assert sorted({s.day for s in result.plan.sessions}) == [0, 1]
    assert {b.day for b in result.plan.admin} <= {0, 1}
    assert validate_plan(instance, result.plan) == ()


def test_associated_admin_only_where_there_are_sessions() -> None:
    """Regresión B04 y B05: administrativo asociado >= exigido y nunca sin sesiones si no hay adicional."""
    settings = PlanningSettings(time_limit_s=10, workers=4, extra_admin_daily_cap_min=0)
    master = small_master(
        settings=settings,
        demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(2, 600, "EVA", 1, 1)),
    )
    instance = make_instance(master)
    result = solve(instance)
    metrics = compute_metrics(instance, result.plan)
    assert metrics.totals.admin_min == metrics.totals.admin_required_min == 15 + 60
    session_days = {(s.staff, s.day) for s in result.plan.sessions}
    assert {(b.staff, b.day) for b in result.plan.admin} == session_days


def test_extra_admin_respects_daily_cap() -> None:
    instance = make_instance(small_master(demand=(DemandItem(0, 540, "IND", 1, 2),)))
    result = solve(instance)
    per_day: Counter[tuple[str, int]] = Counter()
    for block in result.plan.admin:
        per_day[(block.staff, block.day)] += block.duration
    required = {(s.staff, s.day): 15 for s in result.plan.sessions}
    for key, minutes in per_day.items():
        assert minutes <= required.get(key, 0) + instance.settings.extra_admin_daily_cap_min
    assert validate_plan(instance, result.plan) == ()


def test_caps_are_hard_and_count_sessions() -> None:
    """Regresión B06 y H1: tope semanal de 2 talleres permite 2 (no 1); tope diario por persona."""
    groups = small_master(demand=tuple(DemandItem(day, 900, "GRP", 1, 2) for day in range(4)))
    result = solve(make_instance(groups))
    assert sum(1 for s in result.plan.sessions if s.service == "GRP") == 2

    capped = small_master(
        service_types=(service("IND", max_day_per_staff=1),),
        staff=(person("A"),),
        demand=(DemandItem(0, 540, "IND", 3, 2),),
    )
    assert len(solve(make_instance(capped)).plan.sessions) == 1


def test_coverage_never_exceeds_demand_and_rooms_are_exclusive() -> None:
    master = small_master(
        staff=(person("A"), person("B"), person("D")),
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("R2", "Sala de usos múltiples", RoomKind.MULTIPURPOSE, True, True, 4, active=False),
            Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, simultaneous_hard=3),
        ),
        demand=(DemandItem(0, 540, "IND", 3, 2),),
    )
    instance = make_instance(master)
    result = solve(instance)
    assert len(result.plan.sessions) == 2
    assert {s.room for s in result.plan.sessions} == {"R1"}
    assert validate_plan(instance, result.plan) == ()


def test_no_candidates_returns_an_empty_plan() -> None:
    master = small_master(
        roles=(Role("CL", "Clínico", True, frozenset({"IND"})), Role("AD", "Apoyo", False)),
        demand=(DemandItem(0, 540, "FAM", 1, 2),),
    )
    result = solve(make_instance(master))
    assert result.plan.sessions == ()
    assert result.coverage_value == 0
    assert result.phase_b is None


def test_higher_priority_demand_wins_a_genuine_trade_off() -> None:
    """Regresión B09: la prioridad de la demanda decide qué se cubre cuando dos atenciones compiten por una persona.

    La prioridad entra como peso de la fase A, no como un orden fijo de fases por tipo de atención.
    """
    master = small_master(
        staff=(person("A", contracts=contract_minutes(60)),),
        demand=(DemandItem(0, 540, "IND", 1, 1), DemandItem(0, 540, "FAM", 1, 3)),
    )
    result = solve(make_instance(master))
    assert [s.service for s in result.plan.sessions] == ["IND"]


def test_evaluation_admin_only_consumes_the_contract_of_who_evaluates() -> None:
    """Regresión B09: el administrativo de las evaluaciones no se reserva a quien no está habilitado para evaluar.

    A no evalúa y su contrato alcanza justo para dos atenciones con su administrativo asociado. Una reserva
    general de tiempo para evaluaciones le quitaría una de ellas.
    """
    roles = (
        Role("CL", "Clínico", True, frozenset({"IND", "FAM"})),
        Role("EV", "Evaluador", True, frozenset({"EVA"})),
        Role("AD", "Apoyo administrativo", False),
    )
    master = small_master(
        roles=roles,
        staff=(person("A", contracts=contract_minutes(120)), person("B", role="EV"), person("C", role="AD")),
        demand=(DemandItem(0, 540, "IND", 1, 2), DemandItem(1, 540, "IND", 1, 2), DemandItem(2, 600, "EVA", 1, 1)),
    )
    instance = make_instance(master)
    result = solve(instance)
    assert [s.service for s in result.plan.sessions if s.staff == "A"] == ["IND", "IND"]
    assert result.plan.session_minutes("A") + result.plan.admin_minutes("A") == 120
    assert [s.service for s in result.plan.sessions if s.staff == "B"] == ["EVA"]
    assert validate_plan(instance, result.plan) == ()


def test_plan_value_does_not_depend_on_staff_or_room_codes() -> None:
    """Regresión H5: el modelo no depende de códigos fijos; renombrar personas y salas no cambia el óptimo.

    La sala administrativa, las salas de grupo y de evaluación se reconocen por su tipo y sus flags.
    """
    demand = (DemandItem(0, 540, "IND", 2, 2), DemandItem(1, 600, "EVA", 1, 1), DemandItem(2, 900, "GRP", 1, 3))
    original = small_master(demand=demand)
    rooms = {"R1": "SALA-X", "R2": "SALA-Y", "ADM": "REG-Z"}
    staff = {"A": "Z-9", "B": "M-4", "C": "A-1"}
    renamed = small_master(
        demand=demand,
        service_types=tuple(replace(s, rooms=frozenset(rooms[r] for r in s.rooms)) for s in original.service_types),
        rooms=tuple(replace(r, code=rooms[r.code]) for r in original.rooms),
        staff=tuple(replace(p, code=staff[p.code]) for p in original.staff),
    )
    first = solve(make_instance(original))
    second_instance = make_instance(renamed)
    second = solve(second_instance)
    assert first.status == second.status == "óptimo"
    assert first.coverage_value == second.coverage_value == 9
    assert first.quality_value == second.quality_value
    assert {s.room for s in second.plan.sessions} <= {"SALA-X", "SALA-Y"}
    assert validate_plan(second_instance, second.plan) == ()


def test_room_continuity_weight_keeps_staff_in_the_same_room() -> None:
    """Regresión H3: el peso de continuidad de sala cambia de verdad el plan, no solo se declara en el escenario."""
    roles = (
        Role("CL", "Clínico", True, frozenset({"IND", "FAM", "GRP"})),
        Role("EV", "Evaluador", True, frozenset({"EVA"})),
        Role("AD", "Apoyo administrativo", False),
    )
    staff = (
        person("A", room_rules=(RoomRule("R2", RoomRuleKind.PREFERRED, rank=1),)),
        # Disponibilidad ajustada para que la evaluación de B solo pueda empezar a las 09:00
        # (deja exactamente 60 minutos antes para su administrativo asociado): así ocupa
        # la sala R2 durante todo el primer bloque de la demanda de A.
        person(
            "B",
            role="EV",
            availability=(AvailabilityWindow(0, 480, 540), AvailabilityWindow(0, 540, 660)),
        ),
        person("C", role="AD"),
    )
    demand = (
        DemandItem(0, 540, "EVA", 1, 1),
        DemandItem(0, 540, "IND", 1, 2),
        DemandItem(0, 720, "IND", 1, 2),
    )
    master = small_master(roles=roles, staff=staff, demand=demand)

    def rooms_used_by_a(scenario: Scenario) -> set[str]:
        result = solve(make_instance(master, scenario))
        assert result.coverage_value == 8
        return {s.room for s in result.plan.sessions if s.staff == "A"}

    default = Scenario("Base", "", id=1)
    strong_continuity = Scenario("Continuo", "", room_continuity_weight=20, id=2)
    assert rooms_used_by_a(default) == {"R1", "R2"}
    assert rooms_used_by_a(strong_continuity) == {"R1"}


def test_mix_slack_is_reported() -> None:
    """Regresión H2: una cota de mezcla inalcanzable se resuelve con holgura penalizada y reportada.

    La cota no debe actuar como restricción dura oculta: la corrida sigue siendo factible.
    """
    scenario = Scenario("Mezcla", "", mix_min={"FAM": 0.9}, id=1)
    instance = make_instance(small_master(), scenario)
    result = solve(instance)
    assert result.mix_slack["FAM"] > 0
    mix = compute_metrics(instance, result.plan).mix
    assert [row.service for row in mix] == ["FAM"]
    assert mix[0].deviation_min > 0


def test_deterministic_mode_is_reproducible() -> None:
    demand = tuple(DemandItem(day, 540, "IND", 2, 2) for day in range(5))
    instance = make_instance(small_master(demand=demand))
    assert solve(instance).plan == solve(instance).plan


def test_cancel_raises_and_returns_nothing() -> None:
    instance = make_instance()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelledError):
        optimize(instance, generate_candidates(instance), time_limit_s=2, cancel=cancel)


def test_progress_is_reported_until_done() -> None:
    instance = make_instance()
    seen: list[int] = []
    optimize(
        instance,
        generate_candidates(instance),
        time_limit_s=2,
        deterministic=True,
        progress=lambda pct, _msg: seen.append(pct),
    )
    assert seen[0] < seen[-1] == 100
    assert seen == sorted(seen)


def test_time_limit_defaults_to_settings() -> None:
    master = small_master(settings=PlanningSettings(time_limit_s=1, workers=2))
    instance = make_instance(replace(master))
    result = optimize(instance, generate_candidates(instance), deterministic=True)
    assert result.coverage_value == 4
