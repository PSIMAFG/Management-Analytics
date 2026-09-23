"""Resultado de una importación validada fila por fila."""

from __future__ import annotations

from dataclasses import dataclass

from staffing_simulator.domain.text import plural


@dataclass(frozen=True)
class RejectedRow:
    """Fila de la planilla que no se importó y el motivo."""

    row_number: int
    reason: str


@dataclass(frozen=True)
class ImportReport:
    """Resumen de una importación.

    `applied` indica si los cambios se guardaron. Una importación nunca queda
    a medias: o se guardan todas las filas aceptadas en una sola transacción
    o no se guarda ninguna. `replaced` cuenta las filas aceptadas que
    reemplazan un dato ya registrado (por ejemplo, el monto ejecutado de un
    programa en un mes).
    """

    total_rows: int
    accepted: int
    rejected: tuple[RejectedRow, ...]
    applied: bool
    created_persons: int = 0
    replaced: int = 0

    @property
    def imported(self) -> int:
        return self.accepted if self.applied else 0

    @property
    def summary(self) -> str:
        if self.total_rows == 0:
            return "La planilla no tiene filas con datos."
        rows = plural(self.total_rows, "fila", "filas")
        if self.applied:
            verb = "Se importó" if self.accepted == 1 else "Se importaron"
            text = f"{verb} {self.accepted} de {rows}."
            if self.rejected:
                skipped = "Se omitió" if len(self.rejected) == 1 else "Se omitieron"
                text += f" {skipped} {plural(len(self.rejected), 'fila', 'filas')} con errores."
            return text
        if self.rejected:
            verb = "tiene" if len(self.rejected) == 1 else "tienen"
            return (
                f"No se importó ninguna fila: {len(self.rejected)} de {rows} {verb} errores. "
                "Corrija la planilla o importe solo las filas válidas."
            )
        ready = "lista" if self.accepted == 1 else "listas"
        return f"La planilla es válida: {plural(self.accepted, 'fila', 'filas')} {ready} para importar."
