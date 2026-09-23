"""Generador reproducible de observaciones sintéticas.

Produce tres años de datos (dos completos y el año en curso hasta agosto) para
24 indicadores y 5 sedes. Los supuestos buscan conservar los patrones de un
caso real sin reproducir ninguna cifra:

- Volúmenes proporcionales al tamaño de cada sede.
- Estacionalidad: caída marcada en febrero y en diciembre, alza en otoño y primavera.
- Niveles de cumplimiento variados: sedes y indicadores en verde, amarillo y rojo.
- Stocks (personas en seguimiento o en acompañamiento) declarados solo en los meses de corte.
- Casos de borde reales: meses no informados, ceros de subregistro, un dato imposible,
  una referencia del año anterior en cero (meta no determinable) y una sede sin plan cargado.

Cada serie (indicador, sede, año) usa su propio generador con semilla derivada,
de modo que el resultado es idéntico en cada ejecución.
"""

from __future__ import annotations

import math
import random
import zlib
from collections.abc import Callable
from dataclasses import replace

from kpi_monitor.data.catalog_seed import ABSOLUTE_GOALS, INDICATORS, SITE_TARGETS, SITES, YEARS, build_catalog
from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Origin
from kpi_monitor.domain.models import Observation, Period

SEED = 2026
CURRENT_PERIOD = Period(2026, 8)
PREVALENCE = 0.20
SITE_ORDER = tuple(site.code for site in SITES)
# Tamaño relativo de cada sede (volumen de actividad), acorde con su población de referencia.
SITE_SIZE = {"NOR": 1.32, "SUR": 0.84, "CEN": 1.07, "ORI": 0.76, "PON": 0.98}

_RAW_SEASON = (0.88, 0.62, 1.05, 1.08, 1.12, 1.02, 0.98, 1.06, 1.02, 1.10, 1.08, 0.79)
SEASONALITY = tuple(v * 12 / sum(_RAW_SEASON) for v in _RAW_SEASON)

# Nivel del año en curso por sede (Norte, Sur, Centro, Oriente, Poniente). Según el indicador es
# un factor de logro frente a la meta, una proporción, un promedio de días o una tasa anual.
LEVEL_CURRENT: dict[str, tuple[float | None, ...]] = {
    "CG01": (1.08, 1.02, 0.97, 0.92, 0.88),
    "CG02": (0.95, 0.90, 0.935, 0.855, 0.78),
    "CG03": (0.82, 0.74, 0.79, 0.665, 0.59),
    "CG04": (1.12, None, 0.78, None, None),
    "CG06": (16.0, 21.0, 27.0, 18.0, 38.0),
    "CG08": (0.82, 0.69, 0.78, 0.75, 0.645),
    "CG09": (0.86, 0.88, 0.82, 0.84, 0.77),
    "CG10": (0.96, 0.88, 0.93, 0.75, 0.84),
    "CG11": (1.05, 0.95, 0.88, 1.10, 0.80),
    "CG12": (0.85, 0.80, 0.95, 0.90, 0.84),
    "CG13": (1.15, 1.05, 0.95, 0.85, 1.10),
    "CG14": (1.05, 0.98, 1.10, 0.95, 1.00),
    "CG15": (1.10, 1.02, 0.97, 1.12, 0.84),
    "CG16": (0.90, 0.84, 0.78, None, 0.945),
    "PC01": (0.42, 0.38, 0.45, 0.35, 0.40),
    "PC02": (0.30, 0.245, 0.215, 0.28, 0.26),
    "PC03": (0.76, 0.68, 0.73, 0.57, 0.66),
    "PC04": (1.78, 1.49, 1.63, 1.32, 1.02),
    "PC05": (0.93, 0.83, 1.00, 0.70, 0.89),
    "IA02": (12.2, 10.8, 11.2, 9.2, 12.5),
    "IA03": (0.12, 0.095, 0.115, 0.085, 0.11),
}
# Series con trayectoria definida año a año porque de ellas dependen metas interanuales o coberturas.
CONCENTRATION = {  # controles por persona en seguimiento al año
    2024: (6.8, 10.9, 7.6, 6.2, 9.1),
    2025: (7.2, 11.5, 8.0, 6.4, 9.8),
    2026: (8.3, 10.2, 8.1, 6.3, 9.4),
}
GROUP_SHARE = {  # proporción de intervenciones grupales; Poniente no registró ninguna en 2025
    2024: (0.072, 0.081, 0.078, 0.069, 0.055),
    2025: (0.083, 0.092, 0.088, 0.078, 0.0),
    2026: (0.097, 0.104, 0.098, 0.096, 0.070),
}
COVERAGE = {  # personas en seguimiento sobre la población esperada
    2024: (0.228, 0.214, 0.232, 0.207, 0.196),
    2025: (0.238, 0.226, 0.238, 0.216, 0.192),
    2026: (0.250, 0.235, 0.245, 0.225, 0.190),
}
# Volumen mensual base del denominador por sede de tamaño 1 (flujo sobre flujo).
BASE_VOLUME = {
    "CG02": 160.0, "CG03": 22.0, "CG06": 14.0, "CG07": 700.0, "CG08": 26.0, "CG09": 19.0,
    "PC01": 8.0, "PC02": 14.0, "PC03": 11.0, "PC04": 11.0,
}  # fmt: skip
# Metas absolutas de los indicadores cuyo nivel se expresa como factor de logro frente a la meta.
GOAL_FACTOR = {code: ABSOLUTE_GOALS[code] for code in ("CG01", "CG04", "CG11", "CG12", "CG13", "CG14", "CG15")}

# Casos de borde que se inyectan sobre los datos generados.
NOT_REPORTED = (
    ("ORI", 2026, 8, ("CG01", "CG02", "CG03", "CG06")),
    ("SUR", 2026, 3, ("CG08", "CG09")),
    ("CEN", 2025, 11, ("CG15",)),
    ("NOR", 2024, 7, ("CG02", "CG03")),
)
UNDERREPORTED = (("SUR", 2026, 6, "CG12"), ("PON", 2026, 5, "CG03"))
IMPOSSIBLE = (("CEN", 2026, 4, "CG02", 9.0),)
NO_PLAN = (("ORI", 2026, "CG16"),)
LOADED_AT = "2026-09-01T08:00:00"


class SeriesRandom(random.Random):
    """Generador con las distribuciones discretas que se necesitan (solo biblioteca estándar)."""

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 40:
            return max(0, round(self.gauss(lam, math.sqrt(lam))))
        limit = math.exp(-lam)
        count, product = 0, self.random()
        while product > limit:
            count += 1
            product *= self.random()
        return count

    def binomial(self, n: int, p: float) -> int:
        p = min(1.0, max(0.0, p))
        if n <= 0 or p == 0:
            return 0
        if p == 1:
            return n
        if n > 60:
            mean = n * p
            return min(n, max(0, round(self.gauss(mean, math.sqrt(mean * (1 - p))))))
        return sum(1 for _ in range(n) if self.random() < p)


def _rng(seed: int, *parts: object) -> SeriesRandom:
    text = ":".join(str(p) for p in (seed, *parts))
    return SeriesRandom(zlib.crc32(text.encode("utf-8")))


def _level(code: str, site_index: int, year: int, seed: int) -> float | None:
    """Nivel del indicador en un año: el del año en curso con una deriva hacia atrás."""
    current = LEVEL_CURRENT[code][site_index]
    if current is None or year == CURRENT_PERIOD.year:
        return current
    drift = _rng(seed, "drift", code, site_index, year)
    if code == "CG06":
        return current * drift.uniform(0.95, 1.2)
    factor = drift.uniform(0.9, 1.04) ** (CURRENT_PERIOD.year - year)
    value = current * factor
    catalog_indicator = next(ind for ind in INDICATORS if ind.code == code)
    if catalog_indicator.natural_ceiling is not None:
        value = min(value, catalog_indicator.natural_ceiling * 0.99)
    return value


class _Context:
    """Datos compartidos por los generadores de cada indicador."""

    def __init__(self, catalog: Catalog, seed: int) -> None:
        self.catalog = catalog
        self.seed = seed

    def last_month(self, year: int) -> int:
        return CURRENT_PERIOD.month if year == CURRENT_PERIOD.year else 12

    def follow_up_stock(self, site_index: int, year: int, month: int) -> float:
        """Personas en seguimiento: cobertura por población esperada, creciendo dentro del año."""
        site = SITE_ORDER[site_index]
        population = self.catalog.population(site, year) or 0
        base = COVERAGE[year][site_index] * PREVALENCE * population
        return base * (0.975 + 0.05 * month / 12)

    def accompaniment_stock(self, site_index: int, year: int, month: int) -> float:
        site = SITE_ORDER[site_index]
        fill = _level("CG10", site_index, year, self.seed) or 0.0
        return SITE_TARGETS["CG10"][site] * fill * ABSOLUTE_GOALS["CG10"] * (0.97 + 0.06 * month / 12)

    def linked_stock(self, site_index: int, year: int, month: int) -> float:
        """Población vinculada: algo mayor que el compromiso de control integral del año."""
        site = SITE_ORDER[site_index]
        commitment = self.catalog.target("CG14", year, site) or float(SITE_TARGETS["CG14"][site])
        return commitment * 1.3 * (0.98 + 0.03 * month / 12)


def _obs(code: str, site: str, year: int, month: int, num: float | None, den: float | None) -> Observation:
    origin = Origin.MANUAL if code == "CG16" else Origin.SYNTHETIC
    return Observation(code, site, year, month, num, den, True, origin)


def _flow_ratio(ctx: _Context, code: str, site_index: int, year: int) -> list[Observation]:
    """Numerador y denominador de flujo: proporciones, intensidad y días de espera."""
    site = SITE_ORDER[site_index]
    rng = _rng(ctx.seed, code, site, year)
    size = SITE_SIZE[site]
    level = GROUP_SHARE[year][site_index] if code == "CG07" else _level(code, site_index, year, ctx.seed)
    assert level is not None
    rows = []
    for month in range(1, ctx.last_month(year) + 1):
        season = SEASONALITY[month - 1]
        den = rng.poisson(BASE_VOLUME[code] * size * season)
        if code == "CG06":
            num = round(den * level * rng.uniform(0.85, 1.15))
        elif code == "PC04":
            num = rng.poisson(den * level)
        else:
            num = rng.binomial(den, level * rng.uniform(0.96, 1.03) if level < 1 else level)
        rows.append(_obs(code, site, year, month, float(num), float(den)))
    return rows


def _fixed_flow(ctx: _Context, code: str, site_index: int, year: int) -> list[Observation]:
    """Numerador de flujo contra una meta fija de la sede."""
    site = SITE_ORDER[site_index]
    target = ctx.catalog.target(code, year, site)
    level = _level(code, site_index, year, ctx.seed)
    if target is None or level is None:
        return []
    rng = _rng(ctx.seed, code, site, year)
    expected = target * GOAL_FACTOR[code] * level / 12
    return [
        _obs(code, site, year, m, float(rng.poisson(expected * SEASONALITY[m - 1])), None)
        for m in range(1, ctx.last_month(year) + 1)
    ]


def _flow_over_stock(ctx: _Context, code: str, site_index: int, year: int) -> list[Observation]:
    """Numerador de flujo sobre un stock de personas declarado solo en los meses de corte."""
    site = SITE_ORDER[site_index]
    indicator = ctx.catalog.indicator(code)
    rng = _rng(ctx.seed, code, site, year)
    stock_fn: Callable[[int, int, int], float]
    if code in ("CG11", "CG12", "CG13"):
        stock_fn = ctx.accompaniment_stock
        level = _level(code, site_index, year, ctx.seed) or 0.0
        annual_rate = indicator.multiplier * GOAL_FACTOR[code] * level
    elif code == "CG15":
        stock_fn = ctx.linked_stock
        annual_rate = GOAL_FACTOR[code] * (_level(code, site_index, year, ctx.seed) or 0.0)
    elif code == "CG05":
        stock_fn = ctx.follow_up_stock
        annual_rate = CONCENTRATION[year][site_index]
    else:
        stock_fn = ctx.follow_up_stock
        annual_rate = _level(code, site_index, year, ctx.seed) or 0.0
    rows = []
    for month in range(1, ctx.last_month(year) + 1):
        stock = stock_fn(site_index, year, month)
        num = rng.poisson(stock * annual_rate * SEASONALITY[month - 1] / 12)
        declared = float(round(stock * (1 + rng.gauss(0, 0.006)))) if month in indicator.stock_months else None
        rows.append(_obs(code, site, year, month, float(num), declared))
    return rows


def _stock_numerator(ctx: _Context, code: str, site_index: int, year: int) -> list[Observation]:
    """Numerador de stock, informado solo en los meses de corte."""
    site = SITE_ORDER[site_index]
    indicator = ctx.catalog.indicator(code)
    rng = _rng(ctx.seed, code, site, year)
    rows = []
    for month in indicator.stock_months:
        if month > ctx.last_month(year):
            break
        if code == "IA01":
            value = ctx.follow_up_stock(site_index, year, month)
        elif code == "CG10":
            value = ctx.accompaniment_stock(site_index, year, month)
        else:
            target = ctx.catalog.target(code, year, site) or 0.0
            level = _level(code, site_index, year, ctx.seed) or 0.0
            factor = GOAL_FACTOR.get(code, 1.0)
            value = target * factor * level * (0.97 + 0.03 * month / 12)
        rows.append(_obs(code, site, year, month, float(max(0, round(value * (1 + rng.gauss(0, 0.01))))), None))
    return rows


def _manual_plan(ctx: _Context, code: str, site_index: int, year: int) -> list[Observation]:
    """Plan con carga manual: acciones programadas y ejecutadas por mes."""
    site = SITE_ORDER[site_index]
    level = _level(code, site_index, year, ctx.seed)
    if level is None:
        return []
    rng = _rng(ctx.seed, code, site, year)
    rows = []
    for month in range(1, ctx.last_month(year) + 1):
        programmed = rng.poisson(5 * SITE_SIZE[site]) + 1
        rows.append(_obs(code, site, year, month, float(rng.binomial(programmed, level)), float(programmed)))
    return rows


def _generator(code: str, catalog: Catalog) -> Callable[[_Context, str, int, int], list[Observation]]:
    indicator = catalog.indicator(code)
    kind = indicator.denominator_type
    if kind is DenominatorType.MANUAL:
        return _manual_plan
    if indicator.aggregation is Aggregation.STOCK:
        return _stock_numerator
    if kind is DenominatorType.FIXED_SITE:
        return _fixed_flow
    if kind in (DenominatorType.STOCK, DenominatorType.K_STOCK):
        return _flow_over_stock
    return _flow_ratio


def _apply_edge_cases(observations: list[Observation]) -> list[Observation]:
    by_key = {obs.key: obs for obs in observations}
    for site, year, month, codes in NOT_REPORTED:
        for code in codes:
            key = (code, site, year, month)
            if key in by_key:
                by_key[key] = replace(by_key[key], numerator=None, denominator=None, reported=False)
    for site, year, month, code in UNDERREPORTED:
        key = (code, site, year, month)
        by_key[key] = replace(by_key[key], numerator=0.0)
    for site, year, month, code, excess in IMPOSSIBLE:
        key = (code, site, year, month)
        obs = by_key[key]
        by_key[key] = replace(obs, numerator=(obs.denominator or 0.0) + excess)
    no_plan = {(code, site, year) for site, year, code in NO_PLAN}
    return [obs for key, obs in by_key.items() if (key[0], key[1], key[2]) not in no_plan]


def generate_observations(catalog: Catalog | None = None, seed: int = SEED) -> list[Observation]:
    """Observaciones sintéticas de todos los indicadores vigentes, ordenadas por clave."""
    catalog = catalog or build_catalog()
    ctx = _Context(catalog, seed)
    observations: list[Observation] = []
    for year in YEARS:
        for indicator in catalog.active_indicators(year):
            generate = _generator(indicator.code, catalog)
            for site_index in range(len(SITE_ORDER)):
                observations.extend(generate(ctx, indicator.code, site_index, year))
    observations = _apply_edge_cases(observations)
    return sorted(observations, key=lambda o: (o.year, o.month, o.indicator_code, o.site_code))
