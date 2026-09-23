"""Reglas de dependencia entre capas, verificadas sobre los imports del código."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from factories import REFERENCE
from optibox.data.seed import generate_master_data

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "optibox"

FORBIDDEN = {
    "domain": ("optibox.data", "optibox.services", "optibox.ui", "PySide6", "sqlite3", "openpyxl"),
    "data": ("optibox.services", "optibox.ui", "PySide6"),
    "services": ("optibox.ui", "PySide6"),
    "ui": ("optibox.data",),
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
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((PACKAGE / layer).glob("*.py"))
        for name in _imports(path)
        if any(name == banned or name.startswith(banned + ".") for banned in FORBIDDEN[layer])
    ]
    assert offenders == []


def test_master_data_codes_are_not_hardcoded() -> None:
    """Regresión H5: ningún código de persona, sala, cargo o tipo de atención queda cableado en el programa.

    Las reglas se resuelven por atributos (cargo, tipo y flags de la sala, destinatario del bloqueo), así
    que cambiar la dotación o las salas no exige tocar el código. Solo el generador sintético conoce los
    códigos de ejemplo.
    """
    master = generate_master_data(REFERENCE)
    codes = (
        {role.code for role in master.roles}
        | {kind.code for kind in master.service_types}
        | {room.code for room in master.rooms}
        | {member.code for member in master.staff}
    )
    offenders = [
        f"{path.relative_to(PACKAGE)}:{node.lineno}: {node.value}"
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.name != "seed.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in codes
    ]
    assert offenders == []
