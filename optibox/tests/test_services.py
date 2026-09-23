"""Servicios: planificación, datos maestros y preparación de la base."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from factories import MONDAY, REFERENCE, PlannedDatabase
from optibox.data import db
from optibox.domain.agenda import AgendaKind
from optibox.domain.models import AbsenceStatus, DemandItem
from optibox.domain.validator import validate_plan
from optibox.errors import DataError, OperationCancelledError, ValidationError
from optibox.services.bootstrap import prepare_database
from optibox.services.master_data_service import MasterDataService
from optibox.services.planning_service import PlanningService


def test_planned_run_is_valid_and_explains_the_shortfall(planned_db: PlannedDatabase) -> None:
    service = PlanningService(planned_db.path)
    detail = service.load_run(planned_db.run_id)
    totals = detail.metrics.totals
    assert validate_plan(detail.instance, detail.plan) == ()
    assert totals.demand_sessions == detail.instance.total_demand
    assert 0.7 <= (totals.coverage_pct or 0) < 1.0
    assert totals.unmet_sessions == sum(u.shortfall for u in detail.unmet)
    assert all(u.cause_label for u in detail.unmet)
    assert totals.weighted_covered >= detail.summary.greedy_value
    assert [e.position for e in detail.exclusions] == list(range(1, len(detail.exclusions) + 1))
    assert detail.metrics.staff_load and detail.metrics.daily


def test_default_week_and_initial_run(planned_db: PlannedDatabase) -> None:
    service = PlanningService(planned_db.path)
    assert service.default_week() == MONDAY
    initial = service.initial_run()
    assert initial is not None and initial.id == planned_db.run_id
    assert [run.id for run in service.list_runs(MONDAY + timedelta(days=2))] == [planned_db.run_id]
    assert service.list_runs(MONDAY + timedelta(days=7)) == ()
    assert [run.id for run in service.compare_runs([planned_db.run_id])] == [planned_db.run_id]
    assert len(service.scenarios()) == 3


def test_agendas_through_the_service(planned_db: PlannedDatabase) -> None:
    service = PlanningService(planned_db.path)
    detail = service.load_run(planned_db.run_id)
    busiest = max(detail.metrics.staff_load, key=lambda load: load.session_min)
    entries = service.staff_agenda(detail, busiest.code)
    sessions = [e for e in entries if e.kind is AgendaKind.SESSION]
    assert len(sessions) == busiest.sessions
    admin_room = service.room_agenda(detail, "ADM")
    assert admin_room and all(e.kind is AgendaKind.ADMIN for e in admin_room)


def test_optimize_saves_a_new_run_with_progress(seeded_db: Path) -> None:
    service = PlanningService(seeded_db)
    scenario = service.scenarios()[1]
    assert scenario.id is not None
    seen: list[int] = []
    detail = service.optimize(MONDAY, scenario.id, 1.0, deterministic=True, progress=lambda pct, _msg: seen.append(pct))
    assert seen[0] == 0 and seen[-1] == 100
    assert detail.summary.scenario_name == scenario.name
    assert [run.id for run in service.list_runs()] == [detail.summary.id]
    reloaded = service.load_run(detail.summary.id)
    assert reloaded.metrics.totals == detail.metrics.totals
    assert reloaded.plan == detail.plan
    service.delete_run(detail.summary.id)
    assert service.list_runs() == ()
    assert service.initial_run() is None


def test_cancelled_optimization_saves_nothing(seeded_db: Path) -> None:
    service = PlanningService(seeded_db)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelledError):
        service.optimize(MONDAY, 1, 2.0, cancel=cancel)
    assert service.list_runs() == ()


def test_optimize_validates_its_inputs(seeded_db: Path) -> None:
    service = PlanningService(seeded_db)
    with pytest.raises(ValidationError):
        service.optimize(MONDAY, 1, 0)
    with pytest.raises(ValidationError):
        service.optimize(MONDAY, 999, 2.0)
    master = MasterDataService(seeded_db)
    zeroes = [
        DemandItem(row.weekday, row.block_start, row.service_code, 0, row.priority) for row in master.demand_grid()
    ]
    master.save_demand(zeroes)
    with pytest.raises(ValidationError, match="demanda"):
        service.optimize(MONDAY, 1, 2.0)


def test_holiday_week_drops_its_demand(seeded_db: Path) -> None:
    instance = PlanningService(seeded_db).build_instance(MONDAY + timedelta(days=7), 1)
    closed = [day for day in instance.days if not day.is_open]
    assert len(closed) == 1
    assert instance.holiday_demand_dropped > 0


def test_demand_grid_and_update(seeded_db: Path) -> None:
    master = MasterDataService(seeded_db)
    grid = master.demand_grid()
    services = master.service_types()
    assert len(grid) == (4 * 8 + 7) * len(services)
    row = next(r for r in grid if r.weekday == 0 and r.block_start == 540 and r.service_code == "AIN")
    assert row.block_label == "09:00-10:00"
    assert row.day_name == "Lunes"
    master.update_demand(0, 540, "AIN", 9, 1)
    updated = next(r for r in master.demand_grid() if (r.weekday, r.block_start, r.service_code) == (0, 540, "AIN"))
    assert (updated.sessions, updated.priority, updated.priority_label) == (9, 1, "Alta")


@pytest.mark.parametrize(
    ("weekday", "block", "code", "sessions", "priority"),
    [
        (0, 540, "AIN", -1, 2),
        (0, 780, "AIN", 1, 2),
        (0, 570, "AIN", 1, 2),
        (0, 540, "XXX", 1, 2),
        (0, 540, "AIN", 1, 5),
    ],
)
def test_update_demand_rejects_invalid_values(
    seeded_db: Path, weekday: int, block: int, code: str, sessions: int, priority: int
) -> None:
    with pytest.raises(ValidationError):
        MasterDataService(seeded_db).update_demand(weekday, block, code, sessions, priority)


def test_save_demand_is_all_or_nothing(seeded_db: Path) -> None:
    master = MasterDataService(seeded_db)
    before = master.master_data().demand
    with pytest.raises(ValidationError):
        master.save_demand([DemandItem(0, 540, "AIN", 7, 2), DemandItem(0, 780, "AIN", 1, 2)])
    assert master.master_data().demand == before
    with pytest.raises(ValidationError):
        master.save_demand([DemandItem(0, 540, "AIN", 7, 2), DemandItem(0, 540, "AIN", 8, 2)])
    assert master.save_demand([DemandItem(0, 540, "AIN", 7, 2)]) == 1


def test_add_contract_through_the_service(seeded_db: Path) -> None:
    master = MasterDataService(seeded_db)
    master.add_contract("TO-02", date(2027, 3, 1), 30)
    person = next(s for s in master.staff() if s.code == "TO-02")
    assert sorted(c.weekly_minutes for c in person.contracts) == [22 * 60, 30 * 60]
    with pytest.raises(ValidationError):
        master.add_contract("TO-02", date(2027, 6, 1), 0)
    with pytest.raises(ValidationError):
        master.add_contract("TO-02", date(2027, 6, 1), 22.1)
    with pytest.raises(DataError):
        master.add_contract("NADIE", date(2027, 6, 1), 22)


def test_absences_through_the_service(seeded_db: Path) -> None:
    master = MasterDataService(seeded_db)
    record = master.add_absence("TS-01", MONDAY, "Trámite", AbsenceStatus.APPROVED, 480, 600)
    assert record.absence.blocks
    master.set_absence_status(record.id, AbsenceStatus.REJECTED)
    stored = {r.id: r.absence for r in master.absences()}
    assert stored[record.id].status is AbsenceStatus.REJECTED
    master.delete_absence(record.id)
    with pytest.raises(ValidationError):
        master.add_absence("TS-01", MONDAY + timedelta(days=5), "Sábado")
    with pytest.raises(ValidationError):
        master.add_absence("TS-01", MONDAY, "Trámite", start=485, end=600)
    with pytest.raises(ValidationError):
        master.add_absence("NADIE", MONDAY, "Trámite")
    with pytest.raises(ValidationError):
        master.add_absence("TS-01", MONDAY, "  ")


def test_prepare_database_seeds_once_and_saves_a_demo_run(tmp_path: Path) -> None:
    path = tmp_path / "optibox.db"
    assert prepare_database(path, reference=REFERENCE, demo_time_limit_s=1.0) is True
    service = PlanningService(path)
    runs = service.list_runs()
    assert len(runs) == 1
    assert runs[0].scenario_name == "Equilibrado"
    assert prepare_database(path, reference=REFERENCE) is False
    assert len(service.list_runs()) == 1
    assert prepare_database(path, reset=True, reference=REFERENCE, demo_scenarios=()) is True
    assert service.list_runs() == ()


def test_corrupt_database_file_is_reported_with_its_path(tmp_path: Path) -> None:
    """Un archivo que no es una base SQLite se informa como error de datos, no como error inesperado."""
    path = tmp_path / "optibox.db"
    path.write_bytes(b"esto no es una base de datos SQLite" * 20)
    with pytest.raises(DataError, match="dañada o en uso") as caught:
        prepare_database(path, reference=REFERENCE)
    assert str(path) in caught.value.user_message
    with pytest.raises(DataError):
        PlanningService(path).list_runs()


def test_reset_reports_a_file_that_cannot_be_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "optibox.db"
    path.write_bytes(b"")

    def locked(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError("el archivo está en uso")

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(DataError, match="No se pudo eliminar"):
        prepare_database(path, reset=True, reference=REFERENCE)


def test_integrity_errors_become_data_errors(seeded_db: Path) -> None:
    with pytest.raises(DataError, match="contradicen otros registros"), db.session(seeded_db) as conn:
        conn.execute("INSERT INTO holiday (day, name) VALUES ('2026-10-05', 'Uno')")
        conn.execute("INSERT INTO holiday (day, name) VALUES ('2026-10-05', 'Dos')")
