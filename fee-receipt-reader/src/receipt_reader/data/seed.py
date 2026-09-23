"""Siembra de catálogos y parámetros de ejemplo (semilla fija).

Carga la organización receptora de ejemplo, los 4 programas con sus alias,
la tabla legal de retención por año y los rangos de referencia del valor
hora. Las boletas del lote de ejemplo se registran después, procesando los
archivos sintéticos (ver `services.demo`).
"""

from __future__ import annotations

import sqlite3

from receipt_reader.data import catalog_repo
from receipt_reader.data.db import transaction
from receipt_reader.data.synthetic import DEMO_SETTINGS, PROGRAMS, reference_range
from receipt_reader.domain.models import ProgramAlias, ReferenceRate, Settings
from receipt_reader.domain.retention import LEGAL_RATES_BP

REFERENCE_YEARS = (2025, 2026)


def seed_catalog(conn: sqlite3.Connection, settings: Settings = DEMO_SETTINGS) -> None:
    """Programas, alias, tasas de retención, valores hora de referencia y configuración."""
    with transaction(conn):
        catalog_repo.save_settings(conn, settings)
        for year, rate_bp in LEGAL_RATES_BP.items():
            catalog_repo.upsert_retention_rate(conn, year, rate_bp)
        for spec in PROGRAMS:
            program_id = catalog_repo.insert_program(conn, spec.folder_code, spec.name, spec.short_name)
            for alias, priority in spec.aliases:
                catalog_repo.insert_alias(conn, ProgramAlias(program_id, alias, priority))
            rates = []
            for year in REFERENCE_YEARS:
                low, high = reference_range(spec, year)
                rates.append(ReferenceRate(program_id, year, low, high))
            catalog_repo.upsert_reference_rates(conn, rates)
