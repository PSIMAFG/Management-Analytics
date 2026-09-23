"""Métricas del plan con definiciones explícitas, calculadas siempre desde las sesiones.

Definiciones:

- Cobertura de un (día, bloque, tipo): sesiones que comienzan en el bloque
  sobre sesiones requeridas. No cubierto = max(0, requerido - cubierto).
- Cobertura ponderada: igual, pero cada sesión pesa según la prioridad de su
  demanda (alta 4, media 2, baja 1).
- Atenciones: suma del cupo efectivo de cada sesión (una sesión individual es
  una atención; un taller en grupo entrega tantas atenciones como su cupo).
- Carga por persona: minutos de atención, administrativos (tramos asignados
  más bloqueos administrativos), de reuniones y libres frente al contrato.
  En el personal no asistencial, las tareas no asistenciales son el tiempo
  presente dentro del contrato (sin ausencias ni feriados) menos sus bloqueos.
- Utilización de salas: minutos de sesión sobre minutos abiertos de la sala
  (horario del centro sin almuerzo, sin feriados y sin cierres del centro).
  La acumulada al día d es la suma de minutos de sesión hasta d sobre la suma
  de capacidad hasta d.
- Mezcla: participación de cada tipo en los minutos de sesión frente a las
  cotas mínima y máxima del escenario; el desvío en minutos es la holgura que
  la fase B penaliza.
- Rendimiento de horas de una persona: la jornada programada es su
  disponibilidad dentro del horario abierto del centro, sin almuerzo ni
  feriados. El tiempo contratado disponible es el menor entre la jornada
  programada y el contrato (el exceso de disponibilidad sobre el contrato no
  es ocioso). Los permisos (ausencias aprobadas) se descuentan aparte, no son
  ociosos ni productivos. Productivas = atención + administrativo + reuniones;
  productivas clínicas = solo atención. Ociosas = tiempo contratado disponible
  menos permisos menos productivas, nunca negativo. El personal no asistencial
  no tiene tareas registradas a nivel de sesión: su tiempo presente se cuenta
  como administrativo continuo, igual que en la carga por persona.
- Rendimiento de horas de una sala: horas ociosas = horas abiertas (mismo
  criterio que la utilización) menos horas ocupadas por atenciones.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from optibox.domain.instance import PlanningInstance
from optibox.domain.models import PRIORITY_WEIGHTS
from optibox.domain.plan import Plan, admin_slots_per_day
from optibox.domain.timegrid import block_of, mask_minutes


def ratio(numerator: float, denominator: float) -> float | None:
    """Cociente o None si el denominador es cero (se muestra como guion)."""
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class CoverageRow:
    """Cobertura de un (día, bloque) o de un (día, bloque, tipo) si `service` no es None."""

    day: int
    block: int
    service: str | None
    required: int
    covered: int

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.covered)

    @property
    def pct(self) -> float | None:
        return ratio(min(self.covered, self.required), self.required)


@dataclass(frozen=True)
class ServiceSummary:
    code: str
    name: str
    required: int
    covered: int
    sessions: int
    attentions: int
    minutes: int

    @property
    def pct(self) -> float | None:
        return ratio(min(self.covered, self.required), self.required)


@dataclass(frozen=True)
class StaffLoad:
    """Carga semanal de una persona en minutos."""

    code: str
    name: str
    role: str
    delivers_services: bool
    contract_min: int
    session_min: int
    admin_min: int
    admin_required_min: int
    meeting_min: int
    non_service_min: int
    sessions: int
    attentions: int

    @property
    def assigned_min(self) -> int:
        return self.session_min + self.admin_min + self.meeting_min + self.non_service_min

    @property
    def free_min(self) -> int:
        return max(0, self.contract_min - self.assigned_min)

    @property
    def usage_pct(self) -> float | None:
        return ratio(self.assigned_min, self.contract_min)


@dataclass(frozen=True)
class RoomUse:
    """Uso semanal de una sala de atención."""

    code: str
    name: str
    sessions: int
    session_min: int
    capacity_min: int

    @property
    def pct(self) -> float | None:
        return ratio(self.session_min, self.capacity_min)

    @property
    def idle_min(self) -> int:
        return max(0, self.capacity_min - self.session_min)


@dataclass(frozen=True)
class RoomDayUse:
    room: str
    day: int
    session_min: int
    capacity_min: int

    @property
    def pct(self) -> float | None:
        return ratio(self.session_min, self.capacity_min)

    @property
    def idle_min(self) -> int:
        return max(0, self.capacity_min - self.session_min)


@dataclass(frozen=True)
class StaffHours:
    """Rendimiento de horas de una persona en la semana (ver definiciones del módulo)."""

    code: str
    name: str
    role: str
    delivers_services: bool
    scheduled_min: int
    contract_min: int
    contracted_min: int
    leave_min: int
    session_min: int
    admin_min: int
    meeting_min: int
    idle_min: int

    @property
    def productive_min(self) -> int:
        return self.session_min + self.admin_min + self.meeting_min

    @property
    def available_min(self) -> int:
        """Tiempo contratado disponible menos permisos: base de los porcentajes."""
        return max(0, self.contracted_min - self.leave_min)

    @property
    def productive_pct(self) -> float | None:
        return ratio(self.productive_min, self.available_min)

    @property
    def clinical_pct(self) -> float | None:
        return ratio(self.session_min, self.available_min)

    @property
    def idle_pct(self) -> float | None:
        return ratio(self.idle_min, self.available_min)


@dataclass(frozen=True)
class DayUtilization:
    """Utilización diaria de todas las salas de atención y su acumulado en la semana."""

    day: int
    session_min: int
    capacity_min: int
    cumulative_session_min: int
    cumulative_capacity_min: int

    @property
    def pct(self) -> float | None:
        return ratio(self.session_min, self.capacity_min)

    @property
    def cumulative_pct(self) -> float | None:
        return ratio(self.cumulative_session_min, self.cumulative_capacity_min)


@dataclass(frozen=True)
class AdminRoomSlot:
    day: int
    start: int
    people: int
    soft_capacity: int
    hard_capacity: int


@dataclass(frozen=True)
class PlanTotals:
    """Totales de la semana.

    `admin_min` son los tramos administrativos que asigna el plan y
    `blocking_admin_min` los bloqueos que cuentan como administrativo; la
    columna administrativa de la carga por persona suma ambos.
    """

    demand_sessions: int
    covered_sessions: int
    unmet_sessions: int
    unmet_items: int
    weighted_demand: int
    weighted_covered: int
    sessions: int
    attentions: int
    session_min: int
    admin_min: int
    admin_required_min: int
    blocking_admin_min: int
    room_session_min: int
    room_capacity_min: int
    room_switches: int
    staff_idle_min: int = 0

    @property
    def room_idle_min(self) -> int:
        return max(0, self.room_capacity_min - self.room_session_min)

    @property
    def coverage_pct(self) -> float | None:
        return ratio(self.covered_sessions, self.demand_sessions)

    @property
    def weighted_coverage_pct(self) -> float | None:
        return ratio(self.weighted_covered, self.weighted_demand)

    @property
    def room_utilization_pct(self) -> float | None:
        return ratio(self.room_session_min, self.room_capacity_min)

    @property
    def assigned_hours(self) -> float:
        """Horas de atención asignadas (solo sesiones)."""
        return self.session_min / 60


@dataclass(frozen=True)
class MixRow:
    """Participación de un tipo de atención en los minutos de sesión frente a las cotas del escenario."""

    service: str
    name: str
    minutes: int
    total_minutes: int
    min_share: float | None
    max_share: float | None

    @property
    def share(self) -> float | None:
        return ratio(self.minutes, self.total_minutes)

    @property
    def deviation_min(self) -> int:
        """Minutos por debajo del mínimo o por encima del máximo (0 si está dentro de la banda)."""
        low = (self.min_share or 0.0) * self.total_minutes
        high = (1.0 if self.max_share is None else self.max_share) * self.total_minutes
        return round(max(0.0, low - self.minutes) + max(0.0, self.minutes - high))

    @property
    def within_band(self) -> bool:
        return self.deviation_min == 0


@dataclass(frozen=True)
class PlanMetrics:
    totals: PlanTotals
    coverage_by_block: tuple[CoverageRow, ...]
    coverage_by_block_service: tuple[CoverageRow, ...]
    services: tuple[ServiceSummary, ...]
    staff_load: tuple[StaffLoad, ...]
    rooms: tuple[RoomUse, ...]
    room_days: tuple[RoomDayUse, ...]
    daily: tuple[DayUtilization, ...]
    admin_room: tuple[AdminRoomSlot, ...]
    mix: tuple[MixRow, ...] = ()
    staff_hours: tuple[StaffHours, ...] = ()


def compute_metrics(instance: PlanningInstance, plan: Plan) -> PlanMetrics:
    """Calcula todas las métricas del plan a partir de sus sesiones y tramos administrativos."""
    covered: Counter[tuple[int, int, str]] = Counter()
    for session in plan.sessions:
        covered[(session.day, block_of(session.start), session.service)] += 1

    block_service_rows: list[CoverageRow] = []
    block_totals: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    for item in instance.demand:
        got = min(covered[item.key], item.sessions)
        block_service_rows.append(CoverageRow(item.weekday, item.block_start, item.service_code, item.sessions, got))
        block_totals[(item.weekday, item.block_start)][0] += item.sessions
        block_totals[(item.weekday, item.block_start)][1] += got
    block_rows: list[CoverageRow] = []
    for day in instance.days:
        for block in day.hours.blocks():
            required, got = block_totals.get((day.index, block), [0, 0])
            block_rows.append(CoverageRow(day.index, block, None, required, got))

    services = _service_summaries(instance, plan, covered)
    staff_load = _staff_load(instance, plan)
    rooms, room_days, daily = _room_utilization(instance, plan)
    admin_room = _admin_room_load(instance, plan)
    staff_hours = _hours_performance(instance, plan)

    weighted_demand = instance.weighted_demand
    weighted_covered = sum(
        PRIORITY_WEIGHTS[item.priority] * min(covered[item.key], item.sessions) for item in instance.demand
    )
    demand_total = instance.total_demand
    covered_total = sum(row.covered for row in block_service_rows)
    unmet_rows = [row for row in block_service_rows if row.shortfall > 0]
    rooms_by_staff_day: dict[tuple[str, int], set[str]] = defaultdict(set)
    for session in plan.sessions:
        rooms_by_staff_day[(session.staff, session.day)].add(session.room)
    totals = PlanTotals(
        demand_sessions=demand_total,
        covered_sessions=covered_total,
        unmet_sessions=sum(row.shortfall for row in unmet_rows),
        unmet_items=len(unmet_rows),
        weighted_demand=weighted_demand,
        weighted_covered=weighted_covered,
        sessions=len(plan.sessions),
        attentions=plan.attentions,
        session_min=plan.session_minutes(),
        admin_min=sum(block.duration for block in plan.admin),
        admin_required_min=sum(instance.service_by_code[s.service].admin_minutes for s in plan.sessions),
        blocking_admin_min=sum(sw.blocking_admin_minutes() for sw in instance.staff if sw.contract is not None),
        room_session_min=sum(room.session_min for room in rooms),
        room_capacity_min=sum(room.capacity_min for room in rooms),
        room_switches=sum(len(used) - 1 for used in rooms_by_staff_day.values()),
        staff_idle_min=sum(sh.idle_min for sh in staff_hours),
    )
    return PlanMetrics(
        totals=totals,
        coverage_by_block=tuple(block_rows),
        coverage_by_block_service=tuple(block_service_rows),
        services=services,
        staff_load=staff_load,
        rooms=rooms,
        room_days=room_days,
        daily=daily,
        admin_room=admin_room,
        mix=_mix(instance, plan),
        staff_hours=staff_hours,
    )


def _mix(instance: PlanningInstance, plan: Plan) -> tuple[MixRow, ...]:
    scenario = instance.scenario
    total = plan.session_minutes()
    rows: list[MixRow] = []
    for code in sorted(set(scenario.mix_min) | set(scenario.mix_max)):
        service = instance.service_by_code.get(code)
        if service is None:
            continue
        minutes = sum(s.duration for s in plan.sessions if s.service == code)
        rows.append(MixRow(code, service.name, minutes, total, scenario.mix_min.get(code), scenario.mix_max.get(code)))
    return tuple(rows)


def _service_summaries(
    instance: PlanningInstance, plan: Plan, covered: Counter[tuple[int, int, str]]
) -> tuple[ServiceSummary, ...]:
    required: Counter[str] = Counter()
    got: Counter[str] = Counter()
    for item in instance.demand:
        required[item.service_code] += item.sessions
        got[item.service_code] += min(covered[item.key], item.sessions)
    sessions: Counter[str] = Counter()
    attentions: Counter[str] = Counter()
    minutes: Counter[str] = Counter()
    for session in plan.sessions:
        sessions[session.service] += 1
        attentions[session.service] += session.participants
        minutes[session.service] += session.duration
    return tuple(
        ServiceSummary(
            code=service.code,
            name=service.name,
            required=required[service.code],
            covered=got[service.code],
            sessions=sessions[service.code],
            attentions=attentions[service.code],
            minutes=minutes[service.code],
        )
        for service in instance.service_types
    )


def _staff_load(instance: PlanningInstance, plan: Plan) -> tuple[StaffLoad, ...]:
    loads: list[StaffLoad] = []
    for staff in instance.staff:
        if staff.contract is None:
            continue
        own = [s for s in plan.sessions if s.staff == staff.code]
        blocking = staff.blocking_minutes()
        blocking_admin = staff.blocking_admin_minutes()
        admin_placed = plan.admin_minutes(staff.code)
        non_service = 0
        if not staff.delivers_services:
            # Solo el tiempo en que la persona está presente (sin ausencias ni feriados) puede ser trabajo.
            non_service = max(0, min(staff.contract_minutes, staff.present_minutes) - blocking)
        loads.append(
            StaffLoad(
                code=staff.code,
                name=staff.staff.name,
                role=staff.role.name,
                delivers_services=staff.delivers_services,
                contract_min=staff.contract_minutes,
                session_min=sum(s.duration for s in own),
                admin_min=admin_placed + blocking_admin,
                admin_required_min=sum(instance.service_by_code[s.service].admin_minutes for s in own),
                meeting_min=blocking - blocking_admin,
                non_service_min=non_service,
                sessions=len(own),
                attentions=sum(s.participants for s in own),
            )
        )
    return tuple(loads)


def _hours_performance(instance: PlanningInstance, plan: Plan) -> tuple[StaffHours, ...]:
    rows: list[StaffHours] = []
    for staff in instance.staff:
        if staff.contract is None:
            continue
        scheduled = sum(
            mask_minutes(day.available & planning_day.open_mask)
            for day, planning_day in zip(staff.days, instance.days, strict=True)
        )
        leave = scheduled - staff.present_minutes
        contracted = min(scheduled, staff.contract_minutes)
        own = [s for s in plan.sessions if s.staff == staff.code]
        session_min = sum(s.duration for s in own)
        blocking = staff.blocking_minutes()
        blocking_admin = staff.blocking_admin_minutes()
        admin_min = plan.admin_minutes(staff.code) + blocking_admin
        if not staff.delivers_services:
            admin_min += max(0, min(staff.contract_minutes, staff.present_minutes) - blocking)
        meeting_min = blocking - blocking_admin
        productive = session_min + admin_min + meeting_min
        idle = max(0, contracted - leave - productive)
        rows.append(
            StaffHours(
                code=staff.code,
                name=staff.staff.name,
                role=staff.role.name,
                delivers_services=staff.delivers_services,
                scheduled_min=scheduled,
                contract_min=staff.contract_minutes,
                contracted_min=contracted,
                leave_min=leave,
                session_min=session_min,
                admin_min=admin_min,
                meeting_min=meeting_min,
                idle_min=idle,
            )
        )
    return tuple(rows)


def _room_utilization(
    instance: PlanningInstance, plan: Plan
) -> tuple[tuple[RoomUse, ...], tuple[RoomDayUse, ...], tuple[DayUtilization, ...]]:
    clinical = [room for room in instance.rooms if room.active and not room.is_admin_room]
    minutes: Counter[tuple[str, int]] = Counter()
    count: Counter[str] = Counter()
    for session in plan.sessions:
        minutes[(session.room, session.day)] += session.duration
        count[session.room] += 1
    room_days: list[RoomDayUse] = []
    rooms: list[RoomUse] = []
    for room in clinical:
        week_minutes = 0
        week_capacity = 0
        for day in instance.days:
            capacity = day.room_capacity_minutes
            used = minutes[(room.code, day.index)]
            room_days.append(RoomDayUse(room.code, day.index, used, capacity))
            week_minutes += used
            week_capacity += capacity
        rooms.append(RoomUse(room.code, room.name, count[room.code], week_minutes, week_capacity))
    daily: list[DayUtilization] = []
    cumulative_used = 0
    cumulative_capacity = 0
    for day in instance.days:
        used = sum(minutes[(room.code, day.index)] for room in clinical)
        capacity = day.room_capacity_minutes * len(clinical)
        cumulative_used += used
        cumulative_capacity += capacity
        daily.append(DayUtilization(day.index, used, capacity, cumulative_used, cumulative_capacity))
    return tuple(rooms), tuple(room_days), tuple(daily)


def _admin_room_load(instance: PlanningInstance, plan: Plan) -> tuple[AdminRoomSlot, ...]:
    room = instance.admin_room
    if room is None or room.simultaneous_hard is None:
        return ()
    slots: list[AdminRoomSlot] = []
    for day in instance.days:
        load = admin_slots_per_day(plan.admin, day.index)
        for start in day.hours.slot_starts():
            if day.is_open:
                slots.append(
                    AdminRoomSlot(day.index, start, load.get(start, 0), room.soft_capacity, room.simultaneous_hard)
                )
    return tuple(slots)
