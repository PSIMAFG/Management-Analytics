"""Fábricas de instancias pequeñas y verificables a mano para los tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from optibox.domain.instance import PlanningInstance, build_instance
from optibox.domain.models import (
    Audience,
    AvailabilityWindow,
    Blocking,
    Contract,
    DemandItem,
    MasterData,
    PlanningSettings,
    Role,
    Room,
    RoomKind,
    Scenario,
    ServiceType,
    Staff,
)
from optibox.domain.optimizer import OptimizationResult, optimize
from optibox.domain.plan import AdminBlock, Candidate, Session
from optibox.domain.rules import generate_candidates
from optibox.domain.timegrid import DEFAULT_HOURS

MONDAY = date(2026, 10, 5)
REFERENCE = date(2026, 9, 30)

FULL_WEEK = tuple(AvailabilityWindow(d, 480, 1020 if d < 4 else 960) for d in range(5))


@dataclass(frozen=True)
class PlannedDatabase:
    """Base sintética con una corrida determinista ya guardada (solo lectura en los tests)."""

    path: Path
    run_id: int


def service(code: str, **overrides: object) -> ServiceType:
    """Tipo de atención individual de 45 minutos con 15 minutos administrativos."""
    base = ServiceType(
        code=code,
        name=f"Atención {code}",
        duration_min=45,
        participants=1,
        requires_group_room=False,
        requires_evaluation_room=False,
        admin_minutes=15,
        priority=2,
        color="#1F4E79",
        rooms=frozenset({"R1", "R2"}),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def person(code: str, role: str = "CL", hours: int = 44, **overrides: object) -> Staff:
    base = Staff(
        code=code,
        name=f"Persona {code}",
        role_code=role,
        contracts=(Contract(date(2026, 1, 1), None, hours * 60),),
        availability=FULL_WEEK,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def contract_minutes(minutes: int) -> tuple[Contract, ...]:
    """Contrato indefinido de pocos minutos semanales (para forzar el tope de contrato)."""
    return (Contract(date(2026, 1, 1), None, minutes),)


def small_master(**overrides: object) -> MasterData:
    """Centro mínimo: dos cargos, cuatro tipos de atención, dos salas de atención y una administrativa.

    - IND y FAM: 45 min, 15 min administrativos, salas R1 y R2.
    - EVA: 120 min, 60 min administrativos, prioridad 1, solo R2 (admite evaluación).
    - GRP: 90 min, cupo 6, 30 min administrativos, solo R2 (admite grupos, cupo 4), tope semanal 2.
    - A (44 h) y B (22 h) son asistenciales; C es de apoyo administrativo.
    """
    services = (
        service("IND"),
        service("FAM"),
        service(
            "EVA",
            duration_min=120,
            admin_minutes=60,
            requires_evaluation_room=True,
            rooms=frozenset({"R2"}),
            priority=1,
        ),
        service(
            "GRP",
            duration_min=90,
            participants=6,
            admin_minutes=30,
            requires_group_room=True,
            rooms=frozenset({"R2"}),
            priority=3,
            max_week_total=2,
        ),
    )
    master = MasterData(
        roles=(
            Role("CL", "Clínico", True, frozenset({"IND", "FAM", "EVA", "GRP"})),
            Role("AD", "Apoyo administrativo", False),
        ),
        service_types=services,
        rooms=(
            Room("R1", "Box 1", RoomKind.BOX, False, False, 1),
            Room("R2", "Sala de usos múltiples", RoomKind.MULTIPURPOSE, True, True, 4),
            Room(
                "ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, simultaneous_hard=2, simultaneous_soft=1
            ),
        ),
        staff=(person("A"), person("B", hours=22), person("C", role="AD")),
        blockings=(),
        absences=(),
        holidays=(),
        center_hours=DEFAULT_HOURS,
        demand=(DemandItem(0, 540, "IND", 2, 2),),
        scenarios=(Scenario(name="Base", description="Escenario de prueba", id=1),),
        settings=PlanningSettings(time_limit_s=10, workers=4),
    )
    return replace(master, **overrides)  # type: ignore[arg-type]


def make_instance(master: MasterData | None = None, scenario: Scenario | None = None) -> PlanningInstance:
    master = master or small_master()
    return build_instance(master, MONDAY, scenario or master.scenarios[0], master.settings)


def meeting(weekday: int, start: int, end: int, audience: Audience, target: str | None = None) -> Blocking:
    return Blocking("Reunión", weekday, start, end, audience, target)


def candidate(
    staff: str = "A", service_code: str = "IND", room: str = "R1", day: int = 0, start: int = 540, duration: int = 45
) -> Candidate:
    return Candidate(staff, service_code, room, day, start, duration)


def session(
    instance: PlanningInstance,
    staff: str = "A",
    service_code: str = "IND",
    room: str = "R1",
    day: int = 0,
    start: int = 540,
) -> Session:
    """Sesión con la duración del tipo y el cupo efectivo de la sala."""
    kind = instance.service_by_code[service_code]
    participants = instance.participants(kind, instance.room_by_code[room])
    return Session(staff, service_code, room, day, start, kind.duration_min, participants)


def admin(staff: str, day: int, start: int, end: int) -> AdminBlock:
    return AdminBlock(staff, day, start, end)


def solve(instance: PlanningInstance, limit: float = 1.0) -> OptimizationResult:
    """Etapa 1 y etapa 2 en modo determinista (resultado reproducible)."""
    return optimize(instance, generate_candidates(instance), time_limit_s=limit, deterministic=True)
