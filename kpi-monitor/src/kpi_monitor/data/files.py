"""Escritura segura de archivos exportados."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from kpi_monitor.errors import DataError

log = logging.getLogger(__name__)


def temporary_sibling(path: Path) -> Path:
    """Ruta temporal en la misma carpeta del destino (así el reemplazo final no cruza discos)."""
    return path.with_name(f"{path.name}.tmp")


@contextmanager
def replace_on_success(path: Path) -> Iterator[Path]:
    """Entrega una ruta temporal y la mueve a `path` solo si el bloque termina sin errores.

    Si el bloque falla o se cancela, el temporal se borra y el archivo que ya
    existía en `path` queda intacto: nunca se deja un archivo a medias.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DataError(f"No se pudo crear la carpeta de {path.name}.") from error
    temporary = temporary_sibling(path)
    try:
        yield temporary
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    try:
        temporary.replace(path)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise DataError(
            f"No se pudo guardar {path.name}. Verifique que el archivo no esté abierto en otro programa."
        ) from error
    log.info("Archivo guardado en %s", path)
