"""Persistencia: esquema, restricciones, repositorios y generador sintético reproducible."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from staffing_simulator.app import prepare_database
from staffing_simulator.data import db
from staffing_simulator.data.repositories import (
    CatalogRepository,
    FinancialRepository,
    ParameterRepository,
    ScenarioRepository,
)
from staffing_simulator.data.seed import DEMO_YEAR, EXECUTED_MONTHS, RUT_RANGE, seed_demo_data
from staffing_simulator.domain.models import (
    BudgetItemDraft,
    BudgetItemType,
    CostMethod,
    PartialMonthMethod,
    Position,
    ScenarioDraft,
)
from staffing_simulator.domain.projection import Dimension, project_scenario
from staffing_simulator.domain.rut import normalize_rut
from staffing_simulator.domain.validation import ValidPosition
from staffing_simulator.errors import DataError
from staffing_simulator.paths import AppPaths

TABLES = (
    "site",
    "program",
    "program_budget",
    "budget_item",
    "job_role",
    "rate",
    "salary_scale",
    "salary_adjustment",
    "contract_type",
    "employer_contribution",
    "retention_rate",
    "setting",
    "person",
    "scenario",
    "position",
    "other_expense",
    "execution",
)


def _dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = db.connect(path)
    try:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")] for table in TABLES}
    finally:
        conn.close()


def _seed(path: Path) -> None:
    conn = db.connect(path)
    try:
        assert db.initialize(conn)
        seed_demo_data(conn)
    finally:
        conn.close()


def test_generator_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    _seed(first)
    _seed(second)
    assert _dump(first) == _dump(second)


def test_schema_version_and_reinitialize(empty_db: Path) -> None:
    conn = db.connect(empty_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.initialize(conn)
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(DataError, match="versión de esquema"):
            db.initialize(conn)
    finally:
        conn.close()


def test_constraints_reject_inconsistent_rows(seeded_db: Path) -> None:
    conn = db.connect(seeded_db)
    try:
        item_id = conn.execute("SELECT id FROM budget_item WHERE program_id = 1 LIMIT 1").fetchone()[0]
        base = (
            "INSERT INTO position (scenario_id, job_role_id, contract_type_id, site_id, program_id, "
            f"budget_item_id, weekly_minutes, monthly_minutes, start_date, end_date) "
            f"VALUES (1, 1, 1, 1, 1, {item_id}, {{weekly}}, {{monthly}}, '{{start}}', {{end}})"
        )
        bad_rows = [
            base.format(weekly=2640, monthly="NULL", start="2026-03-18", end="'2025-07-31'"),
            base.format(weekly="NULL", monthly="NULL", start="2026-01-01", end="NULL"),
            base.format(weekly=2640, monthly=2280, start="2026-01-01", end="NULL"),
            base.format(weekly=2640, monthly="NULL", start="18-03-2026", end="NULL"),
            base.replace("VALUES (1,", "VALUES (999,").format(
                weekly=2640, monthly="NULL", start="2026-01-01", end="NULL"
            ),
        ]
        for statement in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rate (category, job_role_id, year, hourly_rate) VALUES ('B', 1, 2026, 1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contract_type (code, name, cost_method, applies_retention) "
                "VALUES ('X', 'X', 'salaried', 1)"
            )
    finally:
        conn.close()


def test_repository_round_trip(empty_db: Path) -> None:
    conn = db.connect(empty_db)
    try:
        catalog = CatalogRepository(conn)
        site = catalog.add_site("SN", "Sede Norte")
        program = catalog.add_program("P1", "Programa Base")
        role = catalog.add_job_role("PSI", "Psicólogo", "B")
        contract = catalog.add_contract_type("HON", "Honorarios", CostMethod.WEEKLY_FEE, True)
        person = catalog.add_person("Persona Sintética", "41234567-3")
        item = FinancialRepository(conn).add_item(
            BudgetItemDraft(program.id, 2026, "P1-RRHH", "Recurso humano", BudgetItemType.HUMAN_RESOURCES, 0)
        )
        scenarios = ScenarioRepository(conn)
        stamp = datetime(2026, 2, 3, 4, 5, 6)
        scenario_id = scenarios.insert(
            ScenarioDraft("Plan", 2026, "Descripción", PartialMonthMethod.FULL_MONTH, Decimal("0.0325")), stamp
        )
        position_id = scenarios.insert_position(
            scenario_id,
            ValidPosition(
                role.id,
                contract.id,
                site.id,
                program.id,
                item.id,
                person.id,
                450,
                None,
                1,
                date(2026, 3, 9),
                None,
                None,
                "n",
            ),
        )
        conn.commit()
        scenario = scenarios.get(scenario_id)
        assert scenario.expected_absence == Decimal("0.0325")
        assert scenario.partial_month_method is PartialMonthMethod.FULL_MONTH
        assert scenario.created_at == stamp
        position = scenarios.position(position_id)
        assert position.job_role == role
        assert position.contract_type == contract
        assert position.person == person
        assert position.weekly_hours == Decimal("7.5")
        assert position.start_date == date(2026, 3, 9)
        assert position.end_date is None
        assert catalog.find_person_by_rut("41234567-3") == person
    finally:
        conn.close()


def test_seed_structure_and_calibration(seeded_db: Path) -> None:
    conn = db.connect(seeded_db)
    try:
        catalog = CatalogRepository(conn).catalog()
        assert [site.name for site in catalog.sites] == [
            "Sede Centro",
            "Sede Norte",
            "Sede Oriente",
            "Sede Poniente",
            "Sede Sur",
        ]
        assert len(catalog.programs) == 4
        assert {item.cost_method for item in catalog.contract_types} == set(CostMethod)
        scenarios = ScenarioRepository(conn)
        names = [scenario.name for scenario in scenarios.list_all()]
        assert names == ["Dotación vigente", "Expansión", "Reconversión a plazo fijo"]
        current = scenarios.positions(1)
        assert 55 <= len(current) <= 70
        weekly = Counter(position.weekly_hours for position in current if position.weekly_hours is not None)
        expected_hours = {44, 41, 37, 35, 33, 32, 30, 28, 22, 18, 15, 11, Decimal("7.5"), 6}
        assert expected_hours <= {Decimal(hours) for hours in weekly}
        assert weekly[Decimal(44)] == max(weekly.values())

        by_person: dict[int, list[Position]] = {}
        for position in current:
            if position.person is not None:
                by_person.setdefault(position.person.id, []).append(position)
        multi = [items for items in by_person.values() if len(items) > 1]
        concurrent = [items for items in multi if len({item.program.id for item in items}) > 1]
        assert concurrent, "falta una persona con posiciones simultáneas en programas distintos"
        segments = [items for items in multi if any(item.end_date for item in items) and len(items) == 2]
        assert segments, "falta un cambio de jornada modelado como dos tramos"
        assert any(item.contract_type.cost_method is CostMethod.HOURLY_FEE for item in current)
        assert any(item.person is None and item.quantity == 2 for item in current)
        assert any(item.end_date is not None and (item.end_date - item.start_date).days < 31 for item in current), (
            "falta un contrato de pocas semanas"
        )
        assert any(item.job_role.code == "MED" for item in current)

        persons = catalog.persons
        assert len({person.full_name for person in persons}) == len(persons)
        for person in persons:
            assert person.rut is not None
            assert normalize_rut(person.rut) == person.rut
            assert RUT_RANGE[0] <= int(person.rut.split("-")[0]) <= RUT_RANGE[1]

        params = ParameterRepository(conn)
        rates = {(item.category, item.year): item.hourly_rate for item in params.category_rates()}
        assert rates[("A", DEMO_YEAR)] / rates[("B", DEMO_YEAR)] == pytest.approx(2.0, rel=0.05)
        role_rate = next(item.hourly_rate for item in params.role_rates() if item.year == DEMO_YEAR)
        assert role_rate / rates[("B", DEMO_YEAR)] == pytest.approx(2.3, rel=0.02)
        assert params.settings().weeks_per_month == 4

        projection = project_scenario(scenarios.get(1), current, params.cost_parameters())
        financial = FinancialRepository(conn)
        budgets = {item.program_id: item.amount for item in financial.program_budgets(DEMO_YEAR)}
        assert len(budgets) == 4
        assert 0.9 < sum(budgets.values()) / projection.total_cost < 1.3
        items = financial.items(year=DEMO_YEAR)
        assert len(items) == 4 * 5  # recurso humano + 4 ítems de operación por programa
        by_program = {row.key: row.total for row in projection.breakdown(Dimension.PROGRAM)}
        hr_items = {item.program.id: item.amount for item in items if item.item_type is BudgetItemType.HUMAN_RESOURCES}
        hr_balances = [hr_items[key] - value for key, value in by_program.items()]
        assert any(balance > 0 for balance in hr_balances)
        assert any(balance < 0 for balance in hr_balances)

        months = {row[0] for row in conn.execute("SELECT DISTINCT month FROM execution")}
        assert months == set(EXECUTED_MONTHS)
        assert conn.execute("SELECT COUNT(*) FROM execution").fetchone()[0] >= 4 * len(EXECUTED_MONTHS)
    finally:
        conn.close()


def test_seed_scenarios_differ_visibly(seeded_db: Path) -> None:
    conn = db.connect(seeded_db)
    try:
        scenarios = ScenarioRepository(conn)
        params = ParameterRepository(conn).cost_parameters()
        totals = {
            scenario.name: project_scenario(scenario, scenarios.positions(scenario.id), params).total_cost
            for scenario in scenarios.list_all()
        }
    finally:
        conn.close()
    base = totals["Dotación vigente"]
    assert totals["Expansión"] / base > 1.05
    assert abs(totals["Reconversión a plazo fijo"] / base - 1) > 0.01


def test_old_schema_version_asks_to_reset_instead_of_guessing(tmp_path: Path) -> None:
    """La versión 3 rediseñó la estructura financiera: una base anterior debe regenerarse, nunca migrarse a ciegas."""
    path = tmp_path / "version1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE site (id INTEGER PRIMARY KEY, code TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    try:
        with pytest.raises(DataError, match="--reiniciar-datos"):
            db.initialize(conn)
    finally:
        conn.close()


def test_prepare_database_reports_a_damaged_file_with_a_clear_message(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path).ensure()
    paths.db_path.write_bytes(b"no es una base de datos " * 64)
    with pytest.raises(DataError, match="--reiniciar-datos") as info:
        prepare_database(paths, reset=False)
    assert str(paths.db_path) in info.value.user_message
    prepare_database(paths, reset=True)
    conn = db.connect(paths.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM scenario").fetchone()[0] == 3
    finally:
        conn.close()


def test_prepare_database_reports_a_file_in_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AppPaths(tmp_path).ensure()
    paths.db_path.write_bytes(b"")

    def locked(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "El archivo está en uso", str(paths.db_path))

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(DataError, match="está en uso"):
        prepare_database(paths, reset=True)
