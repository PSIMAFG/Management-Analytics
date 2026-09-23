"""Fixtures compartidas: catálogo de ejemplo, base sembrada y servicios."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from kpi_monitor.data import db
from kpi_monitor.data.catalog_seed import build_catalog
from kpi_monitor.data.seed import seed_demo_data
from kpi_monitor.data.synthetic import generate_observations
from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.engine import EngineConfig, PeriodEvaluation, evaluate_period
from kpi_monitor.domain.models import MonitorSettings, Observation, Period
from kpi_monitor.services import Services, build_services

# La interfaz se prueba sin abrir ventanas visibles.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DEFAULT_PERIOD = Period(2026, 8)


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return build_catalog()


@pytest.fixture(scope="session")
def observations(catalog: Catalog) -> list[Observation]:
    return generate_observations(catalog)


@pytest.fixture(scope="session")
def engine_config() -> EngineConfig:
    return EngineConfig.from_settings(MonitorSettings())


@pytest.fixture(scope="session")
def evaluation(catalog: Catalog, observations: list[Observation], engine_config: EngineConfig) -> PeriodEvaluation:
    return evaluate_period(catalog, observations, DEFAULT_PERIOD, engine_config)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("plantilla") / "monitor.db"
    conn = db.connect(path)
    try:
        db.initialize(conn)
        seed_demo_data(conn)
    finally:
        conn.close()
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    """Copia fresca de la base sembrada para cada test que escribe."""
    target = tmp_path / "monitor.db"
    shutil.copy(template_db, target)
    return target


@pytest.fixture
def services(db_path: Path) -> Iterator[Services]:
    yield build_services(db_path)
