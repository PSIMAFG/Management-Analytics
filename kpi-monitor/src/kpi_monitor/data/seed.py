"""Creación de la base de ejemplo: catálogo, parámetros y observaciones sintéticas."""

from __future__ import annotations

import logging
import sqlite3

from kpi_monitor.data.catalog_seed import build_catalog
from kpi_monitor.data.db import transaction
from kpi_monitor.data.repositories import CatalogRepository, ObservationRepository, SettingsRepository
from kpi_monitor.data.synthetic import CURRENT_PERIOD, LOADED_AT, PREVALENCE, SEED, generate_observations
from kpi_monitor.domain.models import MonitorSettings

log = logging.getLogger(__name__)


def seed_demo_data(conn: sqlite3.Connection, seed: int = SEED) -> None:
    """Puebla una base recién creada con el catálogo y tres años de datos sintéticos."""
    catalog = build_catalog()
    observations = generate_observations(catalog, seed)
    settings = MonitorSettings(prevalence=PREVALENCE, default_period=CURRENT_PERIOD)
    with transaction(conn):
        CatalogRepository(conn).save(catalog)
        SettingsRepository(conn).save(settings)
        ObservationRepository(conn).upsert_many(observations, LOADED_AT)
    log.info("Datos de ejemplo creados: %d indicadores, %d observaciones", len(catalog.indicators), len(observations))
