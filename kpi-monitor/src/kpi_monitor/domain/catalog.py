"""Catálogo completo: programas, sedes, indicadores, reglas de meta, metas por sede y poblaciones.

El catálogo es la única fuente de metas y pesos. Todas las vistas y reportes
consultan las mismas reglas a través de este objeto.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property

from kpi_monitor.domain.enums import DenominatorType, Direction, GoalRuleType
from kpi_monitor.domain.models import GoalRule, Indicator, Program, Site, SiteGoal, SitePopulation
from kpi_monitor.domain.results import NETWORK_LABEL
from kpi_monitor.domain.text import format_pct
from kpi_monitor.errors import ValidationError

WEIGHT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Catalog:
    """Configuración vigente del monitor, con búsquedas por código."""

    programs: tuple[Program, ...]
    sites: tuple[Site, ...]
    indicators: tuple[Indicator, ...]
    rules: tuple[GoalRule, ...]
    site_goals: tuple[SiteGoal, ...] = ()
    populations: tuple[SitePopulation, ...] = ()

    @cached_property
    def _programs(self) -> dict[str, Program]:
        return {p.code: p for p in self.programs}

    @cached_property
    def _sites(self) -> dict[str, Site]:
        return {s.code: s for s in self.sites}

    @cached_property
    def _indicators(self) -> dict[str, Indicator]:
        return {i.code: i for i in self.indicators}

    @cached_property
    def _rules(self) -> dict[tuple[str, int], GoalRule]:
        return {(r.indicator_code, r.year): r for r in self.rules}

    @cached_property
    def _targets(self) -> dict[tuple[str, int, str], float]:
        return {(g.indicator_code, g.year, g.site_code): g.target for g in self.site_goals}

    @cached_property
    def _populations(self) -> dict[tuple[str, int], int]:
        return {(p.site_code, p.year): p.population for p in self.populations}

    def program(self, code: str) -> Program:
        try:
            return self._programs[code]
        except KeyError:
            raise ValidationError(f"El programa {code} no existe en el catálogo.") from None

    def site(self, code: str) -> Site:
        try:
            return self._sites[code]
        except KeyError:
            raise ValidationError(f"La sede {code} no existe en el catálogo.") from None

    def indicator(self, code: str) -> Indicator:
        try:
            return self._indicators[code]
        except KeyError:
            raise ValidationError(f"El indicador {code} no existe en el catálogo.") from None

    def has_site(self, code: str) -> bool:
        return code in self._sites

    def has_indicator(self, code: str) -> bool:
        return code in self._indicators

    def site_name(self, code: str | None) -> str:
        """Nombre visible de una sede; None representa a la red completa."""
        if code is None:
            return NETWORK_LABEL
        return self.site(code).name

    def rule(self, indicator_code: str, year: int) -> GoalRule | None:
        return self._rules.get((indicator_code, year))

    def target(self, indicator_code: str, year: int, site_code: str) -> float | None:
        return self._targets.get((indicator_code, year, site_code))

    def is_applicable(self, indicator: Indicator, year: int, site_code: str) -> bool:
        """Un indicador de meta fija por sede solo aplica a las sedes con meta asignada en el año."""
        if indicator.denominator_type is not DenominatorType.FIXED_SITE:
            return True
        return self.target(indicator.code, year, site_code) is not None

    def targets_for(self, indicator_code: str, site_code: str) -> dict[int, float]:
        return {
            year: target
            for (ind, year, site), target in self._targets.items()
            if ind == indicator_code and site == site_code
        }

    def population(self, site_code: str, year: int) -> int | None:
        return self._populations.get((site_code, year))

    def populations_for(self, site_code: str) -> dict[int, int]:
        return {year: pop for (site, year), pop in self._populations.items() if site == site_code}

    @cached_property
    def years(self) -> tuple[int, ...]:
        """Años con reglas de meta configuradas."""
        return tuple(sorted({r.year for r in self.rules}))

    def is_active(self, indicator_code: str, year: int) -> bool:
        """Un indicador está vigente en un año si tiene regla de meta para ese año."""
        return (indicator_code, year) in self._rules

    def active_indicators(self, year: int, program_code: str | None = None) -> list[Indicator]:
        chosen = [
            ind
            for ind in self.indicators
            if self.is_active(ind.code, year) and (program_code is None or ind.program_code == program_code)
        ]
        program_order = {p.code: p.sort_order for p in self.programs}
        return sorted(chosen, key=lambda i: (program_order.get(i.program_code, 0), i.sort_order, i.code))

    def weights(self, year: int, program_code: str) -> dict[str, float]:
        return {ind.code: self._rules[(ind.code, year)].weight for ind in self.active_indicators(year, program_code)}

    def ordered_sites(self) -> list[Site]:
        return sorted(self.sites, key=lambda s: (s.sort_order, s.code))

    def ordered_programs(self) -> list[Program]:
        return sorted(self.programs, key=lambda p: (p.sort_order, p.code))


def validate_catalog(catalog: Catalog, year: int | None = None) -> list[str]:
    """Revisa la coherencia del catálogo y devuelve la lista de problemas (vacía si está bien).

    Controla que los pesos de cada programa sumen 100 % en cada año, que los
    indicadores de meta fija tengan metas por sede, que cada tipo de regla sea
    compatible con la dirección del indicador y que las referencias existan.
    Incluye tanto los problemas críticos (`critical_catalog_problems`) como los
    de cobertura de datos (`_coverage_problems`); ver esas funciones para la
    diferencia entre ambos.
    """
    return critical_catalog_problems(catalog, year) + _coverage_problems(catalog, year)


def critical_catalog_problems(catalog: Catalog, year: int | None = None) -> list[str]:
    """Problemas que impiden calcular un índice confiable: referencias rotas del
    catálogo, una regla incompatible con la dirección del indicador, o pesos que
    no suman 100 % en el año.

    A diferencia de `validate_catalog`, no incluye los indicadores de meta fija
    por sede sin ninguna meta asignada ese año: esos quedan "no aplica" en las
    sedes sin meta (`Catalog.is_applicable`) sin invalidar el resto del
    programa, lo que puede pasar legítimamente con datos reales todavía
    incompletos para un año. Es la función que debe usarse para decidir si es
    seguro mostrar un índice (ver `evaluate_period`); `validate_catalog` sigue
    siendo la lista completa para la vista de calidad de datos.
    """
    problems: list[str] = []
    years = [year] if year is not None else list(catalog.years)
    program_codes = {p.code for p in catalog.programs}
    for ind in catalog.indicators:
        if ind.program_code not in program_codes:
            problems.append(f"El indicador {ind.code} apunta a un programa inexistente ({ind.program_code}).")
    for rule in catalog.rules:
        if not catalog.has_indicator(rule.indicator_code):
            problems.append(f"Hay una regla de meta para un indicador inexistente ({rule.indicator_code}).")
    for goal in catalog.site_goals:
        if not catalog.has_site(goal.site_code) or not catalog.has_indicator(goal.indicator_code):
            problems.append(
                f"Hay una meta por sede con referencias inexistentes ({goal.indicator_code}, {goal.site_code})."
            )
    for y in years:
        weights: dict[str, float] = defaultdict(float)
        active_programs: set[str] = set()
        for ind in catalog.active_indicators(y):
            rule = catalog.rule(ind.code, y)
            if rule is None:
                continue
            weights[ind.program_code] += rule.weight
            active_programs.add(ind.program_code)
            if rule.rule_type is GoalRuleType.BANDS and not rule.bands:
                problems.append(f"La regla por tramos de {ind.code} ({y}) no tiene tramos.")
            direction_problem = _direction_problem(ind, rule)
            if direction_problem:
                problems.append(direction_problem)
        for program_code in sorted(active_programs):
            total = weights[program_code]
            if abs(total - 1.0) > WEIGHT_TOLERANCE:
                problems.append(
                    f"Los pesos del programa {program_code} en {y} suman {format_pct(total, 2)} y deben sumar 100 %."
                )
    return problems


def _coverage_problems(catalog: Catalog, year: int | None = None) -> list[str]:
    """Indicadores de meta fija por sede sin ninguna meta asignada en el año.

    No impiden calcular el índice (el indicador queda "no aplica" en las sedes
    sin meta propia), pero conviene mostrarlos como alerta de calidad de datos.
    """
    problems: list[str] = []
    years = [year] if year is not None else list(catalog.years)
    for y in years:
        for ind in catalog.active_indicators(y):
            if ind.denominator_type is DenominatorType.FIXED_SITE and not any(
                catalog.target(ind.code, y, site.code) is not None for site in catalog.sites
            ):
                problems.append(f"El indicador {ind.code} usa meta fija por sede y no tiene metas para {y}.")
    return problems


def _direction_problem(indicator: Indicator, rule: GoalRule) -> str:
    """Regla incompatible con la dirección del indicador (texto vacío si son compatibles).

    Los tramos dan más cumplimiento a los valores más bajos (tiempos de espera),
    así que solo sirven cuando menor es mejor. Las metas de aumento sobre el año
    anterior premian subir, así que solo sirven cuando mayor es mejor.
    """
    label = f"{indicator.code} ({rule.year})"
    if rule.rule_type is GoalRuleType.BANDS and indicator.direction is not Direction.LOWER_IS_BETTER:
        return f"La regla por tramos de {label} premia los valores bajos y el indicador es de mayor es mejor."
    if rule.rule_type.needs_reference and indicator.direction is not Direction.HIGHER_IS_BETTER:
        return f"La meta de aumento de {label} premia subir y el indicador es de menor es mejor."
    return ""
