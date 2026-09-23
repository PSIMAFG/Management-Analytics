"""Tipos de las funciones de avance y de cancelación que reciben las tareas largas."""

from __future__ import annotations

from collections.abc import Callable

# Recibe el porcentaje de avance (0 a 100) y un mensaje breve para el usuario.
ProgressFn = Callable[[int, str], None]
# Devuelve verdadero cuando el usuario pidió detener la tarea.
CancelCheck = Callable[[], bool]
