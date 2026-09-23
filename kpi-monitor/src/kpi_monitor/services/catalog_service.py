"""Consulta del catálogo: programas, sedes, indicadores, metas, pesos, fichas y parámetros."""

from __future__ import annotations

import threading
from pathlib import Path

from kpi_monitor.data.db import open_db, transaction
from kpi_monitor.data.repositories import CatalogRepository, SettingsRepository
from kpi_monitor.domain.catalog import Catalog, validate_catalog
from kpi_monitor.domain.models import GoalRule, Indicator, MonitorSettings, Program, Site
from kpi_monitor.domain.summary import IndicatorSheetView


class CatalogService:
    """Punto único de lectura de la configuración. El catálogo se lee una vez y se guarda en memoria."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._catalog: Catalog | None = None
        self._settings: MonitorSettings | None = None

    def catalog(self) -> Catalog:
        """Catálogo completo (programas, sedes, indicadores, reglas y metas); se lee una sola vez."""
        with self._lock:
            if self._catalog is None:
                with open_db(self.db_path) as conn:
                    self._catalog = CatalogRepository(conn).load()
            return self._catalog

    def settings(self) -> MonitorSettings:
        """Parámetros de cálculo guardados: umbrales del semáforo, prevalencia y período por defecto."""
        with self._lock:
            if self._settings is None:
                with open_db(self.db_path) as conn:
                    self._settings = SettingsRepository(conn).load()
            return self._settings

    def invalidate(self) -> None:
        """Olvida lo leído para volver a consultarlo en la base."""
        with self._lock:
            self._catalog = None
            self._settings = None

    def save_settings(self, settings: MonitorSettings) -> MonitorSettings:
        """Guarda los parámetros de cálculo en una transacción y descarta los leídos antes."""
        message = "No se pudieron guardar los parámetros. Cierre otras ventanas de la aplicación y vuelva a intentarlo."
        with open_db(self.db_path, message) as conn, transaction(conn):
            SettingsRepository(conn).save(settings)
        self.invalidate()
        return settings

    def programs(self) -> list[Program]:
        """Programas en su orden de presentación."""
        return self.catalog().ordered_programs()

    def sites(self) -> list[Site]:
        """Sedes en su orden de presentación."""
        return self.catalog().ordered_sites()

    def years(self) -> list[int]:
        """Años con metas configuradas, de menor a mayor."""
        return list(self.catalog().years)

    def indicators(self, program_code: str | None = None, year: int | None = None) -> list[Indicator]:
        """Indicadores de un programa; con `year`, solo los vigentes ese año."""
        catalog = self.catalog()
        if year is not None:
            return catalog.active_indicators(year, program_code)
        return [
            ind
            for ind in sorted(
                catalog.indicators, key=lambda i: (catalog.program(i.program_code).sort_order, i.sort_order)
            )
            if program_code is None or ind.program_code == program_code
        ]

    def indicator(self, code: str) -> Indicator:
        """Indicador por código; lanza ValidationError si no existe."""
        return self.catalog().indicator(code)

    def goal_rule(self, indicator_code: str, year: int) -> GoalRule | None:
        """Regla de meta del indicador en el año, o None si ese año no tiene regla."""
        return self.catalog().rule(indicator_code, year)

    def weights(self, year: int, program_code: str) -> dict[str, float]:
        """Pesos del programa en el año (código de indicador -> peso entre 0 y 1)."""
        return self.catalog().weights(year, program_code)

    def site_goals(self, indicator_code: str, year: int) -> dict[str, float]:
        """Metas fijas por sede (código de sede -> meta) de un indicador en un año."""
        catalog = self.catalog()
        return {
            site.code: target
            for site in catalog.ordered_sites()
            if (target := catalog.target(indicator_code, year, site.code)) is not None
        }

    def sheet(self, indicator_code: str, year: int) -> IndicatorSheetView:
        """Ficha técnica del indicador con la meta, el peso y las metas por sede del año."""
        catalog = self.catalog()
        indicator = catalog.indicator(indicator_code)
        goals = tuple(
            (catalog.site(code).name, target) for code, target in self.site_goals(indicator_code, year).items()
        )
        return IndicatorSheetView(
            indicator=indicator,
            program=catalog.program(indicator.program_code),
            year=year,
            rule=catalog.rule(indicator_code, year),
            site_goals=goals,
        )

    def validate(self, year: int | None = None) -> list[str]:
        """Problemas de coherencia del catálogo (pesos, metas por sede, referencias)."""
        return validate_catalog(self.catalog(), year)
