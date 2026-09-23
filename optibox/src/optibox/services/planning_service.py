"""Caso de uso principal: planificar una semana, guardar la corrida y consultarla."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from optibox.data import db
from optibox.data.repository import MasterDataRepository, RunRecord, RunRepository
from optibox.data.seed import DEMO_WEEK_KEY
from optibox.domain.agenda import AgendaEntry, room_agenda, staff_agenda
from optibox.domain.diagnosis import diagnose_unmet
from optibox.domain.instance import PlanningInstance, build_instance
from optibox.domain.metrics import compute_metrics
from optibox.domain.models import PlanningSettings, Scenario
from optibox.domain.optimizer import optimize
from optibox.domain.rules import RULE_DESCRIPTIONS, generate_candidates
from optibox.domain.run import RuleExclusion, RunDetail, RunSummary
from optibox.domain.timegrid import week_monday
from optibox.domain.validator import validate_plan
from optibox.errors import OperationCancelledError, PlanValidationError, ValidationError

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, str], None]


class PlanningService:
    """Arma la instancia de una semana, ejecuta las dos etapas, valida, guarda y calcula métricas."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def default_week(self) -> date:
        """Semana de la demo guardada al crear la base, o el próximo lunes si no existe."""
        with db.session(self.db_path) as conn:
            stored = MasterDataRepository(conn).setting(DEMO_WEEK_KEY)
        if stored:
            return date.fromisoformat(stored)
        return week_monday(date.today()) + timedelta(days=7)

    def scenarios(self) -> tuple[Scenario, ...]:
        with db.session(self.db_path) as conn:
            return MasterDataRepository(conn).scenarios()

    def settings(self) -> PlanningSettings:
        with db.session(self.db_path) as conn:
            return MasterDataRepository(conn).settings()

    def scenario(self, scenario_id: int) -> Scenario:
        for scenario in self.scenarios():
            if scenario.id == scenario_id:
                return scenario
        raise ValidationError("El escenario seleccionado ya no existe.")

    def build_instance(self, week_start: date, scenario_id: int) -> PlanningInstance:
        """Instancia de la semana que contiene `week_start` con los datos maestros actuales."""
        with db.session(self.db_path) as conn:
            master = MasterDataRepository(conn).load()
        scenario = next((s for s in master.scenarios if s.id == scenario_id), None)
        if scenario is None:
            raise ValidationError("El escenario seleccionado ya no existe.")
        return build_instance(master, week_start, scenario, master.settings)

    def optimize(
        self,
        week_start: date,
        scenario_id: int,
        time_limit_s: float | None = None,
        *,
        deterministic: bool = False,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> RunDetail:
        """Planifica la semana, valida el plan, guarda la corrida y devuelve su detalle.

        `progress` recibe (porcentaje, mensaje). Si `cancel` se activa, la
        búsqueda se detiene, no se guarda nada y se lanza OperationCancelledError.
        """
        report = progress or (lambda _pct, _msg: None)
        if time_limit_s is not None and time_limit_s <= 0:
            raise ValidationError("El límite de tiempo debe ser positivo.")
        report(0, "Leyendo los datos de la semana")
        instance = self.build_instance(week_start, scenario_id)
        limit = time_limit_s if time_limit_s is not None else instance.settings.time_limit_s
        if not instance.demand:
            raise ValidationError("La semana elegida no tiene demanda registrada: no hay turnos que asignar.")
        report(1, "Etapa 1: aplicando la cadena de reglas")
        stage1 = generate_candidates(instance)
        log.info("Etapa 1: %d candidatos de %d evaluados", len(stage1.candidates), stage1.evaluated)
        if cancel is not None and cancel.is_set():
            raise OperationCancelledError("La optimización fue cancelada. No se guardó ninguna corrida.")
        result = optimize(
            instance, stage1, time_limit_s=limit, deterministic=deterministic, progress=report, cancel=cancel
        )
        report(98, "Validando el plan")
        violations = validate_plan(instance, result.plan)
        if violations:
            for violation in violations:
                log.error("Validación: %s (%s)", violation.message, violation.code)
            raise PlanValidationError(
                "El plan obtenido no pasó la validación independiente y no se guardó. "
                "El detalle quedó registrado en el log.",
                tuple(v.message for v in violations),
            )
        metrics = compute_metrics(instance, result.plan)
        greedy_metrics = compute_metrics(instance, result.greedy)
        unmet = diagnose_unmet(instance, result.plan, stage1)
        exclusions = tuple(
            RuleExclusion(position, code, RULE_DESCRIPTIONS.get(code, code), count)
            for position, (code, count) in enumerate(stage1.exclusions.items(), start=1)
        )
        record = RunRecord(
            created_at=datetime.now(),
            instance=instance,
            scenario_id=instance.scenario.id,
            status=result.status,
            candidates=len(stage1.candidates),
            variables=result.variables,
            phase_a=(result.phase_a.status, result.phase_a.objective, result.phase_a.bound, result.phase_a.seconds),
            phase_b=(
                (result.phase_b.status, result.phase_b.objective, result.phase_b.seconds)
                if result.phase_b
                else (None, None, None)
            ),
            greedy_value=result.greedy_value,
            greedy_covered_sessions=greedy_metrics.totals.covered_sessions,
            covered_sessions=metrics.totals.covered_sessions,
            build_seconds=result.build_seconds,
            total_seconds=result.total_seconds,
            time_limit_s=limit,
            room_utilization=metrics.totals.room_utilization_pct,
            plan=result.plan,
            unmet=unmet,
            exclusions=exclusions,
        )
        report(99, "Guardando la corrida")
        with db.session(self.db_path) as conn, db.transaction(conn):
            run_id = RunRepository(conn).save(record)
        with db.session(self.db_path) as conn:
            summary = RunRepository(conn).summary(run_id)
        log.info(
            "Corrida %d guardada: %d de %d sesiones cubiertas (%s)",
            run_id,
            metrics.totals.covered_sessions,
            metrics.totals.demand_sessions,
            result.status,
        )
        report(100, "Corrida guardada")
        return RunDetail(summary, instance, result.plan, metrics, unmet, exclusions)

    def list_runs(self, week_start: date | None = None) -> tuple[RunSummary, ...]:
        """Corridas guardadas, de la más reciente a la más antigua (opcionalmente de una semana)."""
        with db.session(self.db_path) as conn:
            return RunRepository(conn).list(week_monday(week_start) if week_start else None)

    def compare_runs(self, run_ids: Sequence[int]) -> tuple[RunSummary, ...]:
        """Resúmenes de las corridas pedidas, en el orden indicado."""
        with db.session(self.db_path) as conn:
            repo = RunRepository(conn)
            return tuple(repo.summary(run_id) for run_id in run_ids)

    def load_run(self, run_id: int) -> RunDetail:
        """Detalle completo de una corrida guardada, con sus métricas recalculadas desde su plan."""
        with db.session(self.db_path) as conn:
            summary, instance, plan, unmet, raw_exclusions = RunRepository(conn).load(run_id)
        metrics = compute_metrics(instance, plan)
        exclusions = tuple(
            RuleExclusion(position, code, RULE_DESCRIPTIONS.get(code, code), count)
            for position, code, count in raw_exclusions
        )
        return RunDetail(summary, instance, plan, metrics, unmet, exclusions)

    def initial_run(self) -> RunSummary | None:
        """Corrida para mostrar al abrir: la última de la semana por defecto o, si no hay, la última guardada."""
        runs = self.list_runs(self.default_week())
        if runs:
            return runs[0]
        all_runs = self.list_runs()
        return all_runs[0] if all_runs else None

    def delete_run(self, run_id: int) -> None:
        with db.session(self.db_path) as conn, db.transaction(conn):
            RunRepository(conn).delete(run_id)

    @staticmethod
    def staff_agenda(detail: RunDetail, staff_code: str) -> tuple[AgendaEntry, ...]:
        """Agenda semanal de una persona en la corrida (sesiones, administrativo, bloqueos, ausencias, feriados)."""
        return staff_agenda(detail.instance, detail.plan, staff_code)

    @staticmethod
    def room_agenda(detail: RunDetail, room_code: str) -> tuple[AgendaEntry, ...]:
        """Agenda semanal de una sala en la corrida (en la sala administrativa, ocupación por tramo)."""
        return room_agenda(detail.instance, detail.plan, room_code)
