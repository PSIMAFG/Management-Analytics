"""Lanzador desde el código fuente.

Crea (si no existe) un entorno virtual propio del proyecto, instala las
dependencias que falten, las actualiza dentro de los rangos declarados en
pyproject.toml y abre la aplicación. Solo usa la biblioteca estándar para
poder ejecutarse con cualquier Python 3.11 o superior.

Uso:
    python run.py [argumentos de la aplicación]

Variables de entorno:
    MA_SKIP_UPDATE=1   no consulta el índice de paquetes (útil sin conexión).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import venv
from pathlib import Path

PACKAGE = "staffing_simulator"
REQUIRED_MODULES = ("PySide6", "matplotlib", "openpyxl")
MIN_PYTHON = (3, 11)
PIP_TIMEOUT_SECONDS = 20

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"

log = logging.getLogger("run")


def venv_python() -> Path:
    """Ruta del intérprete dentro del entorno virtual del proyecto."""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> Path:
    """Crea el entorno virtual la primera vez y devuelve su intérprete."""
    python = venv_python()
    if not python.exists():
        log.info("Creando el entorno virtual en %s", VENV_DIR)
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    return python


def runtime_requirements() -> list[str]:
    """Dependencias de ejecución declaradas en pyproject.toml."""
    # tomllib existe desde Python 3.11: se importa aquí para que con una versión anterior
    # main() alcance a mostrar el mensaje de versión mínima en vez de fallar al importar.
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return list(project.get("dependencies", []))


def sync_dependencies(python: Path, requirements: list[str]) -> bool:
    """Instala lo que falta y actualiza dentro de los rangos permitidos."""
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--upgrade-strategy",
        "eager",
        "--disable-pip-version-check",
        "--quiet",
        "--timeout",
        str(PIP_TIMEOUT_SECONDS),
        "--retries",
        "1",
        *requirements,
    ]
    log.info("Comprobando dependencias (%d paquetes)", len(requirements))
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        log.warning("No se pudieron actualizar las dependencias: %s", " | ".join(detail))
        return False
    return True


def modules_available(python: Path) -> bool:
    """Verifica que los módulos requeridos se puedan importar en el entorno."""
    probe = "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in sys.argv[1:]) else 1)"
    result = subprocess.run([str(python), "-c", probe, *REQUIRED_MODULES], check=False)
    return result.returncode == 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    if sys.version_info < MIN_PYTHON:
        log.error("Se requiere Python %d.%d o superior; se está usando %s.", *MIN_PYTHON, sys.version.split()[0])
        return 1
    python = ensure_venv()
    updated = False
    if os.environ.get("MA_SKIP_UPDATE") != "1":
        updated = sync_dependencies(python, runtime_requirements())
    if not updated and not modules_available(python):
        log.error(
            "Faltan dependencias y no fue posible instalarlas. Revise la conexión a internet y vuelva a intentarlo."
        )
        return 1
    if not updated:
        log.info("Se continúa con las dependencias ya instaladas.")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.call([str(python), "-m", PACKAGE, *sys.argv[1:]], cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
