"""Fixtures compartidas: bases sintéticas en carpetas temporales."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from factories import REFERENCE, PlannedDatabase
from optibox.data import db
from optibox.data.seed import seed_demo_data
from optibox.services.planning_service import PlanningService

PLANNED_TIME_LIMIT_S = 5.0


def _seed(path: Path) -> Path:
    with db.session(path) as conn:
        db.initialize(conn)
        seed_demo_data(conn, REFERENCE)
    return path


@pytest.fixture
def seeded_db(tmp_path: Path) -> Iterator[Path]:
    """Base con el esquema y los datos sintéticos, sin corridas (se puede modificar)."""
    yield _seed(tmp_path / "optibox.db")


@pytest.fixture(scope="session")
def planned_db(tmp_path_factory: pytest.TempPathFactory) -> PlannedDatabase:
    """Base sintética con una corrida de la semana de la demo. Los tests no deben modificarla."""
    path = _seed(tmp_path_factory.mktemp("planificada") / "optibox.db")
    service = PlanningService(path)
    scenario = service.scenarios()[0]
    assert scenario.id is not None
    detail = service.optimize(service.default_week(), scenario.id, PLANNED_TIME_LIMIT_S, deterministic=True)
    return PlannedDatabase(path, detail.summary.id)
