"""Heurística voraz: coloca candidatos respetando todas las restricciones duras."""

from __future__ import annotations

from collections import Counter

from factories import contract_minutes, make_instance, person, small_master
from optibox.domain.greedy import CapacityLedger, greedy_plan, weighted_coverage
from optibox.domain.instance import PlanningInstance
from optibox.domain.models import DemandItem, MasterData
from optibox.domain.plan import Plan
from optibox.domain.rules import generate_candidates
from optibox.domain.validator import validate_plan


def _plan(master: MasterData) -> tuple[PlanningInstance, Plan]:
    instance = make_instance(master)
    return instance, greedy_plan(instance, generate_candidates(instance))


def test_contract_counts_sessions_and_associated_admin() -> None:
    """Regresión B02 y B10: sesión más administrativo asociado no pueden superar el contrato."""
    master = small_master(
        staff=(person("A", contracts=contract_minutes(90)),),
        demand=(DemandItem(0, 540, "IND", 5, 2),),
    )
    instance, plan = _plan(master)
    assert len(plan.sessions) == 1
    assert plan.admin_minutes("A") == 15
    assert validate_plan(instance, plan) == ()


def test_associated_admin_is_placed_the_same_day() -> None:
    """Regresión B04: cada sesión deja su administrativo asociado el mismo día."""
    master = small_master(
        demand=(
            DemandItem(0, 540, "IND", 2, 2),
            DemandItem(1, 600, "EVA", 1, 1),
            DemandItem(3, 900, "FAM", 1, 2),
        )
    )
    instance, plan = _plan(master)
    required: Counter[tuple[str, int]] = Counter()
    for s in plan.sessions:
        required[(s.staff, s.day)] += instance.service_by_code[s.service].admin_minutes
    assert {day for _, day in required} == {0, 1, 3}
    for (staff, day), minutes in required.items():
        assert plan.admin_minutes(staff, day) >= minutes
    assert validate_plan(instance, plan) == ()


def test_greedy_never_double_books() -> None:
    """Regresión B03: la sesión nunca se coloca encima de administrativo ni de otra sesión."""
    master = small_master(
        demand=tuple(DemandItem(day, block, "IND", 3, 2) for day in range(5) for block in (540, 600, 660, 840, 900))
    )
    instance, plan = _plan(master)
    assert len(plan.sessions) > 10
    assert validate_plan(instance, plan) == ()


def test_greedy_respects_weekly_caps() -> None:
    """Regresión B06 y H1: el tope semanal cuenta sesiones (2 talleres), no slots."""
    master = small_master(demand=tuple(DemandItem(day, 900, "GRP", 1, 2) for day in range(4)))
    _, plan = _plan(master)
    assert sum(1 for s in plan.sessions if s.service == "GRP") == 2


def test_ledger_from_plan_reproduces_occupation() -> None:
    instance, plan = _plan(small_master())
    ledger = CapacityLedger.from_plan(instance, plan)
    assert sum(ledger.coverage.values()) == len(plan.sessions)
    admin_total = sum(block.duration for block in plan.admin)
    assert ledger.used["A"] + ledger.used["B"] == plan.session_minutes() + admin_total
    assert weighted_coverage(instance, plan.sessions) == 4
