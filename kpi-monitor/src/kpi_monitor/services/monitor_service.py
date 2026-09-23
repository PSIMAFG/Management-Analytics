"""Caso de uso principal: evaluar los indicadores de un período y entregar vistas para la interfaz."""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path

from kpi_monitor.data.db import open_db
from kpi_monitor.data.repositories import ObservationRepository
from kpi_monitor.domain.alerts import Alert, mark_new
from kpi_monitor.domain.engine import EngineConfig, PeriodEvaluation, evaluate_period
from kpi_monitor.domain.enums import Severity
from kpi_monitor.domain.index import WeightedIndex, index_by_month
from kpi_monitor.domain.models import MonitorSettings, Period, Thresholds
from kpi_monitor.domain.progress import ProgressFn
from kpi_monitor.domain.quality import DataQuality, assess_quality
from kpi_monitor.domain.results import NETWORK_LABEL, IndicatorResult
from kpi_monitor.domain.summary import MonitorSummary, StatusMatrix
from kpi_monitor.errors import ValidationError
from kpi_monitor.services.catalog_service import CatalogService

log = logging.getLogger(__name__)

# Años hacia atrás que se leen: el año anterior da la referencia de las metas interanuales y el
# previo a ese permite arrastrar el último corte de stock.
YEARS_BACK = 3


class MonitorService:
    """Evalúa períodos y guarda en memoria el resultado de cada uno hasta que cambian los datos."""

    def __init__(self, db_path: Path, catalog: CatalogService) -> None:
        self.db_path = db_path
        self.catalog_service = catalog
        self._lock = threading.Lock()
        self._raw: dict[Period, PeriodEvaluation] = {}
        self._final: dict[Period, PeriodEvaluation] = {}
        # Cambia en cada invalidación. Una evaluación que empezó antes de un cambio de datos o de
        # parámetros entrega su resultado a quien la pidió, pero no lo deja guardado en memoria.
        self._generation = 0

    def invalidate(self) -> None:
        """Descarta las evaluaciones guardadas (se llama después de importar datos o cambiar parámetros)."""
        with self._lock:
            self._raw.clear()
            self._final.clear()
            self._generation += 1
        log.info("Evaluaciones en memoria descartadas")

    def _store(
        self, cache: dict[Period, PeriodEvaluation], period: Period, value: PeriodEvaluation, generation: int
    ) -> None:
        with self._lock:
            if generation == self._generation:
                cache[period] = value
            else:
                log.info("Los datos cambiaron mientras se evaluaba %s; el resultado no se guarda", period.label)

    def is_evaluated(self, period: Period) -> bool:
        """Indica si la evaluación del período ya está en memoria (consultarla no recalcula nada)."""
        with self._lock:
            return period in self._final

    def update_settings(self, thresholds: Thresholds, prevalence: float) -> MonitorSettings:
        """Guarda los umbrales del semáforo y la prevalencia, y descarta las evaluaciones anteriores.

        Los valores se validan al construir los parámetros (umbral amarillo menor que el verde,
        prevalencia entre 0 % y 100 %); si no cumplen, se lanza ValidationError sin guardar nada.
        """
        settings = replace(self.catalog_service.settings(), thresholds=thresholds, prevalence=prevalence)
        self.catalog_service.save_settings(settings)
        self.invalidate()
        log.info(
            "Parámetros actualizados: umbrales %s y %s, prevalencia %s", thresholds.green, thresholds.yellow, prevalence
        )
        return settings

    def default_period(self) -> Period:
        """Período de corte configurado; nunca se toma de la fecha del sistema."""
        return self.catalog_service.settings().default_period

    def last_month_with_data(self, year: int) -> int | None:
        """Último mes del año con alguna observación reportada, o None si el año no tiene datos."""
        with open_db(self.db_path) as conn:
            return ObservationRepository(conn).last_month_with_data(year)

    def _evaluate_raw(
        self, period: Period, progress: ProgressFn | None, cancel: threading.Event | None, generation: int
    ) -> PeriodEvaluation:
        with self._lock:
            cached = self._raw.get(period)
        if cached is not None:
            return cached
        catalog = self.catalog_service.catalog()
        config = EngineConfig.from_settings(self.catalog_service.settings())
        with open_db(self.db_path) as conn:
            observations = ObservationRepository(conn).find(years=range(period.year - YEARS_BACK, period.year + 1))
        evaluation = evaluate_period(
            catalog, observations, period, config, progress, cancel.is_set if cancel is not None else None
        )
        self._store(self._raw, period, evaluation, generation)
        return evaluation

    def evaluate(
        self, period: Period, progress: ProgressFn | None = None, cancel: threading.Event | None = None
    ) -> PeriodEvaluation:
        """Evaluación completa del período, con las alertas marcadas como nuevas respecto del mes anterior."""
        with self._lock:
            cached = self._final.get(period)
            generation = self._generation
        if cached is not None:
            return cached
        catalog = self.catalog_service.catalog()
        if period.year not in catalog.years:
            raise ValidationError(f"El año {period.year} no tiene metas configuradas.")

        def scaled(start: int, span: int) -> ProgressFn | None:
            if progress is None:
                return None
            return lambda pct, msg: progress(start + pct * span // 100, msg)

        current = self._evaluate_raw(period, scaled(0, 80), cancel, generation)
        previous_period = period.previous()
        previous_alerts: list[Alert] = []
        if previous_period.year in catalog.years:
            previous_alerts = list(self._evaluate_raw(previous_period, scaled(80, 20), cancel, generation).alerts)
        final = replace(current, alerts=tuple(mark_new(current.alerts, previous_alerts)))
        self._store(self._final, period, final, generation)
        if progress is not None:
            progress(100, "Evaluación terminada")
        return final

    def results(
        self, period: Period, site_code: str | None = None, program_code: str | None = None
    ) -> list[IndicatorResult]:
        """Resultados de un nivel (None = red) para todos los indicadores vigentes o los de un programa."""
        return self.evaluate(period).results(site_code, program_code)

    def result(self, period: Period, indicator_code: str, site_code: str | None = None) -> IndicatorResult:
        """Resultado de un indicador en un nivel, con serie mensual, cortes y proyección."""
        return self.evaluate(period).result(indicator_code, site_code)

    def site_comparison(self, period: Period, indicator_code: str) -> list[IndicatorResult]:
        """Red y sedes del indicador, evaluadas con la misma regla (primero la red)."""
        return list(self.evaluate(period).evaluation(indicator_code).all_results)

    def status_matrix(self, period: Period, program_code: str | None = None) -> StatusMatrix:
        """Matriz de semáforo indicador × nivel (red y sedes)."""
        evaluation = self.evaluate(period)
        catalog = evaluation.catalog
        evaluations = [
            e for e in evaluation.evaluations if program_code is None or e.indicator.program_code == program_code
        ]
        levels: list[tuple[str | None, str]] = [(None, NETWORK_LABEL)]
        levels.extend((site.code, site.name) for site in catalog.ordered_sites())
        cells = {(e.indicator.code, r.site_code): r for e in evaluations for r in e.all_results}
        return StatusMatrix(tuple(e.indicator for e in evaluations), tuple(levels), cells)

    def index(self, period: Period, program_code: str, site_code: str | None = None) -> WeightedIndex:
        """Índice ponderado de un programa en un nivel (None = red), a la fecha y proyectado al cierre."""
        return self.evaluate(period).index(program_code, site_code)

    def indexes(self, period: Period, site_code: str | None = None) -> list[WeightedIndex]:
        """Índice ponderado de cada programa vigente para un nivel."""
        return self.evaluate(period).indexes_for(site_code)

    def index_history(self, period: Period, program_code: str, site_code: str | None = None) -> list[float | None]:
        """Índice ponderado a la fecha de cada mes del año hasta el mes de corte."""
        evaluation = self.evaluate(period)
        return index_by_month(evaluation.results(site_code, program_code), program_code, site_code)

    def alerts(
        self, period: Period, program_code: str | None = None, site_code: str | None = None, *, all_levels: bool = True
    ) -> list[Alert]:
        """Alertas del período. Con `all_levels` se incluyen la red y todas las sedes; si no, solo `site_code`."""
        site_codes = None if all_levels else [site_code]
        return self.evaluate(period).alerts_for(program_code, site_codes)

    def summary(self, period: Period, site_code: str | None = None, program_code: str | None = None) -> MonitorSummary:
        """Totales para la franja superior: índice, proyección, conteos por estado y alertas.

        Sin programa se informa el índice del primer programa vigente (el de compromisos de gestión).
        En la red se cuentan las alertas de la red y de todas las sedes; en una sede, solo las suyas.
        """
        evaluation = self.evaluate(period)
        indexes = evaluation.indexes_for(site_code)
        index = next((i for i in indexes if i.program_code == program_code), None) if program_code else None
        if index is None and program_code is None and indexes:
            index = indexes[0]
        alerts = evaluation.alerts_for(program_code, None if site_code is None else [site_code])
        return MonitorSummary(
            period=period,
            site_code=site_code,
            program_code=program_code,
            index=index,
            counts=evaluation.status_counts(site_code, program_code),
            alerts=len(alerts),
            new_alerts=sum(1 for a in alerts if a.is_new),
            critical_alerts=sum(1 for a in alerts if a.severity is Severity.CRITICAL),
        )

    def data_quality(
        self, period: Period, site_code: str | None = None, program_code: str | None = None
    ) -> DataQuality:
        """Datos esperados, informados y faltantes del año hasta el mes de corte, y anomalías.

        Con `site_code` se cuenta solo esa sede (None = todas las sedes) y con `program_code`,
        solo ese programa: el mismo alcance que los totales y las pestañas.
        """
        return assess_quality(self.evaluate(period), site_code, program_code)
