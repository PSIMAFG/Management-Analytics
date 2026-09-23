"""Arranque: qué carpeta de datos usa cada modo de la línea de comandos."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from receipt_reader import app
from receipt_reader.paths import DATA_DIR_ENV, AppPaths


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[AppPaths, Path | None, bool]]:
    """Reemplaza el arranque real por uno que anota la carpeta de datos y la del log."""
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "trabajo"))
    calls: list[tuple[AppPaths, Path | None, bool]] = []

    def fake_run(_args: argparse.Namespace, paths: AppPaths, *, log_dir: Path | None = None) -> int:
        calls.append((paths, log_dir, paths.data_dir.exists()))
        return 0

    monkeypatch.setattr(app, "run", fake_run)
    return calls


def test_normal_start_uses_the_working_folder(
    launches: list[tuple[AppPaths, Path | None, bool]], tmp_path: Path
) -> None:
    assert app.main([]) == 0
    paths, log_dir, _ = launches[0]
    assert paths.data_dir == (tmp_path / "trabajo").resolve()
    assert log_dir is None


def test_captures_never_touch_the_working_database(
    launches: list[tuple[AppPaths, Path | None, bool]], tmp_path: Path
) -> None:
    # Privacidad: la base de trabajo puede tener boletas reales; las capturas usan datos de ejemplo nuevos.
    assert app.main(["--capturas", str(tmp_path / "img")]) == 0
    paths, log_dir, existed = launches[0]
    working = (tmp_path / "trabajo").resolve()
    assert existed
    assert paths.data_dir != working
    assert working not in paths.data_dir.parents
    assert log_dir == working / "logs"
    assert not paths.data_dir.exists()


def test_captures_with_explicit_folder_use_that_folder(
    launches: list[tuple[AppPaths, Path | None, bool]], tmp_path: Path
) -> None:
    assert app.main(["--capturas", str(tmp_path / "img"), "--datos", str(tmp_path / "otra")]) == 0
    paths, log_dir, _ = launches[0]
    assert paths.data_dir == (tmp_path / "otra").resolve()
    assert log_dir is None
