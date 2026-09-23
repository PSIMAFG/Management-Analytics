"""Persistencia en SQLite: esquema, restricciones, transacciones y repositorios."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from kpi_monitor.data import db
from kpi_monitor.data.repositories import CatalogRepository, ObservationRepository, SettingsRepository
from kpi_monitor.data.seed import seed_demo_data
from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.enums import Origin
from kpi_monitor.domain.models import MonitorSettings, Observation, Period, Thresholds
from kpi_monitor.errors import DataError

LOADED_AT = "2026-09-01T08:00:00"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "prueba.db")
    db.initialize(connection)
    yield connection
    connection.close()


@pytest.fixture
def seeded(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
    yield connection
    connection.close()


def test_initialize_creates_schema_once(tmp_path: Path) -> None:
    """El esquema se crea una sola vez, con su versión en user_version, las claves foráneas activas y todas las
    tablas."""
    connection = db.connect(tmp_path / "nueva.db")
    try:
        assert db.initialize(connection) is True
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"site", "program", "indicator", "goal_rule", "goal_band", "site_goal", "observation"} <= tables
        assert {"site_population", "setting"} <= tables
        assert db.initialize(connection) is False
    finally:
        connection.close()


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    """Una base con otra versión de esquema se rechaza con un error que sugiere usar --reiniciar-datos."""
    connection = db.connect(tmp_path / "vieja.db")
    try:
        connection.execute("PRAGMA user_version = 99")
        with pytest.raises(DataError, match="reiniciar-datos"):
            db.initialize(connection)
    finally:
        connection.close()


def test_catalog_round_trip(conn: sqlite3.Connection, catalog: Catalog) -> None:
    """El catálogo completo, incluidos los tramos de las metas, se guarda y se vuelve a leer sin cambios."""
    with db.transaction(conn):
        CatalogRepository(conn).save(catalog)
    loaded = CatalogRepository(conn).load()
    assert set(loaded.programs) == set(catalog.programs)
    assert set(loaded.sites) == set(catalog.sites)
    assert set(loaded.indicators) == set(catalog.indicators)
    assert set(loaded.rules) == set(catalog.rules)
    assert set(loaded.site_goals) == set(catalog.site_goals)
    assert set(loaded.populations) == set(catalog.populations)
    bands = loaded.rule("CG06", 2026)
    assert bands is not None
    assert [b.upper_bound for b in bands.bands] == [20.0, 30.0, 40.0, 50.0, None]


def test_seeded_database_matches_generator(seeded: sqlite3.Connection, observations: list[Observation]) -> None:
    """La base sembrada contiene exactamente las observaciones del generador sintético, con tres años y datos hasta
    agosto."""
    stored = ObservationRepository(seeded).find()
    assert stored == observations
    assert ObservationRepository(seeded).count() == len(observations)
    assert ObservationRepository(seeded).years() == [2024, 2025, 2026]
    assert ObservationRepository(seeded).last_month_with_data(2026) == 8


def test_find_filters(seeded: sqlite3.Connection) -> None:
    """La búsqueda de observaciones filtra por año, mes, sede e indicador; los meses no reportados se leen como
    faltantes."""
    repo = ObservationRepository(seeded)
    rows = repo.find(years=[2026], month=8, site_code="ORI", indicator_codes=["CG01", "CG02"])
    assert {(o.indicator_code, o.site_code, o.year, o.month) for o in rows} == {
        ("CG01", "ORI", 2026, 8),
        ("CG02", "ORI", 2026, 8),
    }
    assert all(o.reported is False for o in rows)
    assert repo.find(years=[]) == []
    assert repo.find(indicator_codes=[]) == []


def test_upsert_counts_new_and_replaced_rows(seeded: sqlite3.Connection) -> None:
    """Guardar observaciones inserta las nuevas, reemplaza las existentes y registra la fecha de carga."""
    repo = ObservationRepository(seeded)
    existing = repo.find(years=[2026], month=1, site_code="NOR", indicator_codes=["CG02"])[0]
    changed = replace(existing, numerator=1.0, origin=Origin.IMPORTED)
    new = Observation("CG02", "NOR", 2026, 9, 5.0, 8.0, True, Origin.IMPORTED)
    with db.transaction(seeded):
        inserted, replaced = repo.upsert_many([changed, new], LOADED_AT)
    assert (inserted, replaced) == (1, 1)
    assert repo.find(years=[2026], month=1, site_code="NOR", indicator_codes=["CG02"])[0] == changed
    loaded = seeded.execute("SELECT loaded_at FROM observation WHERE month = 9").fetchone()[0]
    assert loaded == LOADED_AT


def test_transaction_rolls_back_on_error(seeded: sqlite3.Connection) -> None:
    """Regla: si algo falla dentro de una transacción no queda ningún cambio a medias."""
    repo = ObservationRepository(seeded)
    before = repo.count()
    new = Observation("CG02", "NOR", 2026, 10, 5.0, 8.0)
    with pytest.raises(RuntimeError), db.transaction(seeded):
        repo.upsert_many([new], LOADED_AT)
        raise RuntimeError("falla simulada")
    assert repo.count() == before


def test_foreign_keys_are_enforced(seeded: sqlite3.Connection) -> None:
    """Las claves foráneas impiden guardar observaciones de una sede inexistente y el error se informa en español."""
    unknown_site = Observation("CG02", "XXX", 2026, 9, 1.0, 2.0)
    with pytest.raises(DataError), db.transaction(seeded):
        ObservationRepository(seeded).upsert_many([unknown_site], LOADED_AT)


@pytest.mark.parametrize(
    "values",
    [
        ("CG02", "NOR", 2026, 9, -1.0, 2.0, 1),
        ("CG02", "NOR", 2026, 13, 1.0, 2.0, 1),
        ("CG02", "NOR", 2026, 9, 1.0, None, 0),
        ("CG02", "NOR", 2026, 9, 1.0, 2.0, 2),
    ],
)
def test_check_constraints_protect_observations(seeded: sqlite3.Connection, values: tuple) -> None:
    """Las restricciones CHECK impiden valores negativos, meses inválidos, valores en meses no reportados y banderas
    inválidas."""
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO observation (indicator_code, site_code, year, month, numerator, denominator, reported, "
            "origin, loaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', 'x')",
            values,
        )


def test_unique_key_per_indicator_site_and_month(seeded: sqlite3.Connection) -> None:
    """Regla: solo puede haber una observación por indicador, sede, año y mes."""
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO observation (indicator_code, site_code, year, month, numerator, denominator, reported, "
            "origin, loaded_at) VALUES ('CG02', 'NOR', 2026, 1, 1, 2, 1, 'imported', 'x')"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO goal_rule (indicator_code, year, rule_type, weight) VALUES ('CG01', 2030, 'absolute', 0.1)",
        "INSERT INTO goal_rule (indicator_code, year, rule_type, factor, weight) "
        "VALUES ('CG01', 2030, 'capped_increase', 0.2, 0.1)",
        "INSERT INTO goal_rule (indicator_code, year, rule_type, value, weight) VALUES ('CG01', 2030, 'other', 1, 0.1)",
        "INSERT INTO goal_rule (indicator_code, year, rule_type, value, weight) "
        "VALUES ('CG01', 2030, 'absolute', 1, 2)",
        "INSERT INTO site_goal (indicator_code, year, site_code, target) VALUES ('CG01', 2030, 'NOR', 0)",
        "UPDATE indicator SET direction = 'neutral' WHERE code = 'CG01'",
        "UPDATE indicator SET multiplier = 3 WHERE code = 'CG01'",
    ],
)
def test_catalog_constraints(seeded: sqlite3.Connection, statement: str) -> None:
    """Las restricciones del catálogo impiden reglas de meta incompletas, pesos fuera de rango, metas fijas en cero,
    dirección neutra y multiplicadores indebidos."""
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(statement)


def test_settings_round_trip(conn: sqlite3.Connection) -> None:
    """Los parámetros de cálculo se guardan y se vuelven a leer sin cambios."""
    settings = MonitorSettings(
        thresholds=Thresholds(green=1.0, yellow=0.8),
        prevalence=0.2,
        default_period=Period(2025, 11),
        bootstrap_draws=500,
        bootstrap_seed=11,
        min_months_projection=4,
    )
    repo = SettingsRepository(conn)
    repo.save(settings)
    assert repo.load() == settings


def test_missing_settings_use_defaults(conn: sqlite3.Connection) -> None:
    """Si faltan parámetros guardados se usan los valores por defecto."""
    assert SettingsRepository(conn).load() == MonitorSettings()


def test_invalid_settings_are_reported(conn: sqlite3.Connection) -> None:
    """Un parámetro desconocido o un valor guardado inválido se informan con un error de datos."""
    repo = SettingsRepository(conn)
    with pytest.raises(DataError):
        repo.set("parametro_inventado", "1")
    repo.set("default_month", "agosto")
    with pytest.raises(DataError):
        repo.load()


def test_seed_is_reproducible(tmp_path: Path) -> None:
    """Regla: el generador sintético usa semilla fija; dos bases sembradas por separado son idénticas."""
    dumps = []
    for name in ("a.db", "b.db"):
        connection = db.connect(tmp_path / name)
        try:
            db.initialize(connection)
            seed_demo_data(connection)
            dumps.append(list(connection.iterdump()))
        finally:
            connection.close()
    assert dumps[0] == dumps[1]
