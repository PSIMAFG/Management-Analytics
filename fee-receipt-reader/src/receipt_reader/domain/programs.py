"""Resolución del programa que financia una boleta.

Fuentes en orden de precedencia:

1. Selección del usuario en la revisión.
2. Código de 3 dígitos de la carpeta de origen.
3. Texto de la glosa, buscando los alias del catálogo como palabras completas y
   respetando su prioridad declarada (menor número gana).

Si la carpeta y el texto apuntan a programas distintos, se usa la carpeta y se
marca el conflicto para que una persona lo resuelva. Nunca se infiere el
programa por el RUT del prestador sin confirmación.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from receipt_reader.domain.models import FieldSource, ProgramAlias
from receipt_reader.domain.text import fold, normalize_text


@dataclass(frozen=True)
class TextProgramMatch:
    """Resultado de buscar programas en un texto."""

    program_id: int | None
    alias: str | None = None
    ambiguous: bool = False
    candidates: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProgramResolution:
    """Programa resuelto con su origen y las discrepancias detectadas."""

    program_id: int | None
    source: FieldSource | None
    folder_program_id: int | None
    text_program_id: int | None
    text_ambiguous: bool = False

    @property
    def conflict(self) -> bool:
        return (
            self.folder_program_id is not None
            and self.text_program_id is not None
            and self.folder_program_id != self.text_program_id
        )


def _alias_pattern(alias: str) -> re.Pattern[str]:
    words = fold(normalize_text(alias)).split()
    body = r"\s+".join(re.escape(word) for word in words)
    return re.compile(r"(?<![A-Z0-9])" + body + r"(?![A-Z0-9])")


class ProgramMatcher:
    """Busca alias de programas como palabras completas en un texto."""

    def __init__(self, aliases: Iterable[ProgramAlias]) -> None:
        self._aliases: list[tuple[ProgramAlias, re.Pattern[str]]] = [
            (alias, _alias_pattern(alias.alias)) for alias in aliases if alias.alias.strip()
        ]

    def match(self, text: str | None) -> TextProgramMatch:
        if not text:
            return TextProgramMatch(None)
        folded = fold(normalize_text(text))
        hits = [alias for alias, pattern in self._aliases if pattern.search(folded)]
        if not hits:
            return TextProgramMatch(None)
        best_priority = min(alias.priority for alias in hits)
        best = sorted({alias.program_id for alias in hits if alias.priority == best_priority})
        candidates = tuple(sorted({alias.program_id for alias in hits}))
        if len(best) > 1:
            return TextProgramMatch(None, None, ambiguous=True, candidates=candidates)
        winner = next(alias for alias in hits if alias.priority == best_priority)
        return TextProgramMatch(winner.program_id, winner.alias, ambiguous=False, candidates=candidates)


def resolve_program(
    folder_program_id: int | None, text_match: TextProgramMatch, text_source: FieldSource
) -> ProgramResolution:
    """Aplica la precedencia carpeta > texto (la selección del usuario se aplica en la revisión)."""
    if folder_program_id is not None:
        return ProgramResolution(
            folder_program_id, FieldSource.FOLDER, folder_program_id, text_match.program_id, text_match.ambiguous
        )
    if text_match.program_id is not None:
        return ProgramResolution(text_match.program_id, text_source, None, text_match.program_id, False)
    return ProgramResolution(None, None, None, None, text_match.ambiguous)


def habitual_program(program_ids: Sequence[int]) -> tuple[int, int] | None:
    """Programa más frecuente de una lista (conteo real, no el primero visto).

    Devuelve (programa, veces) o None si hay empate o la lista está vacía.
    """
    counts: dict[int, int] = {}
    for program_id in program_ids:
        counts[program_id] = counts.get(program_id, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0]
