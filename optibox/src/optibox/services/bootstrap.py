"""Preparación de la base: esquema, datos sintéticos y corrida de ejemplo."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from optibox.data import db
from optibox.data.seed import SEED, seed_demo_data
from optibox.errors import AppError, DataError
from optibox.services.planning_service import PlanningService

log = logging.getLogger(__name__)

DEMO_SCENARIOS = ("Equilibrado",)
DEMO_TIME_LIMIT_S = 15.0


def prepare_database(
    db_path: Path,
    reset: bool = False,
    *,
    reference: date | None = None,
    demo_scenarios: Sequence[str] = DEMO_SCENARIOS,
    demo_time_limit_s: float = DEMO_TIME_LIMIT_S,
) -> bool:
    """Crea la base en la primera ejecución, la puebla y guarda corridas de ejemplo.

    Los datos maestros son siempre los mismos (semilla fija). La corrida de
    ejemplo usa la búsqueda paralela del solver con un límite corto: el valor
    de cobertura alcanzado es estable, aunque el detalle del plan puede variar
    levemente entre instalaciones. Devuelve True si la base se creó en esta llamada.
    """
    if reset and db_path.exists():
        try:
            db_path.unlink()
        except OSError as error:
            raise DataError(
                f"No se pudo eliminar la base de datos {db_path} para regenerarla: está en uso por otro programa. "
                "Ciérrelo e intente de nuevo."
            ) from error
        log.info("Base de datos eliminada para regenerarla")
    created = False
    try:
        with db.session(db_path) as conn:
            created = db.initialize(conn)
            if created:
                week = seed_demo_data(conn, reference, SEED)
                log.info("Datos sintéticos creados para la semana del %s", week.isoformat())
    except Exception:
        if created and db_path.exists():
            # Una base a medio poblar se elimina para regenerarla en el próximo inicio.
            db_path.unlink()
        raise
    if not created:
        return False
    service = PlanningService(db_path)
    by_name = {scenario.name: scenario for scenario in service.scenarios()}
    for name in demo_scenarios:
        scenario = by_name.get(name)
        if scenario is None or scenario.id is None:
            log.warning("No existe el escenario de ejemplo %s", name)
            continue
        try:
            detail = service.optimize(service.default_week(), scenario.id, demo_time_limit_s)
        except AppError as error:
            # La base ya quedó poblada: sin corrida de ejemplo la ventana abre vacía y se puede optimizar.
            log.warning("No se pudo guardar la corrida de ejemplo (%s): %s", name, error.user_message)
            continue
        log.info(
            "Corrida de ejemplo %d (%s): %d de %d sesiones cubiertas",
            detail.summary.id,
            name,
            detail.metrics.totals.covered_sessions,
            detail.metrics.totals.demand_sessions,
        )
    return True
