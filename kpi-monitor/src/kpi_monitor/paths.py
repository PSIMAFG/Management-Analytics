"""Ubicación de la base de datos, los logs y las exportaciones.

Desde el código fuente los datos quedan en `<proyecto>/data`; desde el
ejecutable, en una carpeta `data` junto al .exe. Si esa carpeta no admite
escritura se usa `%LOCALAPPDATA%/ManagementAnalytics/<aplicación>`.
La variable de entorno `KPI_MONITOR_DATA_DIR` permite forzar otra ruta.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kpi_monitor import APP_NAME

DATA_DIR_ENV = "KPI_MONITOR_DATA_DIR"
DB_FILENAME = "kpi_monitor.db"


@dataclass(frozen=True)
class AppPaths:
    """Rutas de trabajo de la aplicación."""

    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exportaciones"

    def ensure(self) -> AppPaths:
        for folder in (self.data_dir, self.log_dir, self.export_dir):
            folder.mkdir(parents=True, exist_ok=True)
        return self


def is_frozen() -> bool:
    """Indica si la aplicación corre como ejecutable de PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError:
        return False
    return True


def resolve_paths(override: Path | None = None) -> AppPaths:
    """Determina la carpeta de datos y crea la estructura si falta."""
    if override is not None:
        return AppPaths(override.resolve()).ensure()
    env_value = os.environ.get(DATA_DIR_ENV)
    if env_value:
        return AppPaths(Path(env_value).resolve()).ensure()
    candidate = _base_dir() / "data"
    if _is_writable(candidate):
        return AppPaths(candidate).ensure()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return AppPaths(local / "ManagementAnalytics" / APP_NAME).ensure()
