"""Etapa 2: modelo CP-SAT indexado por tiempo con objetivo lexicográfico.

Fase A: maximiza la demanda cubierta ponderada por prioridad.
Fase B: fija la cobertura de la fase A (con el piso del escenario) y maximiza
la calidad: pesos del escenario por tipo de atención, preferencia y
continuidad de sala, metas por persona, mezcla por tipo y carga de la sala
administrativa. La solución voraz es el hint inicial de la fase A y la
solución de la fase A es el hint de la fase B.

En modo determinista los límites se expresan en tiempo determinista de
CP-SAT y la búsqueda paralela se intercala, de modo que dos corridas con los
mismos datos, semilla y trabajadores producen el mismo plan (se usa en los
tests). En modo normal los límites son de tiempo real.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial

from ortools.sat.python import cp_model

from optibox.domain.greedy import greedy_plan, weighted_coverage
from optibox.domain.instance import PlanningInstance
from optibox.domain.models import PRIORITY_WEIGHTS, frozen_mapping
from optibox.domain.plan import Candidate, Plan, Session, admin_blocks_from_slots, admin_masks
from optibox.domain.rules import Stage1Result
from optibox.domain.timegrid import SLOT_MINUTES, block_of, mask_slots
from optibox.errors import OperationCancelledError

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, str], None]

QUALITY_SCALE = 100
# Bajo este límite (segundos) la fase B se resuelve sin presolve para llegar antes a una solución.
FAST_START_LIMIT_S = 5.0
STATUS_LABELS = {
    cp_model.OPTIMAL: "óptimo",
    cp_model.FEASIBLE: "factible",
    cp_model.INFEASIBLE: "infactible",
    cp_model.MODEL_INVALID: "modelo inválido",
    cp_model.UNKNOWN: "sin solución",
}


@dataclass(frozen=True)
class PhaseResult:
    """Resultado de una fase del solver."""

    name: str
    status: str
    objective: int | None
    bound: int | None
    seconds: float


@dataclass(frozen=True)
class OptimizationResult:
    """Plan final y trazabilidad de la corrida (fases, heurística, tiempos)."""

    plan: Plan
    status: str
    phase_a: PhaseResult
    phase_b: PhaseResult | None
    greedy: Plan
    greedy_value: int
    coverage_value: int
    quality_value: int | None
    build_seconds: float
    total_seconds: float
    variables: int
    mix_slack: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mix_slack", frozen_mapping(self.mix_slack))


class _Progress(cp_model.CpSolverSolutionCallback):
    """Informa el avance del solver en cada solución nueva."""

    def __init__(self, report: ProgressFn, label: str, start_pct: int, end_pct: int, limit_s: float) -> None:
        super().__init__()
        self._report = report
        self._label = label
        self._start = start_pct
        self._end = end_pct
        self._limit = max(limit_s, 0.001)

    def on_solution_callback(self) -> None:
        share = min(1.0, self.wall_time / self._limit)
        pct = int(self._start + (self._end - self._start) * share)
        self._report(pct, f"{self._label}: solución con valor {int(self.objective_value)}")


class _Model:
    """Modelo CP-SAT con las restricciones duras comunes a ambas fases."""

    def __init__(self, instance: PlanningInstance, stage1: Stage1Result) -> None:
        self.instance = instance
        self.model = cp_model.CpModel()
        self.candidates: list[Candidate] = [sc.candidate for sc in stage1.candidates]
        self.x = [self.model.new_bool_var(f"x_{k}") for k in range(len(self.candidates))]
        self.admin: dict[tuple[str, int, int], cp_model.IntVar] = {}
        self.admin_per_slot: dict[tuple[int, int], list[cp_model.IntVar]] = defaultdict(list)
        self._build_admin_vars()
        self._add_staff_and_room_exclusivity()
        self._add_admin_room_capacity()
        self._add_contracts()
        self._add_associated_admin()
        self._add_caps()
        self._add_coverage()
        weights = [
            PRIORITY_WEIGHTS[instance.demand_by_key[(c.day, block_of(c.start), c.service)].priority]
            for c in self.candidates
        ]
        self.coverage_expr = cp_model.LinearExpr.weighted_sum(self.x, weights)

    def _build_admin_vars(self) -> None:
        if self.instance.admin_room is None:
            # Sin sala administrativa no hay dónde hacer administrativo; la instancia ya exige una si hace falta.
            return
        for staff in self.instance.staff:
            if not staff.delivers_services or staff.assignable_minutes <= 0:
                continue
            for day in self.instance.days:
                if not day.is_open:
                    continue
                for start in mask_slots(staff.days[day.index].workable):
                    self.admin[(staff.code, day.index, start)] = self.model.new_bool_var(
                        f"a_{staff.code}_{day.index}_{start}"
                    )

    def _add_staff_and_room_exclusivity(self) -> None:
        staff_slots: dict[tuple[str, int, int], list[cp_model.IntVar]] = defaultdict(list)
        room_slots: dict[tuple[str, int, int], list[cp_model.IntVar]] = defaultdict(list)
        for k, cand in enumerate(self.candidates):
            for start in range(cand.start, cand.end, SLOT_MINUTES):
                staff_slots[(cand.staff, cand.day, start)].append(self.x[k])
                room_slots[(cand.room, cand.day, start)].append(self.x[k])
        for key, var in self.admin.items():
            staff_slots[key].append(var)
        for group in (staff_slots, room_slots):
            for variables in group.values():
                if len(variables) > 1:
                    self.model.add_at_most_one(variables)

    def _add_admin_room_capacity(self) -> None:
        for (_, day, start), var in self.admin.items():
            self.admin_per_slot[(day, start)].append(var)
        room = self.instance.admin_room
        if room is None or room.simultaneous_hard is None:
            return
        for variables in self.admin_per_slot.values():
            if len(variables) > room.simultaneous_hard:
                self.model.add(sum(variables) <= room.simultaneous_hard)

    def _add_contracts(self) -> None:
        variables: dict[str, list[cp_model.IntVar]] = defaultdict(list)
        minutes: dict[str, list[int]] = defaultdict(list)
        for k, cand in enumerate(self.candidates):
            variables[cand.staff].append(self.x[k])
            minutes[cand.staff].append(cand.duration)
        for (staff, _, _), var in self.admin.items():
            variables[staff].append(var)
            minutes[staff].append(SLOT_MINUTES)
        for code, staff_vars in variables.items():
            limit = self.instance.staff_by_code[code].assignable_minutes
            if sum(minutes[code]) > limit:
                self.model.add(cp_model.LinearExpr.weighted_sum(staff_vars, minutes[code]) <= limit)

    def _add_associated_admin(self) -> None:
        extra = self.instance.settings.extra_admin_daily_cap_min
        required: dict[tuple[str, int], list[cp_model.LinearExprT]] = defaultdict(list)
        for k, cand in enumerate(self.candidates):
            minutes = self.instance.service_by_code[cand.service].admin_minutes
            if minutes:
                required[(cand.staff, cand.day)].append(minutes * self.x[k])
        placed: dict[tuple[str, int], list[cp_model.IntVar]] = defaultdict(list)
        for (staff, day, _), var in self.admin.items():
            placed[(staff, day)].append(var)
        for key in sorted(set(required) | set(placed)):
            admin_minutes = SLOT_MINUTES * sum(placed.get(key, []))
            needed = sum(required.get(key, []))
            if key in required:
                self.model.add(admin_minutes >= needed)
            if key in placed:
                self.model.add(admin_minutes <= needed + extra)
        self.admin_by_staff_day = placed

    def _add_caps(self) -> None:
        groups: dict[tuple[object, ...], list[cp_model.IntVar]] = defaultdict(list)
        for k, cand in enumerate(self.candidates):
            groups[("week", cand.service)].append(self.x[k])
            groups[("week_staff", cand.service, cand.staff)].append(self.x[k])
            groups[("day", cand.service, cand.day)].append(self.x[k])
            groups[("day_staff", cand.service, cand.staff, cand.day)].append(self.x[k])
        for key, variables in groups.items():
            service = self.instance.service_by_code[str(key[1])]
            cap = {
                "week": service.max_week_total,
                "week_staff": service.max_week_per_staff,
                "day": service.max_day_total,
                "day_staff": service.max_day_per_staff,
            }[str(key[0])]
            if cap is not None and len(variables) > cap:
                self.model.add(sum(variables) <= cap)

    def _add_coverage(self) -> None:
        groups: dict[tuple[int, int, str], list[cp_model.IntVar]] = defaultdict(list)
        for k, cand in enumerate(self.candidates):
            groups[(cand.day, block_of(cand.start), cand.service)].append(self.x[k])
        for key, variables in groups.items():
            demand = self.instance.demand_by_key[key].sessions
            if len(variables) > demand:
                self.model.add(sum(variables) <= demand)

    def add_hint(self, plan: Plan) -> None:
        self.model.clear_hints()
        chosen = {session.candidate for session in plan.sessions}
        for k, cand in enumerate(self.candidates):
            self.model.add_hint(self.x[k], cand in chosen)
        masks = admin_masks(plan.admin)
        for (staff, day, start), var in self.admin.items():
            self.model.add_hint(var, bool(masks.get((staff, day), 0) >> (start // SLOT_MINUTES) & 1))

    def extract(self, solver: cp_model.CpSolver) -> Plan:
        sessions: list[Session] = []
        for k, cand in enumerate(self.candidates):
            if solver.boolean_value(self.x[k]):
                service = self.instance.service_by_code[cand.service]
                room = self.instance.room_by_code[cand.room]
                sessions.append(
                    Session(
                        staff=cand.staff,
                        service=cand.service,
                        room=cand.room,
                        day=cand.day,
                        start=cand.start,
                        duration=cand.duration,
                        participants=self.instance.participants(service, room),
                    )
                )
        slots: dict[tuple[str, int], int] = defaultdict(int)
        for (staff, day, start), var in self.admin.items():
            if solver.boolean_value(var):
                slots[(staff, day)] |= 1 << (start // SLOT_MINUTES)
        return Plan(sessions=tuple(sessions), admin=admin_blocks_from_slots(dict(slots)))

    def add_quality_objective(self) -> dict[str, cp_model.IntVar]:
        """Agrega las restricciones blandas de la fase B y fija su objetivo. Devuelve las holguras de mezcla."""
        instance = self.instance
        scenario = instance.scenario
        settings = instance.settings
        m = self.model
        terms: list[cp_model.LinearExprT] = []

        for k, cand in enumerate(self.candidates):
            weight = round(10 * scenario.service_weights.get(cand.service, 1.0))
            rank = instance.staff_by_code[cand.staff].staff.preference_rank(cand.room)
            preference = max(0, 4 - rank) if rank is not None else 0
            coefficient = QUALITY_SCALE * (weight + scenario.room_preference_weight * preference)
            if coefficient:
                terms.append(coefficient * self.x[k])

        if scenario.room_continuity_weight:
            rooms_used: dict[tuple[str, int, str], list[cp_model.IntVar]] = defaultdict(list)
            for k, cand in enumerate(self.candidates):
                rooms_used[(cand.staff, cand.day, cand.room)].append(self.x[k])
            for key, variables in rooms_used.items():
                used = m.new_bool_var(f"u_{key[0]}_{key[1]}_{key[2]}")
                m.add_max_equality(used, variables)
                terms.append(-QUALITY_SCALE * scenario.room_continuity_weight * used)

        room = instance.admin_room
        if scenario.admin_excess_weight and room is not None and room.simultaneous_hard is not None:
            soft = room.soft_capacity
            for (day, start), variables in self.admin_per_slot.items():
                if len(variables) <= soft:
                    continue
                excess = m.new_int_var(0, room.simultaneous_hard - soft, f"e_{day}_{start}")
                m.add(excess >= sum(variables) - soft)
                terms.append(-QUALITY_SCALE * scenario.admin_excess_weight * excess)

        if settings.extra_admin_weight:
            terms.extend(QUALITY_SCALE * settings.extra_admin_weight * var for var in self.admin.values())

        if scenario.target_weight:
            by_staff_service: dict[tuple[str, str], list[cp_model.LinearExprT]] = defaultdict(list)
            for k, cand in enumerate(self.candidates):
                by_staff_service[(cand.staff, cand.service)].append((cand.duration // SLOT_MINUTES) * self.x[k])
            for staff in instance.staff:
                for target in staff.staff.targets:
                    expr = sum(by_staff_service.get((staff.code, target.service_code), []))
                    top = sum(c.duration // SLOT_MINUTES for c in self.candidates if c.staff == staff.code) + 1
                    if target.min_minutes is not None:
                        under = m.new_int_var(0, math.ceil(target.min_minutes / SLOT_MINUTES), "under")
                        m.add(under >= target.min_minutes // SLOT_MINUTES - expr)
                        terms.append(-QUALITY_SCALE * scenario.target_weight * under)
                    if target.max_minutes is not None:
                        over = m.new_int_var(0, top, "over")
                        m.add(over >= expr - target.max_minutes // SLOT_MINUTES)
                        terms.append(-QUALITY_SCALE * scenario.target_weight * over)

        slacks: dict[str, cp_model.IntVar] = {}
        if scenario.mix_weight and (scenario.mix_min or scenario.mix_max):
            slots_by_service: dict[str, list[cp_model.LinearExprT]] = defaultdict(list)
            total_slots = 0
            for k, cand in enumerate(self.candidates):
                slots_by_service[cand.service].append((cand.duration // SLOT_MINUTES) * self.x[k])
                total_slots += cand.duration // SLOT_MINUTES
            total = sum(expr for exprs in slots_by_service.values() for expr in exprs)
            bound = 100 * max(total_slots, 1)
            for code in sorted(set(scenario.mix_min) | set(scenario.mix_max)):
                share = sum(slots_by_service.get(code, []))
                slack = m.new_int_var(0, 2 * bound, f"mix_{code}")
                low = round(100 * scenario.mix_min.get(code, 0.0))
                high = round(100 * scenario.mix_max.get(code, 1.0))
                if low > 0:
                    under = m.new_int_var(0, bound, f"mix_lo_{code}")
                    m.add(100 * share + under >= low * total)
                else:
                    under = m.new_constant(0)
                if high < 100:
                    over = m.new_int_var(0, bound, f"mix_hi_{code}")
                    m.add(100 * share - over <= high * total)
                else:
                    over = m.new_constant(0)
                m.add(slack == under + over)
                slacks[code] = slack
                terms.append(-scenario.mix_weight * slack)

        m.maximize(sum(terms) if terms else 0)
        return slacks


def _run_phase(
    model: _Model,
    name: str,
    limit_s: float,
    *,
    deterministic: bool,
    progress: ProgressFn | None,
    cancel: threading.Event | None,
    pct_range: tuple[int, int],
    fast_start: bool = False,
) -> tuple[cp_model.CpSolver, int, float]:
    settings = model.instance.settings
    solver = cp_model.CpSolver()
    params = solver.parameters
    params.random_seed = settings.seed
    params.num_workers = settings.workers
    params.log_search_progress = False
    if fast_start and not deterministic and limit_s < FAST_START_LIMIT_S:
        # Con poco tiempo real el presolve consume casi todo el presupuesto antes de la primera solución.
        params.cp_model_presolve = False
    if deterministic:
        params.interleave_search = True
        params.max_deterministic_time = limit_s
        params.max_time_in_seconds = limit_s * 10 + 30
    else:
        params.max_time_in_seconds = limit_s
    callback = _Progress(progress, name, pct_range[0], pct_range[1], limit_s) if progress else None
    done = threading.Event()
    watcher: threading.Thread | None = None
    if cancel is not None:

        def watch() -> None:
            while not done.wait(0.1):
                if cancel.is_set():
                    solver.stop_search()
                    return

        watcher = threading.Thread(target=watch, name=f"cancel-{name}", daemon=True)
        watcher.start()
    started = time.perf_counter()
    try:
        status = solver.solve(model.model, callback) if callback else solver.solve(model.model)
    finally:
        done.set()
        if watcher is not None:
            watcher.join()
    elapsed = time.perf_counter() - started
    if cancel is not None and cancel.is_set():
        raise OperationCancelledError("La optimización fue cancelada. No se guardó ninguna corrida.")
    log.info("%s: %s en %.1f s", name, STATUS_LABELS.get(status, str(status)), elapsed)
    return solver, status, elapsed


def _phase_result(name: str, solver: cp_model.CpSolver, status: int, seconds: float) -> PhaseResult:
    has_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    return PhaseResult(
        name=name,
        status=STATUS_LABELS.get(status, str(status)),
        objective=round(solver.objective_value) if has_solution else None,
        bound=math.floor(solver.best_objective_bound + 1e-6) if has_solution else None,
        seconds=round(seconds, 3),
    )


@dataclass(frozen=True)
class _Baseline:
    """Datos comunes a todas las salidas de `optimize`: heurística voraz, tiempos y tamaño del modelo."""

    started: float
    greedy: Plan
    greedy_value: int
    build_seconds: float
    variables: int

    def result(
        self,
        plan: Plan,
        status: str,
        phase_a: PhaseResult,
        phase_b: PhaseResult | None = None,
        *,
        coverage_value: int,
        quality_value: int | None = None,
        mix_slack: Mapping[str, int] | None = None,
    ) -> OptimizationResult:
        return OptimizationResult(
            plan=plan,
            status=status,
            phase_a=phase_a,
            phase_b=phase_b,
            greedy=self.greedy,
            greedy_value=self.greedy_value,
            coverage_value=coverage_value,
            quality_value=quality_value,
            build_seconds=round(self.build_seconds, 3),
            total_seconds=round(time.perf_counter() - self.started, 3),
            variables=self.variables,
            mix_slack=mix_slack or {},
        )


@dataclass(frozen=True)
class _PhaseOutcome:
    """Plan que deja una fase (None si no encontró solución) y su trazabilidad."""

    plan: Plan | None
    phase: PhaseResult
    optimal: bool
    quality: int | None = None
    mix_slack: Mapping[str, int] = field(default_factory=dict)


PhaseRunner = Callable[..., tuple[cp_model.CpSolver, int, float]]


def _coverage_phase(
    model: _Model, base: _Baseline, limit_s: float, run: PhaseRunner, report: ProgressFn
) -> _PhaseOutcome:
    """Fase A: maximiza la cobertura ponderada partiendo de la solución voraz."""
    model.model.maximize(model.coverage_expr)
    model.add_hint(base.greedy)
    report(10, "Fase A: maximizando la cobertura de la demanda")
    solver, status, seconds = run("Fase A", limit_s, pct_range=(10, 60))
    phase = _phase_result("Fase A", solver, status, seconds)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _PhaseOutcome(None, phase, optimal=False)
    plan = model.extract(solver)
    coverage = weighted_coverage(model.instance, plan.sessions)
    if coverage < base.greedy_value:
        # Con poco tiempo la búsqueda podría quedar bajo la solución voraz, que es factible: se conserva la mejor.
        log.info("La fase A quedó bajo la heurística (%d < %d); se usa la solución voraz.", coverage, base.greedy_value)
        plan = base.greedy
    return _PhaseOutcome(plan, phase, optimal=status == cp_model.OPTIMAL)


def _quality_phase(model: _Model, plan_a: Plan, limit_s: float, run: PhaseRunner, report: ProgressFn) -> _PhaseOutcome:
    """Fase B: fija el piso de cobertura del escenario y maximiza la calidad con el plan de la fase A como hint."""
    instance = model.instance
    floor = math.ceil(weighted_coverage(instance, plan_a.sessions) * instance.scenario.coverage_floor_pct / 100)
    model.model.add(model.coverage_expr >= floor)
    slacks = model.add_quality_objective()
    model.add_hint(plan_a)
    report(60, "Fase B: mejorando la calidad del plan")
    solver, status, seconds = run("Fase B", limit_s, pct_range=(60, 98), fast_start=True)
    phase = _phase_result("Fase B", solver, status, seconds)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        log.warning("La fase B no encontró solución; se conserva el plan de la fase A.")
        return _PhaseOutcome(plan_a, phase, optimal=False)
    mix_slack = {code: round(solver.value(var) / 100) for code, var in slacks.items()}
    return _PhaseOutcome(model.extract(solver), phase, status == cp_model.OPTIMAL, phase.objective, mix_slack)


def optimize(
    instance: PlanningInstance,
    stage1: Stage1Result,
    *,
    time_limit_s: float | None = None,
    deterministic: bool = False,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> OptimizationResult:
    """Resuelve la semana en dos fases y devuelve el mejor plan encontrado.

    Si la fase B no encuentra solución se conserva la de la fase A; si la
    fase A tampoco, se usa el plan voraz (siempre factible).
    """
    started = time.perf_counter()
    limit = time_limit_s if time_limit_s is not None else instance.settings.time_limit_s
    report = progress or (lambda _pct, _msg: None)

    report(2, "Construyendo la solución voraz")
    greedy = greedy_plan(instance, stage1)
    greedy_value = weighted_coverage(instance, greedy.sessions)
    if cancel is not None and cancel.is_set():
        raise OperationCancelledError("La optimización fue cancelada. No se guardó ninguna corrida.")

    report(5, "Construyendo el modelo CP-SAT")
    model = _Model(instance, stage1)
    base = _Baseline(started, greedy, greedy_value, time.perf_counter() - started, len(model.model.proto.variables))
    if not model.candidates:
        return base.result(greedy, "óptimo", PhaseResult("Fase A", "óptimo", 0, 0, 0.0), coverage_value=0)

    # En modo determinista el presupuesto no puede depender del reloj.
    budget = limit if deterministic else max(limit - base.build_seconds, 1.0)
    share = instance.settings.phase_a_share
    run = partial(_run_phase, model, deterministic=deterministic, progress=progress, cancel=cancel)
    first = _coverage_phase(model, base, budget * share, run, report)
    if first.plan is None:
        log.warning("La fase A no encontró solución; se usa el plan voraz.")
        return base.result(greedy, "heurística", first.phase, coverage_value=greedy_value)
    remaining = budget * (1 - share) if deterministic else max(budget - first.phase.seconds, 1.0)
    second = _quality_phase(model, first.plan, remaining, run, report)
    plan = second.plan or first.plan
    report(100, "Optimización terminada")
    return base.result(
        plan,
        "óptimo" if first.optimal and second.optimal else "factible",
        first.phase,
        second.phase,
        coverage_value=weighted_coverage(instance, plan.sessions),
        quality_value=second.quality,
        mix_slack=second.mix_slack,
    )
