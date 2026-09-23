"""Repositorios: traducen filas de SQLite a objetos del dominio y viceversa."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence

from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Direction, GoalRuleType, Origin, Scale
from kpi_monitor.domain.models import (
    GoalBand,
    GoalRule,
    Indicator,
    IndicatorSheet,
    MonitorSettings,
    Observation,
    Period,
    Program,
    Site,
    SiteGoal,
    SitePopulation,
    Thresholds,
)
from kpi_monitor.errors import DataError


def _months_to_text(months: Sequence[int]) -> str:
    return ",".join(str(m) for m in months)


def _months_from_text(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(",") if part.strip())


class CatalogRepository:
    """Lectura y escritura del catálogo (programas, sedes, indicadores, metas y poblaciones)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def programs(self) -> list[Program]:
        rows = self.conn.execute("SELECT * FROM program ORDER BY sort_order, code")
        return [Program(r["code"], r["name"], r["description"], r["sort_order"]) for r in rows]

    def sites(self) -> list[Site]:
        rows = self.conn.execute("SELECT * FROM site ORDER BY sort_order, code")
        return [Site(r["code"], r["name"], r["kind"], r["sort_order"]) for r in rows]

    def indicators(self) -> list[Indicator]:
        rows = self.conn.execute("SELECT * FROM indicator ORDER BY sort_order, code")
        return [self._indicator(r) for r in rows]

    @staticmethod
    def _indicator(row: sqlite3.Row) -> Indicator:
        return Indicator(
            code=row["code"],
            program_code=row["program_code"],
            name=row["name"],
            short_name=row["short_name"],
            aggregation=Aggregation(row["aggregation"]),
            denominator_type=DenominatorType(row["denominator_type"]),
            direction=Direction(row["direction"]),
            scale=Scale(row["scale"]),
            sheet=IndicatorSheet(
                measures=row["measures"],
                numerator=row["numerator_desc"],
                denominator=row["denominator_desc"],
                source=row["source"],
                zero_meaning=row["zero_meaning"],
                notes=row["notes"],
            ),
            multiplier=row["multiplier"],
            natural_ceiling=row["natural_ceiling"],
            stock_months=_months_from_text(row["stock_months"]),
            cut_months=_months_from_text(row["cut_months"]),
            sort_order=row["sort_order"],
        )

    def goal_rules(self) -> list[GoalRule]:
        bands: dict[tuple[str, int], list[GoalBand]] = defaultdict(list)
        for row in self.conn.execute("SELECT * FROM goal_band ORDER BY indicator_code, year, band_order"):
            bands[(row["indicator_code"], row["year"])].append(GoalBand(row["upper_bound"], row["compliance"]))
        rules = []
        for row in self.conn.execute("SELECT * FROM goal_rule ORDER BY indicator_code, year"):
            rules.append(
                GoalRule(
                    indicator_code=row["indicator_code"],
                    year=row["year"],
                    rule_type=GoalRuleType(row["rule_type"]),
                    weight=row["weight"],
                    value=row["value"],
                    factor=row["factor"],
                    ceiling=row["ceiling"],
                    bands=tuple(bands.get((row["indicator_code"], row["year"]), ())),
                    source=row["source"],
                )
            )
        return rules

    def site_goals(self) -> list[SiteGoal]:
        rows = self.conn.execute("SELECT * FROM site_goal ORDER BY indicator_code, year, site_code")
        return [SiteGoal(r["indicator_code"], r["year"], r["site_code"], r["target"]) for r in rows]

    def populations(self) -> list[SitePopulation]:
        rows = self.conn.execute("SELECT * FROM site_population ORDER BY site_code, year")
        return [SitePopulation(r["site_code"], r["year"], r["population"]) for r in rows]

    def load(self) -> Catalog:
        """Catálogo completo en un solo objeto inmutable."""
        return Catalog(
            programs=tuple(self.programs()),
            sites=tuple(self.sites()),
            indicators=tuple(self.indicators()),
            rules=tuple(self.goal_rules()),
            site_goals=tuple(self.site_goals()),
            populations=tuple(self.populations()),
        )

    def add_program(self, program: Program) -> None:
        self.conn.execute(
            "INSERT INTO program (code, name, description, sort_order) VALUES (?, ?, ?, ?)",
            (program.code, program.name, program.description, program.sort_order),
        )

    def add_site(self, site: Site) -> None:
        self.conn.execute(
            "INSERT INTO site (code, name, kind, sort_order) VALUES (?, ?, ?, ?)",
            (site.code, site.name, site.kind, site.sort_order),
        )

    def add_indicator(self, ind: Indicator) -> None:
        self.conn.execute(
            """
            INSERT INTO indicator (
                code, program_code, name, short_name, sort_order, aggregation, denominator_type, multiplier,
                direction, scale, natural_ceiling, stock_months, cut_months, measures, numerator_desc,
                denominator_desc, source, zero_meaning, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ind.code,
                ind.program_code,
                ind.name,
                ind.short_name,
                ind.sort_order,
                ind.aggregation.value,
                ind.denominator_type.value,
                ind.multiplier,
                ind.direction.value,
                ind.scale.value,
                ind.natural_ceiling,
                _months_to_text(ind.stock_months),
                _months_to_text(ind.cut_months),
                ind.sheet.measures,
                ind.sheet.numerator,
                ind.sheet.denominator,
                ind.sheet.source,
                ind.sheet.zero_meaning,
                ind.sheet.notes,
            ),
        )

    def add_goal_rule(self, rule: GoalRule) -> None:
        self.conn.execute(
            """
            INSERT INTO goal_rule (indicator_code, year, rule_type, value, factor, ceiling, weight, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule.indicator_code,
                rule.year,
                rule.rule_type.value,
                rule.value,
                rule.factor,
                rule.ceiling,
                rule.weight,
                rule.source,
            ),
        )
        self.conn.executemany(
            "INSERT INTO goal_band (indicator_code, year, band_order, upper_bound, compliance) VALUES (?, ?, ?, ?, ?)",
            [
                (rule.indicator_code, rule.year, order, band.upper_bound, band.compliance)
                for order, band in enumerate(rule.bands, start=1)
            ],
        )

    def add_site_goal(self, goal: SiteGoal) -> None:
        self.conn.execute(
            "INSERT INTO site_goal (indicator_code, year, site_code, target) VALUES (?, ?, ?, ?)",
            (goal.indicator_code, goal.year, goal.site_code, goal.target),
        )

    def add_population(self, population: SitePopulation) -> None:
        self.conn.execute(
            "INSERT INTO site_population (site_code, year, population) VALUES (?, ?, ?)",
            (population.site_code, population.year, population.population),
        )

    def save(self, catalog: Catalog) -> None:
        """Inserta un catálogo completo (se usa al crear la base)."""
        for program in catalog.programs:
            self.add_program(program)
        for site in catalog.sites:
            self.add_site(site)
        for population in catalog.populations:
            self.add_population(population)
        for indicator in catalog.indicators:
            self.add_indicator(indicator)
        for rule in catalog.rules:
            self.add_goal_rule(rule)
        for goal in catalog.site_goals:
            self.add_site_goal(goal)


class ObservationRepository:
    """Observaciones mensuales por indicador y sede."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _observation(row: sqlite3.Row) -> Observation:
        return Observation(
            indicator_code=row["indicator_code"],
            site_code=row["site_code"],
            year=row["year"],
            month=row["month"],
            numerator=row["numerator"],
            denominator=row["denominator"],
            reported=bool(row["reported"]),
            origin=Origin(row["origin"]),
        )

    def find(
        self,
        *,
        years: Iterable[int] | None = None,
        month: int | None = None,
        site_code: str | None = None,
        indicator_codes: Iterable[str] | None = None,
    ) -> list[Observation]:
        """Observaciones filtradas, ordenadas por año, mes, indicador y sede."""
        clauses: list[str] = []
        params: list[object] = []
        if years is not None:
            year_list = list(years)
            if not year_list:
                return []
            clauses.append(f"year IN ({','.join('?' * len(year_list))})")
            params.extend(year_list)
        if month is not None:
            clauses.append("month = ?")
            params.append(month)
        if site_code is not None:
            clauses.append("site_code = ?")
            params.append(site_code)
        if indicator_codes is not None:
            codes = list(indicator_codes)
            if not codes:
                return []
            clauses.append(f"indicator_code IN ({','.join('?' * len(codes))})")
            params.extend(codes)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM observation{where} ORDER BY year, month, indicator_code, site_code"
        return [self._observation(row) for row in self.conn.execute(query, params)]

    def existing_keys(self) -> set[tuple[str, str, int, int]]:
        rows = self.conn.execute("SELECT indicator_code, site_code, year, month FROM observation")
        return {(r[0], r[1], r[2], r[3]) for r in rows}

    def upsert_many(self, observations: Iterable[Observation], loaded_at: str) -> tuple[int, int]:
        """Inserta o reemplaza observaciones. Devuelve (nuevas, reemplazadas)."""
        existing = self.existing_keys()
        inserted = replaced = 0
        rows = []
        for obs in observations:
            if obs.key in existing:
                replaced += 1
            else:
                inserted += 1
            rows.append(
                (
                    obs.indicator_code,
                    obs.site_code,
                    obs.year,
                    obs.month,
                    obs.numerator,
                    obs.denominator,
                    int(obs.reported),
                    obs.origin.value,
                    loaded_at,
                )
            )
        try:
            self.conn.executemany(
                """
                INSERT INTO observation (
                    indicator_code, site_code, year, month, numerator, denominator, reported, origin, loaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (indicator_code, site_code, year, month) DO UPDATE SET
                    numerator = excluded.numerator,
                    denominator = excluded.denominator,
                    reported = excluded.reported,
                    origin = excluded.origin,
                    loaded_at = excluded.loaded_at
                """,
                rows,
            )
        except sqlite3.IntegrityError as error:
            raise DataError("Una observación no cumple las reglas de la base de datos.") from error
        return inserted, replaced

    def years(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT year FROM observation ORDER BY year")]

    def last_month_with_data(self, year: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(month) FROM observation WHERE year = ? AND reported = 1", (year,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0])


class SettingsRepository:
    """Parámetros de cálculo guardados como pares clave-valor."""

    KEYS = (
        "green_threshold",
        "yellow_threshold",
        "prevalence",
        "default_year",
        "default_month",
        "bootstrap_draws",
        "bootstrap_seed",
        "min_months_projection",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def raw(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM setting")}

    def set(self, key: str, value: str) -> None:
        if key not in self.KEYS:
            raise DataError(f"El parámetro {key} no existe.")
        self.conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def save(self, settings: MonitorSettings) -> None:
        values = {
            "green_threshold": settings.thresholds.green,
            "yellow_threshold": settings.thresholds.yellow,
            "prevalence": settings.prevalence,
            "default_year": settings.default_period.year,
            "default_month": settings.default_period.month,
            "bootstrap_draws": settings.bootstrap_draws,
            "bootstrap_seed": settings.bootstrap_seed,
            "min_months_projection": settings.min_months_projection,
        }
        for key, value in values.items():
            self.set(key, str(value))

    def load(self) -> MonitorSettings:
        """Lee los parámetros; los que falten toman el valor por defecto."""
        raw = self.raw()
        defaults = MonitorSettings()
        try:
            return MonitorSettings(
                thresholds=Thresholds(
                    green=float(raw.get("green_threshold", defaults.thresholds.green)),
                    yellow=float(raw.get("yellow_threshold", defaults.thresholds.yellow)),
                ),
                prevalence=float(raw.get("prevalence", defaults.prevalence)),
                default_period=Period(
                    int(raw.get("default_year", defaults.default_period.year)),
                    int(raw.get("default_month", defaults.default_period.month)),
                ),
                bootstrap_draws=int(raw.get("bootstrap_draws", defaults.bootstrap_draws)),
                bootstrap_seed=int(raw.get("bootstrap_seed", defaults.bootstrap_seed)),
                min_months_projection=int(raw.get("min_months_projection", defaults.min_months_projection)),
            )
        except ValueError as error:
            raise DataError("Un parámetro guardado en la base no tiene un formato válido.") from error
