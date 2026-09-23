"""Reglas de dependencia entre capas e higiene de las fuentes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "receipt_reader"

# Capa -> prefijos de módulos que no puede importar.
FORBIDDEN = {
    "domain": (
        "receipt_reader.data",
        "receipt_reader.services",
        "receipt_reader.ui",
        "sqlite3",
        "PySide6",
        "matplotlib",
        "openpyxl",
        "pypdfium2",
        "PIL",
        "rapidocr",
    ),
    "data": ("receipt_reader.services", "receipt_reader.ui", "PySide6", "matplotlib"),
    "services": ("receipt_reader.ui", "PySide6"),
    "ui": ("receipt_reader.data", "sqlite3", "openpyxl", "pypdfium2", "rapidocr"),
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_dependencies(layer: str) -> None:
    violations = []
    for path in sorted((PACKAGE_DIR / layer).glob("*.py")):
        for module in _imports(path):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN[layer]):
                violations.append(f"{path.name} importa {module}")
    assert violations == []


def test_sources_have_no_mojibake_or_decorative_banners() -> None:
    # B28: fuentes en UTF-8 sin secuencias rotas; comentarios sin separadores decorativos.
    offenders = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")) + sorted(PACKAGE_DIR.rglob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "Ã" in text or "Â" in text:
            offenders.append(f"{path.name}: mojibake")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("# ===", "# ---", "-- ===", "-- ---")):
                offenders.append(f"{path.name}: separador decorativo")
    assert offenders == []
