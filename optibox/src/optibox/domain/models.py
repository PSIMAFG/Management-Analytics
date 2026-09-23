"""Modelos del dominio: datos maestros inmutables y validados al construirse."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import TypeVar

from optibox.domain.timegrid import (
    WEEK_DAYS,
    WEEKDAY_NAMES,
    DayHours,
    format_range,
    require_aligned,
    require_range,
)
from optibox.errors import ValidationError


class RoomKind(StrEnum):
    """Tipo de sala. La sala administrativa aloja el trabajo administrativo."""

    BOX = "box"
    MULTIPURPOSE = "multiuso"
    EVALUATION = "evaluacion"
    GYM = "gimnasio"
    GROUP = "grupal"
    PSYCHOSOCIAL = "psicosocial"
    ADMIN = "administrativa"


class RoomRuleKind(StrEnum):
    """Regla de una persona sobre una sala."""

    ALLOWED = "permitida"
    FORBIDDEN = "prohibida"
    PREFERRED = "preferida"


class Audience(StrEnum):
    """Destinatario de un bloqueo, resuelto por atributos y no por identificadores fijos."""

    ALL = "todos"
    SERVICE_STAFF = "asistencial"
    ROLE = "cargo"
    STAFF = "persona"


class AbsenceStatus(StrEnum):
    APPROVED = "aprobada"
    PENDING = "pendiente"
    REJECTED = "rechazada"


K = TypeVar("K")
V = TypeVar("V")


def frozen_mapping(values: Mapping[K, V]) -> Mapping[K, V]:
    """Copia de solo lectura de un diccionario, para que los modelos inmutables no se puedan modificar."""
    return MappingProxyType(dict(values))


PRIORITY_LABELS = {1: "Alta", 2: "Media", 3: "Baja"}
# Peso de cada sesión cubierta según la prioridad de su demanda (fase A y cobertura ponderada).
PRIORITY_WEIGHTS = {1: 4, 2: 2, 3: 1}


def _require_code(code: str, what: str) -> None:
    if not code or not code.strip() or code != code.strip():
        raise ValidationError(f"El código de {what} no puede estar vacío ni tener espacios al inicio o al final.")


def _require_priority(priority: int, what: str) -> None:
    if priority not in PRIORITY_LABELS:
        raise ValidationError(f"La prioridad de {what} debe ser 1 (alta), 2 (media) o 3 (baja).")


def _require_optional_cap(value: int | None, what: str) -> None:
    if value is not None and value < 0:
        raise ValidationError(f"El tope {what} no puede ser negativo.")


@dataclass(frozen=True)
class Role:
    """Cargo. Solo los cargos asistenciales reciben sesiones de atención."""

    code: str
    name: str
    delivers_services: bool
    services: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _require_code(self.code, "cargo")
        if not self.delivers_services and self.services:
            raise ValidationError(f"El cargo {self.name} no es asistencial y no puede tener tipos de atención.")


@dataclass(frozen=True)
class ServiceType:
    """Tipo de atención con su duración, cupo, requisitos de sala, administrativo asociado y topes."""

    code: str
    name: str
    duration_min: int
    participants: int
    requires_group_room: bool
    requires_evaluation_room: bool
    admin_minutes: int
    priority: int
    color: str
    rooms: frozenset[str] = frozenset()
    max_week_total: int | None = None
    max_week_per_staff: int | None = None
    max_day_total: int | None = None
    max_day_per_staff: int | None = None

    def __post_init__(self) -> None:
        _require_code(self.code, "tipo de atención")
        if self.duration_min <= 0:
            raise ValidationError(f"La duración de {self.name} debe ser positiva.")
        require_aligned(self.duration_min, f"duración de {self.name}")
        require_aligned(self.admin_minutes, f"minutos administrativos de {self.name}")
        if self.admin_minutes < 0:
            raise ValidationError(f"Los minutos administrativos de {self.name} no pueden ser negativos.")
        if self.participants < 1:
            raise ValidationError(f"El cupo de {self.name} debe ser al menos 1.")
        _require_priority(self.priority, self.name)
        for label, value in (
            ("semanal total", self.max_week_total),
            ("semanal por persona", self.max_week_per_staff),
            ("diario total", self.max_day_total),
            ("diario por persona", self.max_day_per_staff),
        ):
            _require_optional_cap(value, f"{label} de {self.name}")


@dataclass(frozen=True)
class Room:
    """Sala o box. La sala administrativa tiene capacidad simultánea dura y blanda."""

    code: str
    name: str
    kind: RoomKind
    allows_group: bool
    allows_evaluation: bool
    capacity: int
    active: bool = True
    reserved_role: str | None = None
    simultaneous_hard: int | None = None
    simultaneous_soft: int | None = None

    def __post_init__(self) -> None:
        _require_code(self.code, "sala")
        if self.capacity < 1:
            raise ValidationError(f"El cupo de la sala {self.name} debe ser al menos 1.")
        if self.kind is RoomKind.ADMIN:
            if self.simultaneous_hard is None or self.simultaneous_hard < 1:
                raise ValidationError(
                    f"La sala administrativa {self.name} necesita una capacidad simultánea dura positiva."
                )
            soft = self.simultaneous_soft if self.simultaneous_soft is not None else self.simultaneous_hard
            if not 1 <= soft <= self.simultaneous_hard:
                raise ValidationError(f"La capacidad blanda de {self.name} debe estar entre 1 y la capacidad dura.")

    @property
    def is_admin_room(self) -> bool:
        return self.kind is RoomKind.ADMIN

    @property
    def soft_capacity(self) -> int:
        if self.simultaneous_soft is not None:
            return self.simultaneous_soft
        return self.simultaneous_hard or 0


@dataclass(frozen=True)
class Contract:
    """Contrato versionado: vigencia inclusiva y minutos semanales."""

    valid_from: date
    valid_to: date | None
    weekly_minutes: int

    def __post_init__(self) -> None:
        if self.weekly_minutes <= 0:
            raise ValidationError("Los minutos semanales del contrato deben ser positivos.")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValidationError("El fin de vigencia del contrato no puede ser anterior a su inicio.")

    def covers(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)

    def overlaps(self, other: Contract) -> bool:
        end_self = self.valid_to or date.max
        end_other = other.valid_to or date.max
        return self.valid_from <= end_other and other.valid_from <= end_self


@dataclass(frozen=True)
class AvailabilityWindow:
    weekday: int
    start: int
    end: int

    def __post_init__(self) -> None:
        if not 0 <= self.weekday < WEEK_DAYS:
            raise ValidationError("El día de disponibilidad debe estar entre lunes y viernes.")
        require_range(self.start, self.end, f"disponibilidad del {WEEKDAY_NAMES[self.weekday]}")


@dataclass(frozen=True)
class RoomRule:
    room_code: str
    kind: RoomRuleKind
    rank: int | None = None

    def __post_init__(self) -> None:
        if self.kind is RoomRuleKind.PREFERRED and (self.rank is None or self.rank < 1):
            raise ValidationError("Una sala preferida necesita un ranking positivo (1 es la más preferida).")


@dataclass(frozen=True)
class ServiceTarget:
    """Meta blanda de minutos semanales por tipo de atención (banda mínimo-máximo)."""

    service_code: str
    min_minutes: int | None
    max_minutes: int | None

    def __post_init__(self) -> None:
        if self.min_minutes is None and self.max_minutes is None:
            raise ValidationError("Una meta necesita al menos un mínimo o un máximo.")
        if any(value is not None and value < 0 for value in (self.min_minutes, self.max_minutes)):
            raise ValidationError("Las metas de minutos no pueden ser negativas.")
        if self.min_minutes is not None and self.max_minutes is not None and self.min_minutes > self.max_minutes:
            raise ValidationError("El mínimo de una meta no puede superar su máximo.")


@dataclass(frozen=True)
class Staff:
    """Persona del equipo con sus contratos, disponibilidad, competencias y reglas de sala.

    `skills` es una lista blanca individual: vacía significa "todos los tipos de
    atención de su cargo" (sin restricción adicional).
    """

    code: str
    name: str
    role_code: str
    active: bool = True
    contracts: tuple[Contract, ...] = ()
    availability: tuple[AvailabilityWindow, ...] = ()
    skills: frozenset[str] = frozenset()
    room_rules: tuple[RoomRule, ...] = ()
    targets: tuple[ServiceTarget, ...] = ()

    def __post_init__(self) -> None:
        _require_code(self.code, "persona")
        ordered = sorted(self.contracts, key=lambda contract: contract.valid_from)
        for previous, current in pairwise(ordered):
            if previous.overlaps(current):
                raise ValidationError(f"Los contratos de {self.code} tienen vigencias que se solapan.")
        rooms = [rule.room_code for rule in self.room_rules]
        if len(rooms) != len(set(rooms)):
            raise ValidationError(f"{self.code} tiene más de una regla para la misma sala.")
        for weekday in range(WEEK_DAYS):
            windows = sorted((w.start, w.end) for w in self.availability if w.weekday == weekday)
            for (_, end), (start, _) in pairwise(windows):
                if start < end:
                    raise ValidationError(f"La disponibilidad de {self.code} se solapa el {WEEKDAY_NAMES[weekday]}.")

    def contract_on(self, day: date) -> Contract | None:
        """Contrato vigente en una fecha (a lo más uno, porque las vigencias no se solapan)."""
        return next((contract for contract in self.contracts if contract.covers(day)), None)

    def contract_for_week(self, days: tuple[date, ...]) -> Contract | None:
        """Contrato de referencia de la semana: el que cubre más días hábiles (a igualdad, el más reciente)."""
        covered = [contract for contract in map(self.contract_on, days) if contract is not None]
        if not covered:
            return None
        return max(set(covered), key=lambda contract: (covered.count(contract), contract.valid_from))

    def week_contract_minutes(self, days: tuple[date, ...]) -> int:
        """Minutos de contrato de la semana, prorrateados por día hábil.

        Cada día hábil aporta la quinta parte de los minutos semanales del
        contrato vigente ese día (nada si no hay contrato). Una semana cubierta
        completa por un contrato recibe sus minutos exactos; si la vigencia
        empieza, termina o cambia a mitad de semana, cada contrato aporta solo
        la parte de los días que cubre.
        """
        if not days:
            return 0
        return sum(contract.weekly_minutes for contract in map(self.contract_on, days) if contract) // len(days)

    @property
    def allowed_rooms(self) -> frozenset[str]:
        """Salas de la matriz explícita (permitidas y preferidas). Vacía: sin matriz."""
        return frozenset(r.room_code for r in self.room_rules if r.kind is not RoomRuleKind.FORBIDDEN)

    @property
    def has_room_matrix(self) -> bool:
        return any(rule.kind is RoomRuleKind.ALLOWED for rule in self.room_rules)

    @property
    def forbidden_rooms(self) -> frozenset[str]:
        return frozenset(r.room_code for r in self.room_rules if r.kind is RoomRuleKind.FORBIDDEN)

    def preference_rank(self, room_code: str) -> int | None:
        for rule in self.room_rules:
            if rule.room_code == room_code and rule.kind is RoomRuleKind.PREFERRED:
                return rule.rank
        return None


@dataclass(frozen=True)
class Blocking:
    """Bloqueo recurrente (reunión, preparación, cierre, coordinación).

    `weekday` None significa todos los días. Los bloqueos son tiempo de trabajo:
    consumen contrato. `counts_as_admin` los clasifica como administrativos en
    la carga; si no, se muestran como reuniones.
    """

    name: str
    weekday: int | None
    start: int
    end: int
    audience: Audience
    target: str | None = None
    counts_as_admin: bool = False

    def __post_init__(self) -> None:
        if self.weekday is not None and not 0 <= self.weekday < WEEK_DAYS:
            raise ValidationError(f"El día del bloqueo {self.name} debe estar entre lunes y viernes.")
        if not 0 <= self.start < self.end:
            raise ValidationError(
                f"El bloqueo {self.name} tiene un intervalo inválido ({format_range(self.start, self.end)})."
            )
        needs_target = self.audience in (Audience.ROLE, Audience.STAFF)
        if needs_target and not self.target:
            raise ValidationError(f"El bloqueo {self.name} debe indicar el cargo o la persona destinataria.")
        if not needs_target and self.target:
            raise ValidationError(f"El bloqueo {self.name} no admite destinatario específico para '{self.audience}'.")

    def applies_on(self, weekday: int) -> bool:
        return self.weekday is None or self.weekday == weekday

    def applies_to(self, staff: Staff, role: Role) -> bool:
        """Resuelve el destinatario por atributos (cargo asistencial, código de cargo o persona)."""
        if self.audience is Audience.ALL:
            return True
        if self.audience is Audience.SERVICE_STAFF:
            return role.delivers_services
        if self.audience is Audience.ROLE:
            return role.code == self.target
        return staff.code == self.target


@dataclass(frozen=True)
class Absence:
    """Ausencia de una persona. Sin franja es el día completo; solo la aprobada bloquea."""

    staff_code: str
    day: date
    start: int | None
    end: int | None
    kind: str
    status: AbsenceStatus

    def __post_init__(self) -> None:
        if (self.start is None) != (self.end is None):
            raise ValidationError("Una ausencia parcial necesita hora de inicio y de fin.")
        if self.start is not None and self.end is not None and not 0 <= self.start < self.end:
            raise ValidationError("La franja de la ausencia es inválida: el inicio debe ser anterior al fin.")

    @property
    def blocks(self) -> bool:
        return self.status is AbsenceStatus.APPROVED

    @property
    def full_day(self) -> bool:
        return self.start is None


@dataclass(frozen=True)
class AbsenceRecord:
    """Ausencia con su identificador en la base (para editar su estado)."""

    id: int
    absence: Absence


@dataclass(frozen=True)
class Holiday:
    day: date
    name: str


@dataclass(frozen=True)
class DemandItem:
    """Sesiones requeridas de un tipo de atención en un bloque de un día de la semana."""

    weekday: int
    block_start: int
    service_code: str
    sessions: int
    priority: int

    def __post_init__(self) -> None:
        if not 0 <= self.weekday < WEEK_DAYS:
            raise ValidationError("El día de la demanda debe estar entre lunes y viernes.")
        if self.block_start % 60 != 0:
            raise ValidationError("El bloque de demanda debe comenzar en una hora exacta.")
        if self.sessions < 0:
            raise ValidationError("Las sesiones requeridas no pueden ser negativas.")
        _require_priority(self.priority, "la demanda")

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.weekday, self.block_start, self.service_code)


@dataclass(frozen=True)
class Scenario:
    """Parámetros de calidad de la fase B y piso de cobertura respecto de la fase A.

    Los pesos por tipo premian sesiones de ese tipo; las cotas de mezcla son
    fracciones de los minutos de atención con holgura penalizada.
    """

    name: str
    description: str
    service_weights: Mapping[str, float] = field(default_factory=dict)
    mix_min: Mapping[str, float] = field(default_factory=dict)
    mix_max: Mapping[str, float] = field(default_factory=dict)
    room_preference_weight: int = 2
    room_continuity_weight: int = 2
    admin_excess_weight: int = 5
    target_weight: int = 2
    mix_weight: int = 2
    coverage_floor_pct: int = 100
    id: int | None = None

    def __post_init__(self) -> None:
        for name in ("service_weights", "mix_min", "mix_max"):
            object.__setattr__(self, name, frozen_mapping(getattr(self, name)))
        if not self.name.strip():
            raise ValidationError("El escenario necesita un nombre.")
        if not 50 <= self.coverage_floor_pct <= 100:
            raise ValidationError("El piso de cobertura del escenario debe estar entre 50 % y 100 %.")
        for label, value in (
            ("preferencia de sala", self.room_preference_weight),
            ("continuidad de sala", self.room_continuity_weight),
            ("exceso en la sala administrativa", self.admin_excess_weight),
            ("metas", self.target_weight),
            ("mezcla", self.mix_weight),
        ):
            if value < 0:
                raise ValidationError(f"El peso de {label} no puede ser negativo.")
        if any(weight < 0 for weight in self.service_weights.values()):
            raise ValidationError("Los pesos por tipo de atención no pueden ser negativos.")
        for code in set(self.mix_min) | set(self.mix_max):
            low = self.mix_min.get(code, 0.0)
            high = self.mix_max.get(code, 1.0)
            if not 0.0 <= low <= high <= 1.0:
                raise ValidationError(f"Las cotas de mezcla de {code} deben cumplir 0 <= mínimo <= máximo <= 1.")


@dataclass(frozen=True)
class PlanningSettings:
    """Parámetros globales de la planificación y del optimizador."""

    extra_admin_daily_cap_min: int = 30
    extra_admin_weight: int = 1
    time_limit_s: float = 30.0
    seed: int = 42
    workers: int = 8
    phase_a_share: float = 0.6

    def __post_init__(self) -> None:
        require_aligned(self.extra_admin_daily_cap_min, "tope diario de administrativo adicional")
        if self.extra_admin_daily_cap_min < 0:
            raise ValidationError("El tope diario de administrativo adicional no puede ser negativo.")
        if self.time_limit_s <= 0:
            raise ValidationError("El límite de tiempo debe ser positivo.")
        if self.workers < 1:
            raise ValidationError("Se necesita al menos un trabajador de búsqueda.")
        if not 0.1 <= self.phase_a_share <= 0.9:
            raise ValidationError("La fracción de tiempo de la fase A debe estar entre 0,1 y 0,9.")


@dataclass(frozen=True)
class MasterData:
    """Todos los datos maestros necesarios para armar una semana de planificación."""

    roles: tuple[Role, ...]
    service_types: tuple[ServiceType, ...]
    rooms: tuple[Room, ...]
    staff: tuple[Staff, ...]
    blockings: tuple[Blocking, ...]
    absences: tuple[Absence, ...]
    holidays: tuple[Holiday, ...]
    center_hours: tuple[DayHours, ...]
    demand: tuple[DemandItem, ...]
    scenarios: tuple[Scenario, ...]
    settings: PlanningSettings = field(default_factory=PlanningSettings)
