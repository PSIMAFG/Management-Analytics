"""Fixtures compartidas: parámetros de prueba y bases sembradas en carpetas temporales."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from helpers import make_params
from staffing_simulator.data import db
from staffing_simulator.data.seed import seed_demo_data
from staffing_simulator.domain.parameters import CostParameters
from staffing_simulator.services import Services

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def params() -> CostParameters:
    return make_params()


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Base sembrada una sola vez por sesión; cada test trabaja sobre una copia."""
    path = tmp_path_factory.mktemp("plantilla") / "demo.db"
    conn = db.connect(path)
    try:
        db.initialize(conn)
        seed_demo_data(conn)
    finally:
        conn.close()
    return path


@pytest.fixture
def seeded_db(seeded_template: Path, tmp_path: Path) -> Path:
    target = tmp_path / "demo.db"
    shutil.copyfile(seeded_template, target)
    return target


@pytest.fixture
def services(seeded_db: Path) -> Services:
    return Services.create(seeded_db, clock=lambda: datetime(2026, 9, 1, 10, 0, 0))


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    path = tmp_path / "vacia.db"
    conn = db.connect(path)
    try:
        db.initialize(conn)
    finally:
        conn.close()
    return path
