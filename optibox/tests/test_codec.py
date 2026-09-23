"""Copia JSON de la instancia guardada con cada corrida."""

from __future__ import annotations

import json

import pytest

from factories import MONDAY, make_instance, meeting, small_master
from optibox.data import codec
from optibox.domain.instance import PlanningInstance
from optibox.domain.models import Absence, AbsenceStatus, Audience, Holiday, Scenario
from optibox.errors import DataError


def test_instance_roundtrip_is_exact() -> None:
    master = small_master(
        blockings=(meeting(2, 540, 660, Audience.SERVICE_STAFF),),
        absences=(Absence("A", MONDAY, 480, 600, "Permiso", AbsenceStatus.APPROVED),),
        holidays=(Holiday(MONDAY.replace(day=MONDAY.day + 4), "Feriado"),),
    )
    scenario = Scenario("Mezcla", "Con cotas", service_weights={"IND": 2.0}, mix_min={"IND": 0.3}, id=7)
    instance = make_instance(master, scenario)
    restored = codec.loads(PlanningInstance, codec.dumps(instance))
    assert restored == instance
    assert restored.staff_by_code["A"].days[0].absent == instance.staff_by_code["A"].days[0].absent
    assert restored.scenario.mix_min == {"IND": 0.3}


def test_instances_saved_before_the_prorated_contract_still_load() -> None:
    """Una corrida guardada sin los campos nuevos de contrato se lee con el contrato completo de la semana."""
    instance = make_instance()
    data = json.loads(codec.dumps(instance))
    for staff in data["staff"]:
        del staff["prorated_minutes"]
        for day in staff["days"]:
            del day["in_contract"]
    restored = codec.loads(PlanningInstance, json.dumps(data))
    assert restored.staff_by_code["A"].contract_minutes == 44 * 60
    assert all(day.in_contract for day in restored.staff_by_code["A"].days)


def test_corrupt_json_raises_data_error() -> None:
    with pytest.raises(DataError):
        codec.loads(PlanningInstance, "{no es json")
