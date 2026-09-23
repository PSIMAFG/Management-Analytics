"""Redacción de textos para el usuario que dependen de una cantidad."""

from __future__ import annotations


def plural(count: int, singular: str, plural_form: str) -> str:
    """Cantidad con el sustantivo concordado: (1, 'fila', 'filas') -> '1 fila'; (3, ...) -> '3 filas'."""
    return f"{count} {singular if count == 1 else plural_form}"
