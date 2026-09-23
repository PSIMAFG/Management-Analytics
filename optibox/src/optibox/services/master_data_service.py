"""Lectura y edición validada de datos maestros (demanda, contratos, ausencias)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from optibox.data import db
from optibox.data.repository import MasterDataRepository
from optibox.domain.models import (
    PRIORITY_LABELS,
    Absence,
    AbsenceRecord,
    AbsenceStatus,
    Contract,
    DemandItem,
    MasterData,
    Role,
    Room,
    Scenario,
    ServiceType,
    Staff,
)
from optibox.domain.timegrid import SLOT_MINUTES, WEEKDAY_NAMES, format_range, require_range
from optibox.errors import DataError, ValidationError

log = logging.getLogger(__name__)

MAX_SESSIONS_PER_BLOCK = 50


@dataclass(frozen=True)
class DemandRow:
    """Fila editable de la demanda: una por (día, bloque, tipo), incluidas las que valen cero."""

    weekday: int
    day_name: str
    block_start: int
    block_label: str
    service_code: str
    service_name: str
    sessions: int
    priority: int

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS[self.priority]


class MasterDataService:
    """Acceso a los datos maestros para la interfaz, con validación antes de escribir."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def master_data(self) -> MasterData:
        with db.session(self.db_path) as conn:
            return MasterDataRepository(conn).load()

    def staff(self) -> tuple[Staff, ...]:
        return self.master_data().staff

    def roles(self) -> tuple[Role, ...]:
        return self.master_data().roles

    def rooms(self) -> tuple[Room, ...]:
        return self.master_data().rooms

    def service_types(self) -> tuple[ServiceType, ...]:
        return self.master_data().service_types

    def scenarios(self) -> tuple[Scenario, ...]:
        return self.master_data().scenarios

    def absences(self) -> tuple[AbsenceRecord, ...]:
        with db.session(self.db_path) as conn:
            return MasterDataRepository(conn).absences()

    def demand_grid(self) -> tuple[DemandRow, ...]:
        """Todas las combinaciones (día, bloque, tipo) válidas según el horario del centro."""
        master = self.master_data()
        current = {item.key: item for item in master.demand}
        rows: list[DemandRow] = []
        for hours in sorted(master.center_hours, key=lambda h: h.weekday):
            for block in hours.blocks():
                for service in master.service_types:
                    item = current.get((hours.weekday, block, service.code))
                    rows.append(
                        DemandRow(
                            weekday=hours.weekday,
                            day_name=WEEKDAY_NAMES[hours.weekday].capitalize(),
                            block_start=block,
                            block_label=format_range(block, block + 60),
                            service_code=service.code,
                            service_name=service.name,
                            sessions=item.sessions if item else 0,
                            priority=item.priority if item else service.priority,
                        )
                    )
        return tuple(rows)

    def update_demand(
        self, weekday: int, block_start: int, service_code: str, sessions: int, priority: int
    ) -> DemandItem:
        """Guarda la demanda de un (día, bloque, tipo) después de validarla."""
        item = self._validated_demand(self.master_data(), weekday, block_start, service_code, sessions, priority)
        with db.session(self.db_path) as conn, db.transaction(conn):
            MasterDataRepository(conn).upsert_demand(item)
        log.info("Demanda actualizada: %s", item)
        return item

    def save_demand(self, items: Iterable[DemandItem]) -> int:
        """Guarda varias filas de demanda en una sola transacción: se validan todas antes de escribir.

        Devuelve la cantidad de filas guardadas. Si alguna fila no es válida no
        se guarda ninguna y se informa la primera con problemas.
        """
        master = self.master_data()
        validated: list[DemandItem] = []
        seen: set[tuple[int, int, str]] = set()
        for item in items:
            checked = self._validated_demand(
                master, item.weekday, item.block_start, item.service_code, item.sessions, item.priority
            )
            if checked.key in seen:
                block = format_range(item.block_start, item.block_start + 60)
                raise ValidationError(
                    f"La demanda del {WEEKDAY_NAMES[item.weekday]} en el bloque {block} "
                    f"para {item.service_code} aparece más de una vez."
                )
            seen.add(checked.key)
            validated.append(checked)
        with db.session(self.db_path) as conn, db.transaction(conn):
            repo = MasterDataRepository(conn)
            for item in validated:
                repo.upsert_demand(item)
        log.info("Demanda guardada: %d filas", len(validated))
        return len(validated)

    @staticmethod
    def _validated_demand(
        master: MasterData, weekday: int, block_start: int, service_code: str, sessions: int, priority: int
    ) -> DemandItem:
        if not 0 <= sessions <= MAX_SESSIONS_PER_BLOCK:
            raise ValidationError(f"Las sesiones requeridas deben estar entre 0 y {MAX_SESSIONS_PER_BLOCK}.")
        item = DemandItem(weekday, block_start, service_code, sessions, priority)
        if service_code not in {service.code for service in master.service_types}:
            raise ValidationError(f"El tipo de atención {service_code} no existe.")
        hours = {h.weekday: h for h in master.center_hours}.get(weekday)
        if hours is None or block_start not in hours.blocks():
            raise ValidationError("El bloque indicado está fuera del horario del centro para ese día.")
        return item

    def add_contract(self, staff_code: str, valid_from: date, weekly_hours: float) -> Contract:
        """Crea una nueva versión de contrato; el contrato abierto anterior se cierra el día previo."""
        minutes = round(weekly_hours * 60)
        if minutes <= 0 or minutes % SLOT_MINUTES:
            raise ValidationError("Las horas semanales deben ser positivas y equivaler a múltiplos de 15 minutos.")
        if minutes > 60 * 60:
            raise ValidationError("Las horas semanales no pueden superar 60.")
        contract = Contract(valid_from, None, minutes)
        try:
            with db.session(self.db_path) as conn, db.transaction(conn):
                MasterDataRepository(conn).add_contract(staff_code, contract)
        except sqlite3.IntegrityError as error:
            raise DataError("No se pudo guardar el contrato: revise las vigencias.") from error
        return contract

    def set_absence_status(self, absence_id: int, status: AbsenceStatus) -> None:
        """Cambia el estado de una ausencia (solo las aprobadas bloquean la agenda)."""
        with db.session(self.db_path) as conn, db.transaction(conn):
            MasterDataRepository(conn).set_absence_status(absence_id, status)

    def add_absence(
        self,
        staff_code: str,
        day: date,
        kind: str,
        status: AbsenceStatus = AbsenceStatus.PENDING,
        start: int | None = None,
        end: int | None = None,
    ) -> AbsenceRecord:
        """Registra una ausencia de día completo (sin franja) o parcial, validada contra la grilla."""
        if not kind.strip():
            raise ValidationError("Indique el motivo de la ausencia.")
        if day.weekday() > 4:
            raise ValidationError("Las ausencias se registran en días hábiles (lunes a viernes).")
        if start is not None and end is not None:
            require_range(start, end, "la ausencia")
        absence = Absence(staff_code, day, start, end, kind.strip(), status)
        if staff_code not in {person.code for person in self.staff()}:
            raise ValidationError(f"La persona {staff_code} no existe.")
        with db.session(self.db_path) as conn, db.transaction(conn):
            absence_id = MasterDataRepository(conn).add_absence(absence)
        log.info("Ausencia registrada para %s el %s", staff_code, day.isoformat())
        return AbsenceRecord(absence_id, absence)

    def delete_absence(self, absence_id: int) -> None:
        with db.session(self.db_path) as conn, db.transaction(conn):
            MasterDataRepository(conn).delete_absence(absence_id)
