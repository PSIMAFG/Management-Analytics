"""Fixtures compartidas: base de ejemplo (una vez por sesión) y bases de catálogo por test."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from receipt_reader.data import db
from receipt_reader.data.seed import seed_catalog
from receipt_reader.services.demo import create_demo_database
from receipt_reader.services.reports import ReportService
from receipt_reader.services.review import ReviewService
from support import DemoEnvironment, fixed_clock


@pytest.fixture(scope="session")
def demo_env(tmp_path_factory: pytest.TempPathFactory) -> DemoEnvironment:
    """Base de ejemplo completa, creada una sola vez. Los tests que escriben usan `demo_db`."""
    root = tmp_path_factory.mktemp("demo")
    db_path = root / "demo.db"
    result = create_demo_database(db_path, root / "lote_demo", root / "muestras")
    return DemoEnvironment(root, db_path, root / "lote_demo", root / "muestras", result)


@pytest.fixture
def demo_db(demo_env: DemoEnvironment, tmp_path: Path) -> Path:
    """Copia privada de la base de ejemplo (los archivos del lote se comparten, solo se leen)."""
    target = tmp_path / "demo.db"
    shutil.copyfile(demo_env.db_path, target)
    return target


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    """Base vacía con el esquema y los catálogos de ejemplo (programas, tasas, parámetros)."""
    path = tmp_path / "catalogo.db"
    with db.session(path) as conn:
        db.initialize(conn)
        seed_catalog(conn)
    return path


@pytest.fixture
def review(demo_db: Path) -> ReviewService:
    return ReviewService(demo_db, clock=fixed_clock)


@pytest.fixture
def reports(demo_db: Path) -> ReportService:
    return ReportService(demo_db, clock=fixed_clock)
