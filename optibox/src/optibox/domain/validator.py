"""Validador independiente de planes.

No reutiliza el modelo del optimizador: recalcula todo desde las sesiones y
los tramos administrativos del plan y la instancia. Se ejecuta después de cada
corrida y en los tests.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from optibox.domain.instance import PlanningInstance
from optibox.domain.plan import Plan, admin_slots_per_day
from optibox.domain.rules import RuleChain
from optibox.domain.timegrid import SLOT_MINUTES, WEEKDAY_NAMES, block_of, format_minute, format_range


@dataclass(frozen=True)
class Violation:
    """Incumplimiento detectado: código estable y mensaje en español."""

    code: str
    message: str


Report = Callable[[Violation], None]


def validate_plan(instance: PlanningInstance, plan: Plan, chain: RuleChain | None = None) -> tuple[Violation, ...]:
    """Devuelve la lista de incumplimientos del plan (vacía si es válido)."""
    chain = chain or RuleChain()
    violations: list[Violation] = []
    add = violations.append

    known = True
    for session in plan.sessions:
        where = f"{session.staff} el {_day(session.day)} {format_range(session.start, session.end)}"
        if (
            session.staff not in instance.staff_by_code
            or session.service not in instance.service_by_code
            or session.room not in instance.room_by_code
            or not 0 <= session.day < len(instance.days)
        ):
            add(Violation("desconocido", f"La sesión de {where} usa una persona, tipo, sala o día desconocido."))
            known = False
            continue
        service = instance.service_by_code[session.service]
        room = instance.room_by_code[session.room]
        if session.duration != service.duration_min:
            add(Violation("duracion", f"La sesión de {where} no dura lo que exige {service.name}."))
        if session.participants != instance.participants(service, room):
            add(Violation("participantes", f"La sesión de {where} declara un cupo distinto del efectivo."))
        failure = chain.first_failure(instance, session.candidate)
        if failure is not None:
            rule, reason = failure
            add(Violation(f"regla:{rule.code}", f"La sesión de {where} incumple '{rule.description}': {reason}"))
    for block in plan.admin:
        if block.staff not in instance.staff_by_code or not 0 <= block.day < len(instance.days):
            add(
                Violation(
                    "desconocido", f"Un tramo administrativo usa una persona o un día desconocido ({block.staff})."
                )
            )
            known = False
    if not known:
        return tuple(violations)

    _check_overlaps(plan, add)
    _check_admin(instance, plan, add)
    _check_contracts(instance, plan, add)
    _check_caps_and_coverage(instance, plan, add)
    return tuple(violations)


def _day(index: int) -> str:
    return WEEKDAY_NAMES[index] if 0 <= index < len(WEEKDAY_NAMES) else str(index)


def _check_overlaps(plan: Plan, report: Report) -> None:
    staff_busy: dict[tuple[str, int], int] = defaultdict(int)
    room_busy: dict[tuple[str, int], int] = defaultdict(int)
    for session in plan.sessions:
        key = (session.staff, session.day)
        if staff_busy[key] & session.mask:
            report(
                Violation(
                    "solape_persona",
                    f"{session.staff} tiene dos actividades a la vez el {_day(session.day)} "
                    f"a las {format_minute(session.start)}.",
                )
            )
        staff_busy[key] |= session.mask
        room_key = (session.room, session.day)
        if room_busy[room_key] & session.mask:
            report(
                Violation(
                    "solape_sala",
                    f"La sala {session.room} tiene dos sesiones a la vez el {_day(session.day)} "
                    f"a las {format_minute(session.start)}.",
                )
            )
        room_busy[room_key] |= session.mask
    for block in plan.admin:
        key = (block.staff, block.day)
        if staff_busy[key] & block.mask:
            report(
                Violation(
                    "solape_persona",
                    f"{block.staff} tiene administrativo encima de otra actividad el {_day(block.day)} "
                    f"a las {format_minute(block.start)}.",
                )
            )
        staff_busy[key] |= block.mask


def _check_admin(instance: PlanningInstance, plan: Plan, report: Report) -> None:
    extra = instance.settings.extra_admin_daily_cap_min
    required: Counter[tuple[str, int]] = Counter()
    for session in plan.sessions:
        required[(session.staff, session.day)] += instance.service_by_code[session.service].admin_minutes
    placed: Counter[tuple[str, int]] = Counter()
    for block in plan.admin:
        placed[(block.staff, block.day)] += block.duration
        staff = instance.staff_by_code[block.staff]
        if block.start % SLOT_MINUTES or block.end % SLOT_MINUTES or block.end <= block.start:
            report(Violation("administrativo_grilla", f"El administrativo de {block.staff} no está alineado."))
            continue
        if not staff.delivers_services:
            report(Violation("administrativo_cargo", f"{block.staff} no tiene cargo asistencial."))
        if not instance.days[block.day].is_open or block.mask & ~staff.days[block.day].workable:
            report(
                Violation(
                    "administrativo_horario",
                    f"El administrativo de {block.staff} el {_day(block.day)} "
                    f"({format_range(block.start, block.end)}) cae fuera de su horario disponible.",
                )
            )
    for key in sorted(set(required) | set(placed)):
        staff, day = key
        if placed[key] < required[key]:
            report(
                Violation(
                    "administrativo_asociado",
                    f"{staff} tiene {placed[key]} min administrativos el {_day(day)} y sus sesiones exigen "
                    f"{required[key]} min.",
                )
            )
        if placed[key] > required[key] + extra:
            report(
                Violation(
                    "administrativo_tope",
                    f"{staff} supera el tope de administrativo adicional el {_day(day)}.",
                )
            )
    room = instance.admin_room
    if room is None and plan.admin:
        report(
            Violation(
                "sala_administrativa",
                "El plan asigna trabajo administrativo, pero no hay una sala administrativa activa.",
            )
        )
    if room is not None and room.simultaneous_hard is not None:
        for day in range(len(instance.days)):
            for start, load in admin_slots_per_day(plan.admin, day).items():
                if load > room.simultaneous_hard:
                    report(
                        Violation(
                            "sala_administrativa_capacidad",
                            f"La sala administrativa tiene {load} personas el {_day(day)} a las {format_minute(start)} "
                            f"(capacidad {room.simultaneous_hard}).",
                        )
                    )


def _check_contracts(instance: PlanningInstance, plan: Plan, report: Report) -> None:
    used: Counter[str] = Counter()
    for session in plan.sessions:
        used[session.staff] += session.duration
    for block in plan.admin:
        used[block.staff] += block.duration
    for code, minutes in sorted(used.items()):
        staff = instance.staff_by_code[code]
        total = minutes + staff.blocking_minutes()
        if total > staff.contract_minutes:
            report(
                Violation(
                    "contrato",
                    f"{code} suma {total} min (sesiones, administrativo y bloqueos) y su contrato es de "
                    f"{staff.contract_minutes} min.",
                )
            )


def _check_caps_and_coverage(instance: PlanningInstance, plan: Plan, report: Report) -> None:
    week: Counter[str] = Counter()
    week_staff: Counter[tuple[str, str]] = Counter()
    day_total: Counter[tuple[str, int]] = Counter()
    day_staff: Counter[tuple[str, str, int]] = Counter()
    coverage: Counter[tuple[int, int, str]] = Counter()
    for session in plan.sessions:
        week[session.service] += 1
        week_staff[(session.service, session.staff)] += 1
        day_total[(session.service, session.day)] += 1
        day_staff[(session.service, session.staff, session.day)] += 1
        coverage[(session.day, block_of(session.start), session.service)] += 1
    for service in instance.service_types:
        if service.max_week_total is not None and week[service.code] > service.max_week_total:
            report(Violation("tope_semanal", f"{service.name} supera su tope semanal total."))
    for (code, staff), count in sorted(week_staff.items()):
        cap = instance.service_by_code[code].max_week_per_staff
        if cap is not None and count > cap:
            report(Violation("tope_semanal_persona", f"{staff} supera el tope semanal de {code}."))
    for (code, day), count in sorted(day_total.items()):
        cap = instance.service_by_code[code].max_day_total
        if cap is not None and count > cap:
            report(Violation("tope_diario", f"{code} supera su tope diario el {_day(day)}."))
    for (code, staff, day), count in sorted(day_staff.items()):
        cap = instance.service_by_code[code].max_day_per_staff
        if cap is not None and count > cap:
            report(Violation("tope_diario_persona", f"{staff} supera el tope diario de {code} el {_day(day)}."))
    for key, count in sorted(coverage.items()):
        item = instance.demand_by_key.get(key)
        demand = item.sessions if item is not None else 0
        if count > demand:
            day, block, code = key
            report(
                Violation(
                    "cobertura",
                    f"Se asignan {count} sesiones de {code} el {_day(day)} en el bloque {format_minute(block)} "
                    f"y la demanda es {demand}.",
                )
            )
