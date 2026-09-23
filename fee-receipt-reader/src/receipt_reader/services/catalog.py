"""Catálogos y parámetros: configuración, programas, tasas, prestadores e importaciones.

Todo cambio que afecta la validación (parámetros, tasas de retención, valores
hora de referencia) se guarda en una transacción y después revalida todas las
boletas no descartadas.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from receipt_reader.data import catalog_repo, db, excel_import, receipt_repo
from receipt_reader.domain.forms import validate_alias_fields, validate_program_fields, validate_settings
from receipt_reader.domain.models import Program, ProgramAlias, Provider, ReferenceRate, Settings
from receipt_reader.domain.records import RejectedRow
from receipt_reader.domain.text import collapse_spaces, plural
from receipt_reader.errors import ValidationError
from receipt_reader.services.context import Clock, load_snapshot, revalidate, system_clock

MIN_RATE_YEAR = 2000
MAX_RATE_YEAR = 2100
MAX_RATE_BP = 5000


@dataclass(frozen=True)
class ImportReport:
    """Resultado de una importación: filas guardadas y rechazadas con su motivo."""

    imported: int
    rejected: tuple[RejectedRow, ...]

    def message(self) -> str:
        text = plural(self.imported, "fila importada", "filas importadas")
        if self.rejected:
            text += ", " + plural(len(self.rejected), "rechazada")
        return text + "."


class CatalogService:
    """Casos de uso de configuración y catálogos."""

    def __init__(self, db_path: Path, *, clock: Clock = system_clock) -> None:
        self.db_path = db_path
        self._clock = clock

    def settings(self) -> Settings:
        with db.session(self.db_path) as conn:
            return catalog_repo.load_settings(conn)

    def programs(self) -> list[Program]:
        with db.session(self.db_path) as conn:
            return catalog_repo.list_programs(conn)

    def aliases(self) -> list[ProgramAlias]:
        with db.session(self.db_path) as conn:
            return catalog_repo.list_aliases(conn)

    def create_program(self, folder_code: str, name: str, short_name: str) -> int:
        """Crea un programa y revalida (puede resolver boletas con FOLDER_PROGRAM_UNKNOWN)."""
        folder_code, name, short_name = folder_code.strip().zfill(3), collapse_spaces(name), collapse_spaces(short_name)
        validate_program_fields(folder_code, name, short_name)
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.insert_program(conn, folder_code, name, short_name)
        return self.revalidate_all()

    def update_program(self, program_id: int, folder_code: str, name: str, short_name: str, *, active: bool) -> int:
        """Edita un programa (código, nombres o si está activo) y revalida las boletas."""
        folder_code, name, short_name = folder_code.strip().zfill(3), collapse_spaces(name), collapse_spaces(short_name)
        validate_program_fields(folder_code, name, short_name)
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.update_program(conn, program_id, folder_code, name, short_name, active=active)
        return self.revalidate_all()

    def set_program_active(self, program_id: int, active: bool) -> int:
        """Activa o desactiva un programa sin tocar sus demás datos ni las boletas ya asignadas."""
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.set_program_active(conn, program_id, active)
        return self.revalidate_all()

    def add_alias(self, program_id: int, alias: str, priority: int) -> int:
        """Agrega un alias de glosa al programa (menor prioridad gana) y revalida."""
        alias = collapse_spaces(alias)
        validate_alias_fields(alias, priority)
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.insert_alias(conn, ProgramAlias(program_id, alias, priority))
        return self.revalidate_all()

    def remove_alias(self, program_id: int, alias: str) -> int:
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.delete_alias(conn, program_id, alias)
        return self.revalidate_all()

    def retention_rates(self) -> dict[int, int]:
        with db.session(self.db_path) as conn:
            return catalog_repo.load_retention_table(conn).as_dict()

    def reference_rates(self) -> list[ReferenceRate]:
        with db.session(self.db_path) as conn:
            return catalog_repo.list_reference_rates(conn)

    def providers(self) -> list[Provider]:
        with db.session(self.db_path) as conn:
            return catalog_repo.list_providers(conn)

    def update_settings(self, settings: Settings) -> int:
        """Guarda la configuración y revalida todas las boletas. Devuelve cuántas cambiaron de estado."""
        validate_settings(settings)
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.save_settings(conn, settings)
        return self.revalidate_all()

    def update_retention_rate(self, year: int, rate_bp: int) -> int:
        """Fija la tasa legal de un año (por ejemplo, si la ley la cambia) y revalida las boletas.

        Devuelve cuántas boletas cambiaron de estado.
        """
        if not MIN_RATE_YEAR <= year <= MAX_RATE_YEAR:
            raise ValidationError(f"El año debe estar entre {MIN_RATE_YEAR} y {MAX_RATE_YEAR}.")
        if not 0 < rate_bp <= MAX_RATE_BP:
            raise ValidationError("La tasa de retención debe ser mayor que 0 y no superar el 50 %.")
        with db.session(self.db_path) as conn, db.transaction(conn):
            catalog_repo.upsert_retention_rate(conn, year, rate_bp)
        return self.revalidate_all()

    def revalidate_all(self) -> int:
        """Vuelve a validar todas las boletas no descartadas con los parámetros vigentes."""
        now = self._clock()
        with db.session(self.db_path) as conn, db.transaction(conn):
            snapshot = load_snapshot(conn)
            # Todas en una sola pasada: el orden de escritura respeta el folio único entre boletas válidas.
            results = revalidate(conn, snapshot, receipt_repo.active_receipt_ids(conn), now)
        return sum(1 for result in results if result.status is not result.record.status)

    def import_providers(self, path: Path) -> ImportReport:
        result = excel_import.read_providers(path, self._clock())
        with db.session(self.db_path) as conn, db.transaction(conn):
            imported = catalog_repo.upsert_providers(conn, result.accepted)
        return ImportReport(imported, tuple(result.rejected))

    def import_reference_rates(self, path: Path) -> ImportReport:
        with db.session(self.db_path) as conn:
            codes = {program.folder_code: program.id for program in catalog_repo.list_programs(conn)}
        result = excel_import.read_reference_rates(path, codes)
        with db.session(self.db_path) as conn, db.transaction(conn):
            imported = catalog_repo.upsert_reference_rates(conn, result.accepted)
        if imported:
            self.revalidate_all()
        return ImportReport(imported, tuple(result.rejected))

    def import_programs(self, path: Path) -> ImportReport:
        """Importa programas (los crea o actualiza por código de carpeta) y después sus alias.

        Ambos se importan en una sola operación porque los alias dependen de los programas
        recién creados; si un programa se rechaza, sus alias también (código inexistente).
        """
        program_result = excel_import.read_programs(path)
        with db.session(self.db_path) as conn, db.transaction(conn):
            codes: dict[str, int] = {}
            for code, name, short_name, active in program_result.accepted:
                codes[code] = catalog_repo.upsert_program(conn, code, name, short_name, active=active)
            existing = {program.folder_code: program.id for program in catalog_repo.list_programs(conn)}
            all_codes = existing | codes
        alias_result = excel_import.read_aliases(path, all_codes)
        with db.session(self.db_path) as conn, db.transaction(conn):
            for alias in alias_result.accepted:
                catalog_repo.upsert_alias(conn, alias)
        imported = len(program_result.accepted) + len(alias_result.accepted)
        rejected = tuple(program_result.rejected) + tuple(alias_result.rejected)
        if imported:
            self.revalidate_all()
        return ImportReport(imported, rejected)

    def write_provider_template(self, path: Path) -> Path:
        return excel_import.write_provider_template(path, self.providers())

    def write_reference_template(self, path: Path) -> Path:
        return excel_import.write_reference_template(path, self.programs(), self.reference_rates())

    def write_program_template(self, path: Path) -> Path:
        return excel_import.write_program_template(path, self.programs(), self.aliases())
