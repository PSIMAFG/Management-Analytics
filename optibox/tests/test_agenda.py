"""Agenda semanal por persona y por sala derivada del plan."""

from __future__ import annotations

from factories import MONDAY, admin, make_instance, meeting, session, small_master
from optibox.domain.agenda import AgendaKind, room_agenda, staff_agenda
from optibox.domain.models import Absence, AbsenceStatus, Audience, DemandItem, Holiday
from optibox.domain.plan import Plan


def test_staff_agenda_uses_real_afternoon_hours() -> None:
    """Regresión B19: la hora mostrada sale del minuto de inicio, también en la tarde."""
    instance = make_instance(small_master(demand=(DemandItem(0, 840, "IND", 1, 2),)))
    plan = Plan(sessions=(session(instance, "A", start=885),), admin=(admin("A", 0, 930, 945),))
    entries = staff_agenda(instance, plan, "A")
    assert [(e.kind, e.time_label) for e in entries] == [
        (AgendaKind.SESSION, "14:45-15:30"),
        (AgendaKind.ADMIN, "15:30-15:45"),
    ]
    assert entries[0].label == "Atención IND - Box 1"
    assert entries[0].color == "#1F4E79"


def test_staff_agenda_shows_blockings_absences_and_holidays() -> None:
    master = small_master(
        blockings=(meeting(2, 540, 660, Audience.SERVICE_STAFF),),
        absences=(Absence("A", MONDAY.replace(day=MONDAY.day + 1), 480, 600, "Trámite", AbsenceStatus.APPROVED),),
        holidays=(Holiday(MONDAY, "Feriado local"),),
    )
    instance = make_instance(master)
    entries = staff_agenda(instance, Plan(), "A")
    kinds = {(e.day, e.kind, e.label) for e in entries}
    assert (0, AgendaKind.HOLIDAY, "Feriado: Feriado local") in kinds
    assert (1, AgendaKind.ABSENCE, "Ausencia: Trámite") in kinds
    assert (2, AgendaKind.BLOCKING, "Reunión") in kinds
    assert all(e.kind is not AgendaKind.BLOCKING for e in staff_agenda(instance, Plan(), "C"))
    assert staff_agenda(instance, Plan(), "NADIE") == ()


def test_each_absence_keeps_its_own_reason() -> None:
    """Dos ausencias el mismo día se muestran cada una con su motivo, no con el de la primera."""
    day = MONDAY.replace(day=MONDAY.day + 1)
    master = small_master(
        absences=(
            Absence("A", day, 480, 600, "Trámite", AbsenceStatus.APPROVED),
            Absence("A", day, 900, 960, "Control médico", AbsenceStatus.APPROVED),
            Absence("A", day, 720, 780, "Permiso pendiente", AbsenceStatus.PENDING),
        ),
    )
    entries = staff_agenda(make_instance(master), Plan(), "A")
    absences = [(e.time_label, e.label) for e in entries if e.kind is AgendaKind.ABSENCE]
    assert absences == [("08:00-10:00", "Ausencia: Trámite"), ("15:00-16:00", "Ausencia: Control médico")]


def test_admin_room_agenda_counts_people() -> None:
    instance = make_instance()
    plan = Plan(admin=(admin("A", 0, 600, 630), admin("B", 0, 615, 645)))
    entries = room_agenda(instance, plan, "ADM")
    assert [(e.time_label, e.label) for e in entries] == [
        ("10:00-10:15", "1 de 2 personas"),
        ("10:15-10:30", "2 de 2 personas"),
        ("10:30-10:45", "1 de 2 personas"),
    ]


def test_overlapping_entries_are_kept_in_lanes() -> None:
    """Regresión B20: la grilla por sala no pierde sesiones que se superponen."""
    instance = make_instance()
    plan = Plan(sessions=(session(instance, "A", start=540), session(instance, "B", start=555)))
    entries = room_agenda(instance, plan, "R1")
    assert len(entries) == 2
    assert sorted(e.lane for e in entries) == [0, 1]
    assert {e.staff for e in entries} == {"A", "B"}
