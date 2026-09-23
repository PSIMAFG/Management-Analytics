"""Repositorios SQLite: leen y escriben objetos de dominio."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from optibox.data import codec
from optibox.domain.diagnosis import UnmetCause, UnmetDemand
from optibox.domain.instance import PlanningInstance
from optibox.domain.models import (
    Absence,
    AbsenceRecord,
    AbsenceStatus,
    Audience,
    AvailabilityWindow,
    Blocking,
    Contract,
    DemandItem,
    Holiday,
    MasterData,
    PlanningSettings,
    Role,
    Room,
    RoomKind,
    RoomRule,
    RoomRuleKind,
    Scenario,
    ServiceTarget,
    ServiceType,
    Staff,
)
from optibox.domain.plan import AdminBlock, Plan, Session
from optibox.domain.run import RuleExclusion, RunSummary
from optibox.domain.timegrid import DayHours
from optibox.errors import DataError

# Tablas que se vacían al reemplazar los datos maestros. Los escenarios se
# conservan (se actualizan por nombre) para que sus identificadores sigan siendo
# válidos en el historial de corridas y en la interfaz.
MASTER_TABLES = (
    "scenario_weight",
    "demand",
    "blocking",
    "holiday",
    "absence",
    "availability",
    "staff_room",
    "staff_target",
    "staff_skill",
    "service_room",
    "role_service",
    "contract",
    "staff",
    "room",
    "service_type",
    "role",
    "center_hours",
)

SETTING_KEYS = {
    "extra_admin_daily_cap_min": int,
    "extra_admin_weight": int,
    "time_limit_s": float,
    "seed": int,
    "workers": int,
    "phase_a_share": float,
}


def _opt_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


class MasterDataRepository:
    """Lectura y escritura de datos maestros."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _ids(self, table: str) -> dict[str, int]:
        return {row["code"]: row["id"] for row in self.conn.execute(f"SELECT id, code FROM {table}")}

    def setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def settings(self) -> PlanningSettings:
        values: dict[str, int | float] = {}
        for key, cast in SETTING_KEYS.items():
            text = self.setting(key)
            if text is not None:
                values[key] = cast(text)
        return PlanningSettings(**values)  # type: ignore[arg-type]

    def save_settings(self, settings: PlanningSettings) -> None:
        for key in SETTING_KEYS:
            self.set_setting(key, str(getattr(settings, key)))

    def load(self) -> MasterData:
        """Lee todos los datos maestros como objetos de dominio validados."""
        conn = self.conn
        services_by_role: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT rs.role_id, st.code FROM role_service rs JOIN service_type st ON st.id = rs.service_type_id"
        ):
            services_by_role[row["role_id"]].add(row["code"])
        role_code_by_id: dict[int, str] = {}
        roles: list[Role] = []
        for row in conn.execute("SELECT * FROM role ORDER BY code"):
            role_code_by_id[row["id"]] = row["code"]
            roles.append(
                Role(row["code"], row["name"], bool(row["delivers_services"]), frozenset(services_by_role[row["id"]]))
            )

        rooms_by_service: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT sr.service_type_id, r.code FROM service_room sr JOIN room r ON r.id = sr.room_id"
        ):
            rooms_by_service[row["service_type_id"]].add(row["code"])
        services = [
            ServiceType(
                code=row["code"],
                name=row["name"],
                duration_min=row["duration_min"],
                participants=row["participants"],
                requires_group_room=bool(row["requires_group_room"]),
                requires_evaluation_room=bool(row["requires_evaluation_room"]),
                admin_minutes=row["admin_minutes"],
                priority=row["priority"],
                color=row["color"],
                rooms=frozenset(rooms_by_service[row["id"]]),
                max_week_total=row["max_week_total"],
                max_week_per_staff=row["max_week_per_staff"],
                max_day_total=row["max_day_total"],
                max_day_per_staff=row["max_day_per_staff"],
            )
            for row in conn.execute("SELECT * FROM service_type ORDER BY code")
        ]

        rooms = [
            Room(
                code=row["code"],
                name=row["name"],
                kind=RoomKind(row["kind"]),
                allows_group=bool(row["allows_group"]),
                allows_evaluation=bool(row["allows_evaluation"]),
                capacity=row["capacity"],
                active=bool(row["active"]),
                reserved_role=role_code_by_id.get(row["reserved_role_id"]) if row["reserved_role_id"] else None,
                simultaneous_hard=row["simultaneous_hard"],
                simultaneous_soft=row["simultaneous_soft"],
            )
            for row in conn.execute("SELECT * FROM room ORDER BY code")
        ]

        staff = self._load_staff(role_code_by_id)
        staff_code_by_id = {row["id"]: row["code"] for row in conn.execute("SELECT id, code FROM staff")}

        blockings = [
            Blocking(
                name=row["name"],
                weekday=row["weekday"],
                start=row["start_min"],
                end=row["end_min"],
                audience=Audience(row["audience"]),
                target=(
                    role_code_by_id.get(row["role_id"])
                    if row["role_id"]
                    else staff_code_by_id.get(row["staff_id"])
                    if row["staff_id"]
                    else None
                ),
                counts_as_admin=bool(row["counts_as_admin"]),
            )
            for row in conn.execute("SELECT * FROM blocking ORDER BY id")
        ]
        absences = tuple(record.absence for record in self.absences())
        holidays = [
            Holiday(date.fromisoformat(row["day"]), row["name"])
            for row in conn.execute("SELECT * FROM holiday ORDER BY day")
        ]
        hours = [
            DayHours(row["weekday"], row["open_min"], row["close_min"], row["lunch_start_min"], row["lunch_end_min"])
            for row in conn.execute("SELECT * FROM center_hours ORDER BY weekday")
        ]
        return MasterData(
            roles=tuple(roles),
            service_types=tuple(services),
            rooms=tuple(rooms),
            staff=staff,
            blockings=tuple(blockings),
            absences=absences,
            holidays=tuple(holidays),
            center_hours=tuple(hours),
            demand=self.demand(),
            scenarios=self.scenarios(),
            settings=self.settings(),
        )

    def _load_staff(self, role_code_by_id: dict[int, str]) -> tuple[Staff, ...]:
        conn = self.conn
        contracts: dict[int, list[Contract]] = defaultdict(list)
        for row in conn.execute("SELECT * FROM contract ORDER BY staff_id, valid_from"):
            contracts[row["staff_id"]].append(
                Contract(date.fromisoformat(row["valid_from"]), _opt_date(row["valid_to"]), row["weekly_minutes"])
            )
        windows: dict[int, list[AvailabilityWindow]] = defaultdict(list)
        for row in conn.execute("SELECT * FROM availability ORDER BY staff_id, weekday, start_min"):
            windows[row["staff_id"]].append(AvailabilityWindow(row["weekday"], row["start_min"], row["end_min"]))
        skills: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT s.staff_id, st.code FROM staff_skill s JOIN service_type st ON st.id = s.service_type_id"
        ):
            skills[row["staff_id"]].add(row["code"])
        room_rules: dict[int, list[RoomRule]] = defaultdict(list)
        for row in conn.execute(
            "SELECT sr.staff_id, r.code, sr.kind, sr.rank FROM staff_room sr JOIN room r ON r.id = sr.room_id "
            "ORDER BY sr.staff_id, COALESCE(sr.rank, 99), r.code"
        ):
            room_rules[row["staff_id"]].append(RoomRule(row["code"], RoomRuleKind(row["kind"]), row["rank"]))
        targets: dict[int, list[ServiceTarget]] = defaultdict(list)
        for row in conn.execute(
            "SELECT t.staff_id, st.code, t.min_minutes, t.max_minutes FROM staff_target t "
            "JOIN service_type st ON st.id = t.service_type_id ORDER BY t.staff_id, st.code"
        ):
            targets[row["staff_id"]].append(ServiceTarget(row["code"], row["min_minutes"], row["max_minutes"]))
        return tuple(
            Staff(
                code=row["code"],
                name=row["name"],
                role_code=role_code_by_id[row["role_id"]],
                active=bool(row["active"]),
                contracts=tuple(contracts[row["id"]]),
                availability=tuple(windows[row["id"]]),
                skills=frozenset(skills[row["id"]]),
                room_rules=tuple(room_rules[row["id"]]),
                targets=tuple(targets[row["id"]]),
            )
            for row in conn.execute("SELECT * FROM staff ORDER BY code")
        )

    def demand(self) -> tuple[DemandItem, ...]:
        return tuple(
            DemandItem(row["weekday"], row["block_start_min"], row["code"], row["sessions"], row["priority"])
            for row in self.conn.execute(
                "SELECT d.weekday, d.block_start_min, st.code, d.sessions, d.priority FROM demand d "
                "JOIN service_type st ON st.id = d.service_type_id ORDER BY d.weekday, d.block_start_min, st.code"
            )
        )

    def scenarios(self) -> tuple[Scenario, ...]:
        weights: dict[int, dict[str, float]] = defaultdict(dict)
        mix_min: dict[int, dict[str, float]] = defaultdict(dict)
        mix_max: dict[int, dict[str, float]] = defaultdict(dict)
        for row in self.conn.execute(
            "SELECT w.scenario_id, st.code, w.weight, w.mix_min, w.mix_max FROM scenario_weight w "
            "JOIN service_type st ON st.id = w.service_type_id ORDER BY st.code"
        ):
            weights[row["scenario_id"]][row["code"]] = float(row["weight"])
            if row["mix_min"] is not None:
                mix_min[row["scenario_id"]][row["code"]] = float(row["mix_min"])
            if row["mix_max"] is not None:
                mix_max[row["scenario_id"]][row["code"]] = float(row["mix_max"])
        return tuple(
            Scenario(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                service_weights=weights[row["id"]],
                mix_min=mix_min[row["id"]],
                mix_max=mix_max[row["id"]],
                room_preference_weight=row["room_preference_weight"],
                room_continuity_weight=row["room_continuity_weight"],
                admin_excess_weight=row["admin_excess_weight"],
                target_weight=row["target_weight"],
                mix_weight=row["mix_weight"],
                coverage_floor_pct=row["coverage_floor_pct"],
            )
            for row in self.conn.execute("SELECT * FROM scenario ORDER BY id")
        )

    def absences(self) -> tuple[AbsenceRecord, ...]:
        return tuple(
            AbsenceRecord(
                row["id"],
                Absence(
                    staff_code=row["code"],
                    day=date.fromisoformat(row["day"]),
                    start=row["start_min"],
                    end=row["end_min"],
                    kind=row["kind"],
                    status=AbsenceStatus(row["status"]),
                ),
            )
            for row in self.conn.execute(
                "SELECT a.*, s.code FROM absence a JOIN staff s ON s.id = a.staff_id ORDER BY a.day, s.code"
            )
        )

    def upsert_demand(self, item: DemandItem) -> None:
        service_id = self._ids("service_type").get(item.service_code)
        if service_id is None:
            raise DataError(f"No existe el tipo de atención {item.service_code}.")
        self.conn.execute(
            "INSERT INTO demand (weekday, block_start_min, service_type_id, sessions, priority) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(weekday, block_start_min, service_type_id) "
            "DO UPDATE SET sessions = excluded.sessions, priority = excluded.priority",
            (item.weekday, item.block_start, service_id, item.sessions, item.priority),
        )

    def add_absence(self, absence: Absence) -> int:
        """Registra una ausencia y devuelve su identificador."""
        staff_id = self._ids("staff").get(absence.staff_code)
        if staff_id is None:
            raise DataError(f"No existe la persona {absence.staff_code}.")
        cursor = self.conn.execute(
            "INSERT INTO absence (staff_id, day, start_min, end_min, kind, status) VALUES (?, ?, ?, ?, ?, ?)",
            (staff_id, absence.day.isoformat(), absence.start, absence.end, absence.kind, absence.status.value),
        )
        return int(cursor.lastrowid or 0)

    def delete_absence(self, absence_id: int) -> None:
        cursor = self.conn.execute("DELETE FROM absence WHERE id = ?", (absence_id,))
        if cursor.rowcount == 0:
            raise DataError("La ausencia indicada ya no existe.")

    def add_contract(self, staff_code: str, contract: Contract) -> None:
        """Agrega un contrato nuevo. Si hay uno abierto que empieza antes, lo cierra el día anterior."""
        staff_id = self._ids("staff").get(staff_code)
        if staff_id is None:
            raise DataError(f"No existe la persona {staff_code}.")
        self.conn.execute(
            "UPDATE contract SET valid_to = ? WHERE staff_id = ? AND valid_to IS NULL AND valid_from < ?",
            ((contract.valid_from - timedelta(days=1)).isoformat(), staff_id, contract.valid_from.isoformat()),
        )
        try:
            self.conn.execute(
                "INSERT INTO contract (staff_id, valid_from, valid_to, weekly_minutes) VALUES (?, ?, ?, ?)",
                (
                    staff_id,
                    contract.valid_from.isoformat(),
                    contract.valid_to.isoformat() if contract.valid_to else None,
                    contract.weekly_minutes,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise DataError("El contrato se solapa con otro contrato vigente de la misma persona.") from error

    def set_absence_status(self, absence_id: int, status: AbsenceStatus) -> None:
        cursor = self.conn.execute("UPDATE absence SET status = ? WHERE id = ?", (status.value, absence_id))
        if cursor.rowcount == 0:
            raise DataError("La ausencia indicada ya no existe.")

    def replace_all(self, master: MasterData) -> None:
        """Reemplaza todos los datos maestros (las corridas y la configuración se conservan)."""
        for table in MASTER_TABLES:
            self.conn.execute(f"DELETE FROM {table}")
        self.insert_all(master)

    def insert_all(self, master: MasterData) -> None:
        """Inserta los datos maestros en una base sin datos maestros."""
        self._insert_center_hours(master)
        role_ids = self._insert_roles(master)
        service_ids = self._insert_service_types(master)
        room_ids = self._insert_rooms(master, role_ids)
        self._insert_service_links(master, role_ids, service_ids, room_ids)
        for person in master.staff:
            self._insert_staff(person, role_ids, service_ids, room_ids)
        staff_ids = self._ids("staff")
        self._insert_blockings(master, role_ids, staff_ids)
        self._insert_absences(master, staff_ids)
        self._insert_holidays_and_demand(master, service_ids)
        self._insert_scenarios(master, service_ids)
        self.save_settings(master.settings)

    def _insert_center_hours(self, master: MasterData) -> None:
        for hours in master.center_hours:
            self.conn.execute(
                "INSERT INTO center_hours VALUES (?, ?, ?, ?, ?)",
                (hours.weekday, hours.open_min, hours.close_min, hours.lunch_start, hours.lunch_end),
            )

    def _insert_roles(self, master: MasterData) -> dict[str, int]:
        for role in master.roles:
            self.conn.execute(
                "INSERT INTO role (code, name, delivers_services) VALUES (?, ?, ?)",
                (role.code, role.name, int(role.delivers_services)),
            )
        return self._ids("role")

    def _insert_service_types(self, master: MasterData) -> dict[str, int]:
        for service in master.service_types:
            self.conn.execute(
                "INSERT INTO service_type (code, name, duration_min, participants, requires_group_room, "
                "requires_evaluation_room, admin_minutes, priority, color, max_week_total, max_week_per_staff, "
                "max_day_total, max_day_per_staff) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    service.code,
                    service.name,
                    service.duration_min,
                    service.participants,
                    int(service.requires_group_room),
                    int(service.requires_evaluation_room),
                    service.admin_minutes,
                    service.priority,
                    service.color,
                    service.max_week_total,
                    service.max_week_per_staff,
                    service.max_day_total,
                    service.max_day_per_staff,
                ),
            )
        return self._ids("service_type")

    def _insert_rooms(self, master: MasterData, role_ids: dict[str, int]) -> dict[str, int]:
        for room in master.rooms:
            self.conn.execute(
                "INSERT INTO room (code, name, kind, allows_group, allows_evaluation, capacity, active, "
                "reserved_role_id, simultaneous_hard, simultaneous_soft) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    room.code,
                    room.name,
                    room.kind.value,
                    int(room.allows_group),
                    int(room.allows_evaluation),
                    room.capacity,
                    int(room.active),
                    role_ids[room.reserved_role] if room.reserved_role else None,
                    room.simultaneous_hard,
                    room.simultaneous_soft,
                ),
            )
        return self._ids("room")

    def _insert_service_links(
        self,
        master: MasterData,
        role_ids: dict[str, int],
        service_ids: dict[str, int],
        room_ids: dict[str, int],
    ) -> None:
        """Qué cargos realizan cada tipo de atención y en qué salas se puede atender."""
        for role in master.roles:
            for code in sorted(role.services):
                self.conn.execute("INSERT INTO role_service VALUES (?, ?)", (role_ids[role.code], service_ids[code]))
        for service in master.service_types:
            for code in sorted(service.rooms):
                self.conn.execute("INSERT INTO service_room VALUES (?, ?)", (service_ids[service.code], room_ids[code]))

    def _insert_blockings(self, master: MasterData, role_ids: dict[str, int], staff_ids: dict[str, int]) -> None:
        for blocking in master.blockings:
            target = blocking.target
            self.conn.execute(
                "INSERT INTO blocking (name, weekday, start_min, end_min, audience, role_id, staff_id, "
                "counts_as_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    blocking.name,
                    blocking.weekday,
                    blocking.start,
                    blocking.end,
                    blocking.audience.value,
                    role_ids[target] if blocking.audience is Audience.ROLE and target else None,
                    staff_ids[target] if blocking.audience is Audience.STAFF and target else None,
                    int(blocking.counts_as_admin),
                ),
            )

    def _insert_absences(self, master: MasterData, staff_ids: dict[str, int]) -> None:
        for absence in master.absences:
            self.conn.execute(
                "INSERT INTO absence (staff_id, day, start_min, end_min, kind, status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    staff_ids[absence.staff_code],
                    absence.day.isoformat(),
                    absence.start,
                    absence.end,
                    absence.kind,
                    absence.status.value,
                ),
            )

    def _insert_holidays_and_demand(self, master: MasterData, service_ids: dict[str, int]) -> None:
        for holiday in master.holidays:
            self.conn.execute("INSERT INTO holiday (day, name) VALUES (?, ?)", (holiday.day.isoformat(), holiday.name))
        for item in master.demand:
            self.conn.execute(
                "INSERT INTO demand (weekday, block_start_min, service_type_id, sessions, priority) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.weekday, item.block_start, service_ids[item.service_code], item.sessions, item.priority),
            )

    def _insert_scenarios(self, master: MasterData, service_ids: dict[str, int]) -> None:
        """Escenarios (actualizados por nombre) con sus pesos y cotas de mezcla por tipo de atención."""
        for scenario in master.scenarios:
            scenario_id = self._upsert_scenario(scenario)
            codes = sorted(set(scenario.service_weights) | set(scenario.mix_min) | set(scenario.mix_max))
            for code in codes:
                if code not in service_ids:
                    continue
                self.conn.execute(
                    "INSERT INTO scenario_weight (scenario_id, service_type_id, weight, mix_min, mix_max) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        scenario_id,
                        service_ids[code],
                        scenario.service_weights.get(code, 1.0),
                        scenario.mix_min.get(code),
                        scenario.mix_max.get(code),
                    ),
                )

    def _upsert_scenario(self, scenario: Scenario) -> int:
        """Inserta el escenario o actualiza el existente con el mismo nombre; devuelve su id."""
        values = (
            scenario.description,
            scenario.room_preference_weight,
            scenario.room_continuity_weight,
            scenario.admin_excess_weight,
            scenario.target_weight,
            scenario.mix_weight,
            scenario.coverage_floor_pct,
        )
        row = self.conn.execute("SELECT id FROM scenario WHERE name = ?", (scenario.name,)).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE scenario SET description = ?, room_preference_weight = ?, room_continuity_weight = ?, "
                "admin_excess_weight = ?, target_weight = ?, mix_weight = ?, coverage_floor_pct = ? WHERE id = ?",
                (*values, row["id"]),
            )
            return int(row["id"])
        cursor = self.conn.execute(
            "INSERT INTO scenario (name, description, room_preference_weight, room_continuity_weight, "
            "admin_excess_weight, target_weight, mix_weight, coverage_floor_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scenario.name, *values),
        )
        return int(cursor.lastrowid or 0)

    def _insert_staff(
        self,
        person: Staff,
        role_ids: dict[str, int],
        service_ids: dict[str, int],
        room_ids: dict[str, int],
    ) -> None:
        conn = self.conn
        staff_id = conn.execute(
            "INSERT INTO staff (code, name, role_id, active) VALUES (?, ?, ?, ?)",
            (person.code, person.name, role_ids[person.role_code], int(person.active)),
        ).lastrowid
        for contract in person.contracts:
            conn.execute(
                "INSERT INTO contract (staff_id, valid_from, valid_to, weekly_minutes) VALUES (?, ?, ?, ?)",
                (
                    staff_id,
                    contract.valid_from.isoformat(),
                    contract.valid_to.isoformat() if contract.valid_to else None,
                    contract.weekly_minutes,
                ),
            )
        for window in person.availability:
            conn.execute(
                "INSERT INTO availability (staff_id, weekday, start_min, end_min) VALUES (?, ?, ?, ?)",
                (staff_id, window.weekday, window.start, window.end),
            )
        for code in sorted(person.skills):
            conn.execute("INSERT INTO staff_skill VALUES (?, ?)", (staff_id, service_ids[code]))
        for rule in person.room_rules:
            conn.execute(
                "INSERT INTO staff_room (staff_id, room_id, kind, rank) VALUES (?, ?, ?, ?)",
                (staff_id, room_ids[rule.room_code], rule.kind.value, rule.rank),
            )
        for target in person.targets:
            conn.execute(
                "INSERT INTO staff_target VALUES (?, ?, ?, ?)",
                (staff_id, service_ids[target.service_code], target.min_minutes, target.max_minutes),
            )


@dataclass(frozen=True)
class RunRecord:
    """Datos de una corrida listos para guardar."""

    created_at: datetime
    instance: PlanningInstance
    scenario_id: int | None
    status: str
    candidates: int
    variables: int
    phase_a: tuple[str, int | None, int | None, float]
    phase_b: tuple[str | None, int | None, float | None]
    greedy_value: int
    greedy_covered_sessions: int
    covered_sessions: int
    build_seconds: float
    total_seconds: float
    time_limit_s: float
    room_utilization: float | None
    plan: Plan
    unmet: tuple[UnmetDemand, ...]
    exclusions: tuple[RuleExclusion, ...]


class RunRepository:
    """Historial de corridas con su plan, turnos sin cubrir y exclusiones por regla."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, record: RunRecord) -> int:
        instance = record.instance
        phase_a_status, phase_a_value, phase_a_bound, phase_a_seconds = record.phase_a
        phase_b_status, phase_b_value, phase_b_seconds = record.phase_b
        run_id = self.conn.execute(
            "INSERT INTO run (created_at, week_start, scenario_id, scenario_name, status, time_limit_s, seed, workers, "
            "demand_sessions, covered_sessions, greedy_covered_sessions, phase_a_status, phase_a_value, "
            "phase_a_bound, phase_a_seconds, phase_b_status, phase_b_value, phase_b_seconds, greedy_value, "
            "candidates, variables, build_seconds, total_seconds, session_minutes, room_utilization, instance_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.created_at.isoformat(timespec="seconds"),
                instance.week_start.isoformat(),
                record.scenario_id,
                instance.scenario.name,
                record.status,
                record.time_limit_s,
                instance.settings.seed,
                instance.settings.workers,
                instance.total_demand,
                record.covered_sessions,
                record.greedy_covered_sessions,
                phase_a_status,
                phase_a_value,
                phase_a_bound,
                phase_a_seconds,
                phase_b_status,
                phase_b_value,
                phase_b_seconds,
                record.greedy_value,
                record.candidates,
                record.variables,
                record.build_seconds,
                record.total_seconds,
                record.plan.session_minutes(),
                record.room_utilization,
                codec.dumps(instance),
            ),
        ).lastrowid
        assert run_id is not None
        for session in record.plan.sessions:
            self.conn.execute(
                "INSERT INTO assignment (run_id, kind, staff_code, service_code, room_code, weekday, start_min, "
                "duration_min, participants) VALUES (?, 'sesion', ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    session.staff,
                    session.service,
                    session.room,
                    session.day,
                    session.start,
                    session.duration,
                    session.participants,
                ),
            )
        admin_room = instance.admin_room.code if instance.admin_room else None
        for block in record.plan.admin:
            self.conn.execute(
                "INSERT INTO assignment (run_id, kind, staff_code, service_code, room_code, weekday, start_min, "
                "duration_min, participants) VALUES (?, 'administrativo', ?, NULL, ?, ?, ?, ?, 0)",
                (run_id, block.staff, admin_room, block.day, block.start, block.duration),
            )
        for unmet in record.unmet:
            self.conn.execute(
                "INSERT INTO unmet_demand (run_id, weekday, block_start_min, service_code, required, covered, cause, "
                "rule_code, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    unmet.day,
                    unmet.block,
                    unmet.service,
                    unmet.required,
                    unmet.covered,
                    unmet.cause.value,
                    unmet.rule_code,
                    unmet.detail,
                ),
            )
        for exclusion in record.exclusions:
            self.conn.execute(
                "INSERT INTO rule_exclusion (run_id, position, rule_code, excluded) VALUES (?, ?, ?, ?)",
                (run_id, exclusion.position, exclusion.code, exclusion.excluded),
            )
        return int(run_id)

    def list(self, week_start: date | None = None) -> tuple[RunSummary, ...]:
        query = "SELECT * FROM run"
        params: tuple[str, ...] = ()
        if week_start is not None:
            query += " WHERE week_start = ?"
            params = (week_start.isoformat(),)
        rows = self.conn.execute(query + " ORDER BY id DESC", params).fetchall()
        return tuple(self._summary(row) for row in rows)

    def summary(self, run_id: int) -> RunSummary:
        row = self.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise DataError(f"No existe la corrida {run_id}.")
        return self._summary(row)

    def load(
        self, run_id: int
    ) -> tuple[RunSummary, PlanningInstance, Plan, tuple[UnmetDemand, ...], tuple[tuple[int, str, int], ...]]:
        row = self.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise DataError(f"No existe la corrida {run_id}.")
        instance = codec.loads(PlanningInstance, row["instance_json"])
        sessions: list[Session] = []
        admin: list[AdminBlock] = []
        for item in self.conn.execute("SELECT * FROM assignment WHERE run_id = ? ORDER BY id", (run_id,)):
            end = item["start_min"] + item["duration_min"]
            if item["kind"] == "sesion":
                sessions.append(
                    Session(
                        staff=item["staff_code"],
                        service=item["service_code"],
                        room=item["room_code"],
                        day=item["weekday"],
                        start=item["start_min"],
                        duration=item["duration_min"],
                        participants=item["participants"],
                    )
                )
            else:
                admin.append(AdminBlock(item["staff_code"], item["weekday"], item["start_min"], end))
        unmet = tuple(
            UnmetDemand(
                day=item["weekday"],
                block=item["block_start_min"],
                service=item["service_code"],
                required=item["required"],
                covered=item["covered"],
                cause=UnmetCause(item["cause"]),
                detail=item["detail"],
                rule_code=item["rule_code"],
            )
            for item in self.conn.execute(
                "SELECT * FROM unmet_demand WHERE run_id = ? ORDER BY weekday, block_start_min, service_code",
                (run_id,),
            )
        )
        exclusions = tuple(
            (item["position"], item["rule_code"], item["excluded"])
            for item in self.conn.execute("SELECT * FROM rule_exclusion WHERE run_id = ? ORDER BY position", (run_id,))
        )
        return self._summary(row), instance, Plan(tuple(sessions), tuple(admin)), unmet, exclusions

    def delete(self, run_id: int) -> None:
        self.conn.execute("DELETE FROM run WHERE id = ?", (run_id,))

    @staticmethod
    def _summary(row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            week_start=date.fromisoformat(row["week_start"]),
            scenario_name=row["scenario_name"],
            status=row["status"],
            time_limit_s=row["time_limit_s"],
            seed=row["seed"],
            workers=row["workers"],
            demand_sessions=row["demand_sessions"],
            covered_sessions=row["covered_sessions"],
            greedy_covered_sessions=row["greedy_covered_sessions"],
            phase_a_status=row["phase_a_status"],
            phase_a_value=row["phase_a_value"],
            phase_a_bound=row["phase_a_bound"],
            phase_a_seconds=row["phase_a_seconds"],
            phase_b_status=row["phase_b_status"],
            phase_b_value=row["phase_b_value"],
            phase_b_seconds=row["phase_b_seconds"],
            greedy_value=row["greedy_value"],
            candidates=row["candidates"],
            variables=row["variables"],
            build_seconds=row["build_seconds"],
            total_seconds=row["total_seconds"],
            session_minutes=row["session_minutes"],
            room_utilization=row["room_utilization"],
        )
