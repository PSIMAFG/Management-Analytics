"""Generador reproducible de datos sintéticos (semilla fija).

Los datos imitan la estructura y las magnitudes de un centro de atención real
sin reproducir ningún dato identificatorio: 12 personas con la misma mezcla
de cargos (4 terapeutas ocupacionales, 3 fonoaudiólogos, 1 kinesiólogo,
2 psicólogos, 1 trabajador social y 1 apoyo administrativo), contratos de 18
a 44 horas con dos cambios de jornada en el año, 9 salas arquetipo, 7 tipos de
atención con topes, bloqueos operativos, ausencias en la semana de la demo y
un feriado en la semana siguiente. La demanda se genera con una distribución
de Poisson por día, bloque y tipo, calibrada para que la capacidad quede algo
por debajo de la demanda en las horas punta.

Todas las fechas se calculan a partir de la semana de la demo (el lunes
siguiente a la fecha de referencia), de modo que los datos tienen sentido en
cualquier momento en que se genere la base.
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import date, timedelta

from optibox.data.db import transaction
from optibox.data.repository import MasterDataRepository
from optibox.domain.models import (
    Absence,
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
from optibox.domain.timegrid import BLOCK_MINUTES, DEFAULT_HOURS, SLOT_MINUTES, DayHours, week_monday

SEED = 2008
DEMO_WEEK_KEY = "demo_week_start"

FIRST_NAMES = (
    "Camila",
    "Valentina",
    "Javiera",
    "Constanza",
    "Catalina",
    "Daniela",
    "Francisca",
    "Isidora",
    "Antonia",
    "Martina",
    "Florencia",
    "Josefa",
    "Tomás",
    "Benjamín",
    "Vicente",
    "Joaquín",
    "Cristóbal",
    "Agustín",
    "Maximiliano",
    "Gabriel",
)
SURNAMES = (
    "González",
    "Muñoz",
    "Rojas",
    "Díaz",
    "Soto",
    "Contreras",
    "Silva",
    "Sepúlveda",
    "Morales",
    "Fuentes",
    "Araya",
    "Espinoza",
    "Valenzuela",
    "Tapia",
    "Reyes",
    "Castillo",
    "Pizarro",
    "Vergara",
    "Bravo",
    "Olivares",
)

ROLES = (
    Role("TO", "Terapeuta ocupacional", True, frozenset({"EVI", "EVS", "AIN", "EIN", "TGR"})),
    Role("FO", "Fonoaudiólogo/a", True, frozenset({"EVI", "EVS", "AIN", "EIN", "TGR"})),
    Role("KI", "Kinesiólogo/a", True, frozenset({"EVI", "EVS", "AIN", "TGR"})),
    Role("PS", "Psicólogo/a", True, frozenset({"AIN", "AFA", "OFA", "EIN", "TGR"})),
    Role("TS", "Trabajador/a social", True, frozenset({"AFA", "OFA", "TGR"})),
    Role("AD", "Apoyo administrativo", False, frozenset()),
)

_INDIVIDUAL_ROOMS = frozenset({"MUL", "B01", "B02", "GIM", "SGR", "BAP"})
SERVICE_TYPES = (
    ServiceType("EVI", "Evaluación de ingreso", 45, 1, False, False, 15, 1, "#1F4E79", _INDIVIDUAL_ROOMS, 30, 4),
    ServiceType("EVS", "Evaluación de seguimiento", 45, 1, False, False, 15, 2, "#2E86C1", _INDIVIDUAL_ROOMS, 30, 6),
    ServiceType("AIN", "Atención individual", 45, 1, False, False, 15, 2, "#7FB3D5", _INDIVIDUAL_ROOMS, 110, 25),
    ServiceType(
        "AFA", "Atención familiar", 45, 1, False, False, 15, 2, "#B7791F", frozenset({"SPS", "BAP", "MUL"}), 25, 5
    ),
    ServiceType(
        "OFA", "Orientación familiar", 45, 1, False, False, 15, 3, "#D4A857", frozenset({"SPS", "MUL", "BAP"}), 15, 5
    ),
    ServiceType(
        "EIN",
        "Evaluación integral",
        120,
        1,
        False,
        True,
        60,
        2,
        "#5D6D7E",
        frozenset({"EVA", "MUL", "B01", "BAP"}),
        12,
        4,
    ),
    ServiceType(
        "TGR",
        "Taller en grupo",
        90,
        6,
        True,
        False,
        30,
        3,
        "#2E7D32",
        frozenset({"MUL", "GIM", "SGR", "B02"}),
        2,
        1,
        1,
        1,
    ),
)

ROOMS = (
    Room("MUL", "Sala de usos múltiples", RoomKind.MULTIPURPOSE, True, True, 8),
    Room("EVA", "Sala de evaluación", RoomKind.EVALUATION, False, True, 4),
    Room("B01", "Box 1", RoomKind.BOX, False, True, 3, reserved_role="FO"),
    Room("B02", "Box 2", RoomKind.BOX, True, False, 4, reserved_role="TO"),
    Room("GIM", "Gimnasio", RoomKind.GYM, True, True, 6, reserved_role="KI"),
    Room("SGR", "Sala grupal", RoomKind.GROUP, True, False, 3, reserved_role="TO"),
    Room("SPS", "Sala psicosocial", RoomKind.PSYCHOSOCIAL, False, False, 4),
    Room("ADM", "Sala administrativa", RoomKind.ADMIN, False, False, 10, simultaneous_hard=10, simultaneous_soft=8),
    Room("BAP", "Box de apoyo", RoomKind.BOX, False, True, 1),
)

# Patrones de disponibilidad (día: lista de ventanas "HH:MM-HH:MM").
_FULL = {0: ["08:00-17:00"], 1: ["08:00-17:00"], 2: ["08:00-17:00"], 3: ["08:00-17:00"], 4: ["08:00-16:00"]}
_PATTERNS = {
    "completa": _FULL,
    "lun_mar_mie_am_jue_pm": {0: ["08:00-17:00"], 1: ["08:00-17:00"], 2: ["08:00-13:00"], 3: ["14:00-17:00"]},
    "mie_am_jue_vie": {2: ["08:00-13:00"], 3: ["08:00-17:00"], 4: ["08:00-16:00"]},
    "mar_am_mie_jue_vie_am": {1: ["08:00-14:00"], 2: ["08:00-17:00"], 3: ["08:00-17:00"], 4: ["08:00-14:00"]},
    "lun_mar_mie_am_jue": {0: ["08:00-17:00"], 1: ["08:00-17:00"], 2: ["08:00-11:00"], 3: ["08:00-17:00"]},
    "lun_am_mie_vie": {0: ["08:00-13:00"], 2: ["08:00-17:00"], 4: ["08:00-16:00"]},
    "lun_mar_mie_jue_am": {0: ["08:00-17:00"], 1: ["08:00-17:00"], 2: ["08:00-17:00"], 3: ["08:00-13:00"]},
}

# Código, cargo, patrón, horas por contrato, lista blanca, reglas de sala y metas (minutos).
_P = RoomRuleKind.PREFERRED
_A = RoomRuleKind.ALLOWED
_F = RoomRuleKind.FORBIDDEN
_STAFF_BLUEPRINT: tuple[
    tuple[str, str, str, tuple[int, ...], tuple[str, ...], tuple[RoomRule, ...], tuple[ServiceTarget, ...]], ...
] = (
    (
        "TO-01",
        "TO",
        "completa",
        (44,),
        ("EVI", "EVS", "AIN", "TGR"),
        (RoomRule("SGR", _P, 1), RoomRule("B02", _A), RoomRule("MUL", _A)),
        (),
    ),
    (
        "TO-02",
        "TO",
        "lun_mar_mie_am_jue_pm",
        (22,),
        ("EVI", "EVS", "AIN", "TGR"),
        (RoomRule("B02", _P, 1), RoomRule("MUL", _A), RoomRule("BAP", _A)),
        (),
    ),
    (
        "TO-03",
        "TO",
        "completa",
        (44,),
        ("EVI", "EVS", "AIN", "EIN"),
        (RoomRule("MUL", _P, 1), RoomRule("EVA", _P, 2), RoomRule("B02", _A), RoomRule("SGR", _A), RoomRule("BAP", _A)),
        (ServiceTarget("EIN", 360, 720),),
    ),
    (
        "TO-04",
        "TO",
        "mie_am_jue_vie",
        (22,),
        ("EVI", "EVS", "AIN"),
        (RoomRule("B02", _P, 1), RoomRule("MUL", _P, 2), RoomRule("BAP", _A)),
        (),
    ),
    (
        "FO-01",
        "FO",
        "mar_am_mie_jue_vie_am",
        (30,),
        ("EVI", "EVS", "AIN", "TGR"),
        (RoomRule("B01", _P, 1), RoomRule("BAP", _A)),
        (),
    ),
    (
        "FO-02",
        "FO",
        "lun_mar_mie_am_jue",
        (30,),
        ("EVI", "EVS", "AIN", "EIN"),
        (RoomRule("B01", _P, 1), RoomRule("MUL", _P, 2), RoomRule("EVA", _P, 3), RoomRule("BAP", _A)),
        (ServiceTarget("EIN", 360, 720), ServiceTarget("AIN", None, 480)),
    ),
    ("FO-03", "FO", "lun_am_mie_vie", (18,), ("EVI", "EVS", "AIN"), (RoomRule("BAP", _P, 1), RoomRule("MUL", _F)), ()),
    ("KI-01", "KI", "completa", (44,), (), (RoomRule("GIM", _P, 1), RoomRule("MUL", _A)), ()),
    (
        "PS-01",
        "PS",
        "lun_mar_mie_jue_am",
        (32, 22),
        ("AFA", "OFA", "TGR"),
        (RoomRule("SPS", _P, 1), RoomRule("MUL", _A)),
        (),
    ),
    (
        "PS-02",
        "PS",
        "completa",
        (44, 22),
        ("EIN", "AIN"),
        (RoomRule("EVA", _P, 1), RoomRule("BAP", _A), RoomRule("SPS", _A)),
        (ServiceTarget("EIN", 360, 720),),
    ),
    ("TS-01", "TS", "completa", (44,), (), (RoomRule("SPS", _P, 1),), ()),
    ("AD-01", "AD", "completa", (44,), (), (), ()),
)

# Demanda base por bloque (sesiones esperadas) y factores por día y hora.
_BASE_RATE = {"AIN": 2.7, "EVI": 0.6, "EVS": 0.6, "AFA": 0.35, "OFA": 0.25, "EIN": 0.55}
_DAY_FACTOR = (1.1, 1.05, 0.85, 1.0, 0.8)
_HOUR_FACTOR = {480: 0.8, 540: 1.3, 600: 1.35, 660: 1.2, 720: 0.9, 840: 0.95, 900: 0.85, 960: 0.5}
_GROUP_DEMAND = ((1, 900), (3, 840))
# Durante la reunión técnica del miércoles la demanda agendada es residual.
_MEETING_BLOCKS = frozenset({(2, 540), (2, 600)})
_MEETING_FACTOR = 0.25


def _hhmm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _windows(pattern: str) -> tuple[AvailabilityWindow, ...]:
    windows: list[AvailabilityWindow] = []
    for weekday, ranges in sorted(_PATTERNS[pattern].items()):
        for text in ranges:
            start, end = text.split("-")
            windows.append(AvailabilityWindow(weekday, _hhmm(start), _hhmm(end)))
    return tuple(windows)


def _poisson(rng: random.Random, rate: float) -> int:
    """Muestra de Poisson por el método de Knuth (tasas pequeñas)."""
    if rate <= 0:
        return 0
    limit = math.exp(-rate)
    count = 0
    product = rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def demo_week(reference: date) -> date:
    """Semana de la demo: el lunes siguiente a la fecha de referencia."""
    return week_monday(reference) + timedelta(days=7)


def _names(rng: random.Random, count: int) -> list[str]:
    names: set[str] = set()
    result: list[str] = []
    while len(result) < count:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)} {rng.choice(SURNAMES)}"
        if name not in names:
            names.add(name)
            result.append(name)
    return result


def _contracts(hours: tuple[int, ...], monday: date, change: date) -> tuple[Contract, ...]:
    """Un contrato indefinido, o dos consecutivos si la persona cambia de jornada en `change`."""
    start = (monday - timedelta(days=200)).replace(day=1)
    if len(hours) == 1:
        return (Contract(start, None, hours[0] * 60),)
    return (
        Contract(start, change - timedelta(days=1), hours[0] * 60),
        Contract(change, None, hours[1] * 60),
    )


def _staff(rng: random.Random, monday: date) -> tuple[Staff, ...]:
    names = _names(rng, len(_STAFF_BLUEPRINT))
    changes = {"PS-01": monday - timedelta(days=56), "PS-02": monday + timedelta(days=21)}
    staff: list[Staff] = []
    for (code, role, pattern, hours, skills, rooms, targets), name in zip(_STAFF_BLUEPRINT, names, strict=True):
        staff.append(
            Staff(
                code=code,
                name=name,
                role_code=role,
                contracts=_contracts(hours, monday, changes.get(code, monday)),
                availability=_windows(pattern),
                skills=frozenset(skills),
                room_rules=rooms,
                targets=targets,
            )
        )
    return tuple(staff)


def _blockings() -> tuple[Blocking, ...]:
    return (
        Blocking("Preparación de salas", None, 480, 495, Audience.ALL),
        Blocking("Cierre del centro", None, 1005, 1020, Audience.ALL),
        Blocking("Cierre del centro (viernes)", 4, 945, 960, Audience.ALL),
        Blocking("Reunión técnica semanal", 2, 540, 660, Audience.SERVICE_STAFF),
        Blocking("Coordinación de programa", 0, 840, 1005, Audience.STAFF, "TO-03", counts_as_admin=True),
        Blocking("Coordinación de programa", 2, 840, 1005, Audience.STAFF, "TO-03", counts_as_admin=True),
        Blocking("Supervisión de casos", 3, 720, 780, Audience.ROLE, "PS"),
        Blocking("Coordinación con la red", 1, 630, 675, Audience.ROLE, "TS", counts_as_admin=True),
        Blocking("Gestión de casos en terreno", 3, 840, 1005, Audience.ROLE, "TS", counts_as_admin=True),
    )


def _absences(monday: date) -> tuple[Absence, ...]:
    return (
        Absence("FO-01", monday + timedelta(days=3), None, None, "Permiso administrativo", AbsenceStatus.APPROVED),
        Absence("TO-02", monday + timedelta(days=1), 480, 780, "Permiso parcial", AbsenceStatus.APPROVED),
        Absence("KI-01", monday + timedelta(days=4), None, None, "Vacaciones", AbsenceStatus.PENDING),
        Absence("TS-01", monday, None, None, "Permiso administrativo", AbsenceStatus.REJECTED),
    )


def _fits_somewhere(hours: DayHours, block: int, duration: int, closures: tuple[Blocking, ...]) -> bool:
    """Indica si una sesión de `duration` minutos puede comenzar en algún slot del bloque."""
    closed = [(b.start, b.end) for b in closures if b.applies_on(hours.weekday)]
    for start in range(block, block + BLOCK_MINUTES, SLOT_MINUTES):
        end = start + duration
        if hours.contains(start, end) and all(end <= c_start or c_end <= start for c_start, c_end in closed):
            return True
    return False


def _demand(rng: random.Random) -> tuple[DemandItem, ...]:
    """Demanda Poisson por día, bloque y tipo, solo donde el tipo cabe en el horario del centro."""
    items: list[DemandItem] = []
    services = {service.code: service for service in SERVICE_TYPES}
    closures = tuple(b for b in _blockings() if b.audience is Audience.ALL)
    for hours in DEFAULT_HOURS:
        for block in hours.blocks():
            factor = _DAY_FACTOR[hours.weekday] * _HOUR_FACTOR.get(block, 1.0)
            if (hours.weekday, block) in _MEETING_BLOCKS:
                factor *= _MEETING_FACTOR
            for code, base in _BASE_RATE.items():
                service = services[code]
                if not _fits_somewhere(hours, block, service.duration_min, closures):
                    continue
                sessions = _poisson(rng, base * factor)
                priority = service.priority
                if priority > 1 and rng.random() < 0.15:
                    priority -= 1
                if sessions:
                    items.append(DemandItem(hours.weekday, block, code, sessions, priority))
    for weekday, block in _GROUP_DEMAND:
        items.append(DemandItem(weekday, block, "TGR", 1, 2))
    return tuple(sorted(items, key=lambda item: item.key))


def _scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            name="Equilibrado",
            description="Equilibra cobertura, continuidad de sala y metas del equipo.",
            service_weights=dict.fromkeys(_BASE_RATE, 1.0) | {"TGR": 1.0},
            mix_min={"AIN": 0.35},
            mix_max={"EIN": 0.35, "TGR": 0.1},
            room_preference_weight=2,
            room_continuity_weight=2,
            admin_excess_weight=5,
            target_weight=2,
            mix_weight=2,
            coverage_floor_pct=100,
        ),
        Scenario(
            name="Solo cobertura",
            description="Mantiene la cobertura de la fase A y no considera preferencias de sala, metas ni mezcla.",
            service_weights=dict.fromkeys(_BASE_RATE, 1.0) | {"TGR": 1.0},
            room_preference_weight=0,
            room_continuity_weight=1,
            admin_excess_weight=2,
            target_weight=0,
            mix_weight=0,
            coverage_floor_pct=100,
        ),
        Scenario(
            name="Prioriza evaluaciones",
            description="Acepta hasta 5 % menos de cobertura ponderada para agendar más evaluaciones integrales.",
            service_weights={"EIN": 6.0, "EVI": 2.0, "EVS": 1.0, "AIN": 1.0, "AFA": 1.0, "OFA": 1.0, "TGR": 1.0},
            mix_min={"EIN": 0.12},
            room_preference_weight=1,
            room_continuity_weight=1,
            admin_excess_weight=5,
            target_weight=2,
            mix_weight=3,
            coverage_floor_pct=95,
        ),
    )


def generate_master_data(reference: date, seed: int = SEED) -> MasterData:
    """Datos maestros sintéticos para la semana de la demo que sigue a `reference`."""
    rng = random.Random(seed)
    monday = demo_week(reference)
    return MasterData(
        roles=ROLES,
        service_types=SERVICE_TYPES,
        rooms=ROOMS,
        staff=_staff(rng, monday),
        blockings=_blockings(),
        absences=_absences(monday),
        holidays=(Holiday(monday + timedelta(days=11), "Feriado nacional"),),
        center_hours=DEFAULT_HOURS,
        demand=_demand(rng),
        scenarios=_scenarios(),
        settings=PlanningSettings(),
    )


def seed_demo_data(conn: sqlite3.Connection, reference: date | None = None, seed: int = SEED) -> date:
    """Puebla una base vacía con los datos sintéticos y devuelve el lunes de la semana de la demo."""
    reference = reference or date.today()
    master = generate_master_data(reference, seed)
    repo = MasterDataRepository(conn)
    with transaction(conn):
        repo.insert_all(master)
        repo.set_setting(DEMO_WEEK_KEY, demo_week(reference).isoformat())
    return demo_week(reference)
