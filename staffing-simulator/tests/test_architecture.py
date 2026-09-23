"""Reglas de dependencia entre capas, verificadas sobre los import del código fuente."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "staffing_simulator"
FORBIDDEN = {
    "domain": ("staffing_simulator.data", "staffing_simulator.services", "staffing_simulator.ui", "PySide6", "sqlite3"),
    "data": ("staffing_simulator.services", "staffing_simulator.ui", "PySide6"),
    "services": ("staffing_simulator.ui", "PySide6"),
    "ui": ("staffing_simulator.data",),
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_import_forbidden_modules(layer: str) -> None:
    offenders = []
    for path in sorted((PACKAGE / layer).rglob("*.py")):
        for name in _imports(path):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN[layer]):
                offenders.append(f"{path.relative_to(PACKAGE)} importa {name}")
    assert offenders == []
