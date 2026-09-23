"""Persistencia SQLite: esquema, restricciones, repositorios y corridas."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from factories import MONDAY, REFERENCE, PlannedDatabase
from optibox.data import db
from optibox.data.repository import MasterDataRepository, RunRepository
from optibox.data.seed import generate_master_data
from optibox.domain.models import Absence, AbsenceStatus, Contract, MasterData, Staff
from optibox.errors import DataError


def _normalized_staff(staff: Staff) -> tuple[object, ...]:
    return (
        staff.code,
        staff.name,
        staff.role_code,
        staff.active,
        tuple(sorted(staff.contracts, key=lambda c: c.valid_from)),
        frozenset(staff.availability),
        staff.skills,
        frozenset(staff.room_rules),
        frozenset(staff.targets),
    )


def _normalized(master: MasterData) -> dict[str, object]:
    return {
        "roles": sorted(master.roles, key=lambda r: r.code),
        "services": sorted(master.service_types, key=lambda s: s.code),
        "rooms": sorted(master.rooms, key=lambda r: r.code),
        "staff": sorted((_normalized_staff(s) for s in master.staff), key=lambda s: str(s[0])),
        "blockings": sorted(master.blockings, key=repr),
        "absences": sorted(master.absences, key=repr),
        "holidays": sorted(master.holidays, key=repr),
        "hours": sorted(master.center_hours, key=lambda h: h.weekday),
        "demand": sorted(master.demand, key=lambda d: d.key),
        "scenarios": sorted(
            (
                (s.name, s.description, s.service_weights, s.mix_min, s.mix_max, s.coverage_floor_pct)
                for s in master.scenarios
            ),
            key=repr,
        ),
        "settings": master.settings,
    }


def test_schema_is_created_once_with_its_version(tmp_path: Path) -> None:
    path = tmp_path / "base.db"
    with db.session(path) as conn:
        assert db.initialize(conn) is True
        assert db.initialize(conn) is False
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "base.db"
    with db.session(path) as conn:
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(DataError):
            db.initialize(conn)


def test_master_data_roundtrip(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn:
        loaded = MasterDataRepository(conn).load()
    assert _normalized(loaded) == _normalized(generate_master_data(REFERENCE))
    assert all(scenario.id is not None for scenario in loaded.scenarios)


def test_open_ended_contract_stays_empty(seeded_db: Path) -> None:
    """Regresión M1: un contrato sin fecha de término vuelve como None, nunca como texto."""
    with db.session(seeded_db) as conn:
        raw = conn.execute("SELECT valid_to FROM contract WHERE valid_to IS NULL").fetchall()
        staff = MasterDataRepository(conn).load().staff
    assert raw
    open_ended = [c for person in staff for c in person.contracts if c.valid_to is None]
    assert len(open_ended) == len(staff)


def test_overlapping_contract_is_rejected_by_the_database(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn:
        staff_id = conn.execute("SELECT id FROM staff WHERE code = 'TO-01'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contract (staff_id, valid_from, valid_to, weekly_minutes) VALUES (?, ?, NULL, 600)",
                (staff_id, "2026-11-01"),
            )


def test_add_contract_keeps_history(seeded_db: Path) -> None:
    """Regresión B22: una nueva jornada cierra la versión abierta y conserva el historial."""
    with db.session(seeded_db) as conn, db.transaction(conn):
        repo = MasterDataRepository(conn)
        repo.add_contract("TO-01", Contract(date(2027, 1, 1), None, 22 * 60))
        person = next(s for s in repo.load().staff if s.code == "TO-01")
    first, second = sorted(person.contracts, key=lambda c: c.valid_from)
    assert first.valid_to == date(2026, 12, 31)
    assert first.weekly_minutes == 44 * 60
    assert (second.valid_from, second.valid_to, second.weekly_minutes) == (date(2027, 1, 1), None, 22 * 60)


def test_add_contract_overlapping_a_closed_version_fails(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn:
        repo = MasterDataRepository(conn)
        person = next(s for s in repo.load().staff if s.code == "PS-02")
        start = min(c.valid_from for c in person.contracts)
        with pytest.raises(DataError):
            repo.add_contract("PS-02", Contract(start.replace(day=start.day + 1), None, 600))


def test_check_and_foreign_key_constraints(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO staff (code, name, role_id) VALUES ('X-01', 'Persona X', 999)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO room (code, name, kind, allows_group, allows_evaluation, capacity) "
                "VALUES ('RX', 'Sala X', 'cocina', 0, 0, 1)"
            )
        service_id = conn.execute("SELECT id FROM service_type LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO demand (weekday, block_start_min, service_type_id, sessions, priority) "
                "VALUES (0, 570, ?, 1, 2)",
                (service_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO holiday (day, name) VALUES ('31-12-2026', 'Mal escrito')")


def test_absence_lifecycle(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn, db.transaction(conn):
        repo = MasterDataRepository(conn)
        absence_id = repo.add_absence(Absence("KI-01", MONDAY, 840, 960, "Trámite", AbsenceStatus.PENDING))
        repo.set_absence_status(absence_id, AbsenceStatus.APPROVED)
        stored = {record.id: record.absence for record in repo.absences()}
        assert stored[absence_id].status is AbsenceStatus.APPROVED
        repo.delete_absence(absence_id)
        assert absence_id not in {record.id for record in repo.absences()}
        with pytest.raises(DataError):
            repo.set_absence_status(absence_id, AbsenceStatus.REJECTED)


def test_replace_all_keeps_scenario_ids(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn, db.transaction(conn):
        repo = MasterDataRepository(conn)
        before = {s.name: s.id for s in repo.scenarios()}
        repo.replace_all(repo.load())
        after = {s.name: s.id for s in repo.scenarios()}
    assert before == after


def test_transaction_rolls_back_on_error(seeded_db: Path) -> None:
    with db.session(seeded_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM holiday").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
            conn.execute("INSERT INTO holiday (day, name) VALUES ('2027-01-01', 'Año nuevo')")
            conn.execute("INSERT INTO holiday (day, name) VALUES ('2027-01-01', 'Repetido')")
        assert conn.execute("SELECT COUNT(*) FROM holiday").fetchone()[0] == before


def test_run_roundtrip(planned_db: PlannedDatabase) -> None:
    with db.session(planned_db.path) as conn:
        repo = RunRepository(conn)
        summary, instance, plan, unmet, exclusions = repo.load(planned_db.run_id)
        listed = repo.list()
    assert [run.id for run in listed] == [planned_db.run_id]
    assert summary.week_start == MONDAY
    assert instance.week_start == MONDAY
    assert len(plan.sessions) == summary.covered_sessions
    assert plan.session_minutes() == summary.session_minutes
    assert exclusions
    assert all(u.shortfall > 0 for u in unmet)


def test_missing_run_raises(planned_db: PlannedDatabase) -> None:
    with db.session(planned_db.path) as conn, pytest.raises(DataError):
        RunRepository(conn).summary(9999)
