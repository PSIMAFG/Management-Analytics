"""Instancia de planificación de una semana: datos maestros resueltos para lunes a viernes.

La instancia precalcula, por persona y día, máscaras de slots (enteros usados
como conjuntos de bits) con la disponibilidad, las ausencias aprobadas y los
feriados, y los bloqueos que le aplican. Las reglas, el optimizador, el
validador y las métricas trabajan sobre estas máscaras.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import cached_property

from optibox.domain.models import (
    PRIORITY_WEIGHTS,
    Absence,
    Audience,
    Blocking,
    Contract,
    DemandItem,
    MasterData,
    PlanningSettings,
    Role,
    Room,
    Scenario,
    ServiceType,
    Staff,
)
from optibox.domain.timegrid import (
    DayHours,
    mask_minutes,
    slot_mask,
    slot_mask_outward,
    week_dates,
    week_monday,
)
from optibox.errors import ValidationError


@dataclass(frozen=True)
class PlanningDay:
    """Día hábil de la semana planificada."""

    index: int
    date: date
    hours: DayHours
    holiday: str | None
    closure_mask: int

    @property
    def is_open(self) -> bool:
        return self.holiday is None

    @property
    def open_mask(self) -> int:
        """Slots abiertos del centro (vacío si es feriado)."""
        return self.hours.open_mask if self.is_open else 0

    @property
    def room_capacity_minutes(self) -> int:
        """Minutos en que una sala puede usarse: horario abierto menos cierres y preparación del centro."""
        return mask_minutes(self.open_mask & ~self.closure_mask)


@dataclass(frozen=True)
class StaffDay:
    """Máscaras de slots de una persona en un día.

    `available` son las ventanas propias de la persona (pueden incluir el
    almuerzo); `open` es el horario abierto del centro ese día. Un día que
    ningún contrato cubre (`in_contract` falso) no tiene disponibilidad.
    """

    available: int
    open: int
    absent: int
    blocked: int
    blocked_admin: int
    in_contract: bool = True

    @property
    def workable(self) -> int:
        """Slots en que la persona puede recibir sesiones o trabajo administrativo."""
        return self.available & self.open & ~self.absent & ~self.blocked

    @property
    def present(self) -> int:
        """Slots disponibles y sin ausencia (incluye los bloqueados, que son tiempo de trabajo)."""
        return self.available & self.open & ~self.absent


@dataclass(frozen=True)
class StaffWeek:
    """Persona con su contrato de la semana y sus máscaras diarias.

    `contract` es el contrato de referencia (el que cubre más días hábiles) y
    `prorated_minutes` los minutos de contrato de la semana, prorrateados por
    día hábil cuando la vigencia empieza, termina o cambia a mitad de semana.
    Las instancias guardadas antes de existir el prorrateo no lo traen: en
    ese caso se usan los minutos completos del contrato de referencia.
    """

    staff: Staff
    role: Role
    contract: Contract | None
    days: tuple[StaffDay, ...]
    prorated_minutes: int | None = None

    @property
    def code(self) -> str:
        return self.staff.code

    @property
    def delivers_services(self) -> bool:
        return self.role.delivers_services

    @property
    def contract_minutes(self) -> int:
        """Minutos de contrato disponibles en la semana (sesiones, administrativo y bloqueos)."""
        if self.prorated_minutes is not None:
            return self.prorated_minutes
        return self.contract.weekly_minutes if self.contract is not None else 0

    @property
    def present_minutes(self) -> int:
        """Minutos en que la persona está presente: disponible, sin ausencias aprobadas ni feriados."""
        return sum(mask_minutes(day.present) for day in self.days)

    def blocking_minutes(self, day: int | None = None) -> int:
        """Minutos de bloqueos dentro de la disponibilidad (tiempo de trabajo que consume contrato)."""
        days = range(len(self.days)) if day is None else (day,)
        return sum(mask_minutes(self.days[d].blocked & self.days[d].present) for d in days)

    def blocking_admin_minutes(self) -> int:
        return sum(mask_minutes(d.blocked_admin & d.present) for d in self.days)

    @property
    def assignable_minutes(self) -> int:
        """Minutos de contrato que quedan para sesiones y administrativo después de los bloqueos."""
        return max(0, self.contract_minutes - self.blocking_minutes())


@dataclass(frozen=True)
class PlanningInstance:
    """Todo lo necesario para planificar una semana con un escenario."""

    week_start: date
    days: tuple[PlanningDay, ...]
    staff: tuple[StaffWeek, ...]
    service_types: tuple[ServiceType, ...]
    rooms: tuple[Room, ...]
    demand: tuple[DemandItem, ...]
    scenario: Scenario
    settings: PlanningSettings
    holiday_demand_dropped: int = 0
    blockings: tuple[Blocking, ...] = ()
    absences: tuple[Absence, ...] = ()

    @cached_property
    def staff_by_code(self) -> dict[str, StaffWeek]:
        return {sw.code: sw for sw in self.staff}

    @cached_property
    def service_by_code(self) -> dict[str, ServiceType]:
        return {service.code: service for service in self.service_types}

    @cached_property
    def room_by_code(self) -> dict[str, Room]:
        return {room.code: room for room in self.rooms}

    @cached_property
    def demand_by_key(self) -> dict[tuple[int, int, str], DemandItem]:
        return {item.key: item for item in self.demand}

    @cached_property
    def admin_room(self) -> Room | None:
        """Sala administrativa activa (la primera por código si hubiera varias)."""
        admin = sorted((r for r in self.rooms if r.is_admin_room and r.active), key=lambda r: r.code)
        return admin[0] if admin else None

    @property
    def total_demand(self) -> int:
        return sum(item.sessions for item in self.demand)

    @property
    def weighted_demand(self) -> int:
        """Demanda ponderada por prioridad (cota superior del valor de la fase A)."""
        return sum(PRIORITY_WEIGHTS[item.priority] * item.sessions for item in self.demand)

    def participants(self, service: ServiceType, room: Room) -> int:
        """Cupo efectivo de una sesión: el menor entre el cupo del tipo y el de la sala."""
        return min(service.participants, room.capacity)


def _center_hours(master: MasterData) -> dict[int, DayHours]:
    hours = {h.weekday: h for h in master.center_hours}
    missing = [d for d in range(5) if d not in hours]
    if missing:
        raise ValidationError("Falta el horario del centro para algún día de lunes a viernes.")
    return hours


def _staff_day(
    staff: Staff,
    role: Role,
    day: PlanningDay,
    blockings: tuple[Blocking, ...],
    absences: tuple[Absence, ...],
) -> StaffDay:
    in_contract = staff.contract_on(day.date) is not None
    available = 0
    for window in staff.availability:
        if in_contract and window.weekday == day.index:
            available |= slot_mask(window.start, window.end)
    open_mask = day.hours.open_mask
    absent = 0 if day.is_open else open_mask
    for absence in absences:
        if absence.staff_code != staff.code or absence.day != day.date or not absence.blocks:
            continue
        if absence.start is None or absence.end is None:
            absent |= open_mask
        else:
            absent |= slot_mask_outward(absence.start, absence.end)
    blocked = 0
    blocked_admin = 0
    for blocking in blockings:
        if blocking.applies_on(day.index) and blocking.applies_to(staff, role):
            mask = slot_mask_outward(blocking.start, blocking.end) & open_mask
            blocked |= mask
            if blocking.counts_as_admin:
                blocked_admin |= mask
    return StaffDay(
        available=available,
        open=open_mask,
        absent=absent & available & open_mask,
        blocked=blocked,
        blocked_admin=blocked_admin,
        in_contract=in_contract,
    )


def build_instance(
    master: MasterData,
    week_start: date,
    scenario: Scenario,
    settings: PlanningSettings | None = None,
) -> PlanningInstance:
    """Arma la instancia de la semana (lunes a viernes) que contiene `week_start`."""
    monday = week_monday(week_start)
    dates = week_dates(monday)
    hours = _center_hours(master)
    holidays = {h.day: h.name for h in master.holidays}
    closures = tuple(b for b in master.blockings if b.audience is Audience.ALL)
    days: list[PlanningDay] = []
    for index, day_date in enumerate(dates):
        day_hours = hours[index]
        closure = 0
        for blocking in closures:
            if blocking.applies_on(index):
                closure |= slot_mask_outward(blocking.start, blocking.end)
        days.append(
            PlanningDay(
                index=index,
                date=day_date,
                hours=day_hours,
                holiday=holidays.get(day_date),
                closure_mask=closure & day_hours.open_mask,
            )
        )
    planning_days = tuple(days)

    roles = {role.code: role for role in master.roles}
    staff_weeks: list[StaffWeek] = []
    for staff in sorted(master.staff, key=lambda s: s.code):
        if not staff.active:
            continue
        role = roles.get(staff.role_code)
        if role is None:
            raise ValidationError(f"La persona {staff.code} tiene un cargo desconocido ({staff.role_code}).")
        staff_weeks.append(
            StaffWeek(
                staff=staff,
                role=role,
                contract=staff.contract_for_week(dates),
                days=tuple(_staff_day(staff, role, day, master.blockings, master.absences) for day in planning_days),
                prorated_minutes=staff.week_contract_minutes(dates),
            )
        )

    services = tuple(sorted(master.service_types, key=lambda s: s.code))
    rooms = tuple(sorted(master.rooms, key=lambda r: r.code))
    needs_admin = any(service.admin_minutes > 0 for service in services)
    if needs_admin and not any(room.is_admin_room and room.active for room in rooms):
        raise ValidationError(
            "No hay una sala administrativa activa y las atenciones exigen trabajo administrativo. "
            "Active o agregue una sala de tipo administrativa en los datos maestros."
        )
    service_codes = {service.code for service in services}
    demand: list[DemandItem] = []
    dropped = 0
    for item in master.demand:
        if item.service_code not in service_codes:
            raise ValidationError(f"La demanda usa un tipo de atención desconocido ({item.service_code}).")
        day = planning_days[item.weekday]
        if item.sessions == 0 or item.block_start not in day.hours.blocks():
            continue
        if not day.is_open:
            dropped += item.sessions
            continue
        demand.append(item)
    demand.sort(key=lambda item: item.key)
    week_absences = tuple(
        sorted(
            (a for a in master.absences if a.day in dates),
            key=lambda a: (a.day, a.staff_code, a.start or 0),
        )
    )

    return PlanningInstance(
        week_start=monday,
        days=planning_days,
        staff=tuple(staff_weeks),
        service_types=services,
        rooms=rooms,
        demand=tuple(demand),
        scenario=scenario,
        settings=settings or master.settings,
        holiday_demand_dropped=dropped,
        blockings=master.blockings,
        absences=week_absences,
    )
