"""Catálogo de ejemplo: 5 sedes, 3 programas y 24 indicadores con sus reglas de meta por año.

Los nombres son genéricos de gestión. Las metas, pesos, multiplicadores y
tramos siguen la estructura y el orden de magnitud de un caso real, con
valores redondeados y perturbados.
"""

from __future__ import annotations

from kpi_monitor.domain.catalog import Catalog
from kpi_monitor.domain.enums import Aggregation, DenominatorType, Direction, GoalRuleType, Scale
from kpi_monitor.domain.models import (
    GoalBand,
    GoalRule,
    Indicator,
    IndicatorSheet,
    Program,
    Site,
    SiteGoal,
    SitePopulation,
)

YEARS = (2024, 2025, 2026)
QUARTERLY = (3, 6, 9, 12)
SEMIANNUAL = (6, 12)
PROGRAM_CUTS = (4, 7, 12)
ACTIVITY_CUTS = (5, 7, 9, 12)
REGISTRY = "Registro estadístico mensual de actividades de la sede."
STOCK_REGISTRY = "Registro de personas en seguimiento, declarado en los meses de corte."
ZERO_NOT_REPORTED = (
    "Un cero en un mes informado suele indicar falta de registro; un mes no informado queda como faltante."
)
ZERO_REAL = "Un cero informado es un resultado real del mes; un mes no informado queda como faltante."

PROGRAMS = (
    Program(
        "CG",
        "Compromisos de gestión",
        "Dieciséis indicadores comprometidos por la organización, con pesos que suman 100 % cada año.",
        1,
    ),
    Program(
        "PC",
        "Programa complementario",
        "Programa de acompañamiento con metas propias; algunos indicadores tienen años de línea base.",
        2,
    ),
    Program(
        "IA",
        "Índice de actividad",
        "Tres indicadores sobre las personas en seguimiento, evaluados en los cortes de mayo, julio, septiembre "
        "y diciembre.",
        3,
    ),
)

SITES = (
    Site("NOR", "Sede Norte", "Centro con unidad de urgencia", 1),
    Site("SUR", "Sede Sur", "Centro", 2),
    Site("CEN", "Sede Centro", "Centro con unidad de urgencia", 3),
    Site("ORI", "Sede Oriente", "Centro", 4),
    Site("PON", "Sede Poniente", "Centro", 5),
)

# Población de referencia 2026 (sintética) y crecimiento anual aproximado.
POPULATION_2026 = {"NOR": 23480, "SUR": 12310, "CEN": 17620, "ORI": 10870, "PON": 16040}
POPULATION_GROWTH = 1.011


def _population(site: str, year: int) -> int:
    value = POPULATION_2026[site] / POPULATION_GROWTH ** (2026 - year)
    return round(value / 10) * 10


def _sheet(
    measures: str, numerator: str, denominator: str, zero: str, notes: str = "", source: str = REGISTRY
) -> IndicatorSheet:
    return IndicatorSheet(measures, numerator, denominator, source, zero, notes)


INDICATORS = (
    Indicator(
        code="CG01",
        program_code="CG",
        name="Cumplimiento de evaluaciones preventivas comprometidas",
        short_name="Evaluaciones preventivas",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FIXED_SITE,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        cut_months=PROGRAM_CUTS,
        sort_order=1,
        sheet=_sheet(
            "Qué parte de las evaluaciones preventivas comprometidas para el año se aplicó efectivamente.",
            "Evaluaciones preventivas aplicadas en el mes; se suman los meses del año.",
            "Compromiso anual de evaluaciones de la sede (meta fija por sede). En la red, la suma de los compromisos.",
            ZERO_NOT_REPORTED,
            "Puede superar el 100 % si la sede aplica más evaluaciones que las comprometidas.",
        ),
    ),
    Indicator(
        code="CG02",
        program_code="CG",
        name="Proporción de evaluaciones con consejería",
        short_name="Consejería tras evaluación",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=PROGRAM_CUTS,
        sort_order=2,
        sheet=_sheet(
            "Qué parte de las personas evaluadas recibió además una consejería breve.",
            "Consejerías realizadas a personas evaluadas en el mes.",
            "Evaluaciones preventivas aplicadas en el mes.",
            ZERO_NOT_REPORTED,
            "Un valor sobre 100 % indica un error de registro: consejerías sin evaluación asociada.",
        ),
    ),
    Indicator(
        code="CG03",
        program_code="CG",
        name="Proporción de derivaciones asistidas en casos con riesgo",
        short_name="Derivación asistida",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=PROGRAM_CUTS,
        sort_order=3,
        sheet=_sheet(
            "Qué parte de las personas evaluadas con riesgo fue derivada con acompañamiento hasta la atención.",
            "Derivaciones asistidas realizadas en el mes.",
            "Personas evaluadas con resultado de riesgo en el mes.",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="CG04",
        program_code="CG",
        name="Cumplimiento de atenciones del equipo de apoyo en urgencias",
        short_name="Atenciones en urgencias",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FIXED_SITE,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        cut_months=PROGRAM_CUTS,
        sort_order=4,
        sheet=_sheet(
            "Avance de las atenciones del equipo de apoyo en las unidades de urgencia frente a su compromiso anual.",
            "Atenciones realizadas por el equipo de apoyo en el mes.",
            "Compromiso anual de atenciones de la unidad (meta fija por sede).",
            ZERO_NOT_REPORTED,
            "Solo las sedes con unidad de urgencia tienen meta; en las demás el indicador no aplica.",
        ),
    ),
    Indicator(
        code="CG05",
        program_code="CG",
        name="Controles por persona en seguimiento",
        short_name="Controles por persona",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.STOCK,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.RATE,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=5,
        sheet=_sheet(
            "Concentración de controles: cuántos controles recibe en el año cada persona en seguimiento.",
            "Controles realizados en el mes; se suman los meses del año.",
            "Personas en seguimiento al último corte trimestral (stock; no se suma).",
            ZERO_NOT_REPORTED,
            "La meta de cada sede es su valor del año anterior con un aumento porcentual y un tope de controles por "
            "persona (ver la regla del año).",
        ),
    ),
    Indicator(
        code="CG06",
        program_code="CG",
        name="Tiempo de espera a primera atención",
        short_name="Tiempo de espera (días)",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.LOWER_IS_BETTER,
        scale=Scale.DAYS,
        cut_months=PROGRAM_CUTS,
        sort_order=6,
        sheet=_sheet(
            "Promedio de días que esperan las personas derivadas hasta su primera atención.",
            "Suma de los días de espera de los casos que tuvieron su primera atención en el mes.",
            "Casos con primera atención en el mes.",
            "Un promedio de cero días significa atención inmediata; un mes sin casos queda sin valor.",
            "Se evalúa por tramos del promedio acumulado, sin redondear.",
        ),
    ),
    Indicator(
        code="CG07",
        program_code="CG",
        name="Proporción de intervenciones grupales sobre el total de atenciones",
        short_name="Intervenciones grupales",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=PROGRAM_CUTS,
        sort_order=7,
        sheet=_sheet(
            "Peso de las intervenciones grupales dentro del total de atenciones de seguimiento.",
            "Intervenciones grupales realizadas en el mes.",
            "Total de atenciones de seguimiento (individuales y grupales) en el mes.",
            ZERO_NOT_REPORTED,
            "Desde 2025 la meta es un aumento porcentual sobre la proporción del año anterior completo.",
        ),
    ),
    Indicator(
        code="CG08",
        program_code="CG",
        name="Cobertura de evaluación de resultados al egreso",
        short_name="Evaluación al egreso",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=PROGRAM_CUTS,
        sort_order=8,
        sheet=_sheet(
            "Qué parte de las personas egresadas tiene una evaluación de resultados al ingreso y al egreso.",
            "Personas egresadas con evaluación de resultados aplicada.",
            "Personas egresadas en el mes.",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="CG09",
        program_code="CG",
        name="Proporción de egresos con resultado favorable",
        short_name="Egresos favorables",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=PROGRAM_CUTS,
        sort_order=9,
        sheet=_sheet(
            "Qué parte de las personas evaluadas al egreso obtiene un resultado favorable.",
            "Personas evaluadas al egreso con resultado favorable.",
            "Personas evaluadas al egreso en el mes.",
            ZERO_REAL,
        ),
    ),
    Indicator(
        code="CG10",
        program_code="CG",
        name="Cobertura de personas en acompañamiento comprometidas",
        short_name="Personas en acompañamiento",
        aggregation=Aggregation.STOCK,
        denominator_type=DenominatorType.FIXED_SITE,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=10,
        sheet=_sheet(
            "Cuántas de las personas comprometidas para acompañamiento están efectivamente en acompañamiento.",
            "Personas en acompañamiento al último corte trimestral (stock; no se suma).",
            "Compromiso anual de personas en acompañamiento de la sede (meta fija por sede).",
            "Un cero en un corte informado indica que no hay personas en acompañamiento o falta de registro.",
            "No se prorratea: se compara el último corte con el compromiso completo.",
            STOCK_REGISTRY,
        ),
    ),
    Indicator(
        code="CG11",
        program_code="CG",
        name="Visitas en terreno por persona en acompañamiento",
        short_name="Visitas en terreno",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.K_STOCK,
        multiplier=8.0,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=11,
        sheet=_sheet(
            "Avance de las visitas en terreno frente al estándar anual por persona en acompañamiento.",
            "Visitas en terreno realizadas en el mes.",
            "Estándar anual: 8 visitas por cada persona en acompañamiento al último corte.",
            ZERO_NOT_REPORTED,
            "El multiplicador se aplica una sola vez sobre el número de personas.",
        ),
    ),
    Indicator(
        code="CG12",
        program_code="CG",
        name="Contactos remotos por persona en acompañamiento",
        short_name="Contactos remotos",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.K_STOCK,
        multiplier=12.0,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=12,
        sheet=_sheet(
            "Avance de los contactos remotos de seguimiento frente al estándar anual por persona.",
            "Contactos remotos de seguimiento realizados en el mes.",
            "Estándar anual: 12 contactos por cada persona en acompañamiento al último corte.",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="CG13",
        program_code="CG",
        name="Reuniones de coordinación por persona en acompañamiento",
        short_name="Reuniones de coordinación",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.K_STOCK,
        multiplier=10.0,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=13,
        sheet=_sheet(
            "Avance de las reuniones de coordinación con otras instituciones frente al estándar anual por persona.",
            "Reuniones de coordinación realizadas en el mes.",
            "Estándar anual: 10 reuniones por cada persona en acompañamiento al último corte.",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="CG14",
        program_code="CG",
        name="Cobertura de control integral en población vinculada",
        short_name="Control integral vigente",
        aggregation=Aggregation.STOCK,
        denominator_type=DenominatorType.FIXED_SITE,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=14,
        sheet=_sheet(
            "Cuántas personas de la población vinculada comprometida tienen su control integral al día.",
            "Personas vinculadas con control integral vigente al último corte trimestral (stock).",
            "Compromiso anual de personas vinculadas con control vigente (meta fija por sede).",
            "Un cero en un corte informado indica falta de registro.",
            "",
            STOCK_REGISTRY,
        ),
    ),
    Indicator(
        code="CG15",
        program_code="CG",
        name="Controles por persona en población vinculada",
        short_name="Controles población vinculada",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.STOCK,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.RATE,
        stock_months=QUARTERLY,
        cut_months=PROGRAM_CUTS,
        sort_order=15,
        sheet=_sheet(
            "Concentración de controles en la población vinculada durante el año.",
            "Controles realizados a personas de la población vinculada en el mes.",
            "Personas de la población vinculada al último corte trimestral (stock; no se suma).",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="CG16",
        program_code="CG",
        name="Cumplimiento del plan de coordinación intersectorial",
        short_name="Plan intersectorial",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.MANUAL,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=PROGRAM_CUTS,
        sort_order=16,
        sheet=_sheet(
            "Qué parte de las acciones programadas en el plan de coordinación con otras instituciones se ejecutó.",
            "Acciones del plan ejecutadas en el mes.",
            "Acciones del plan programadas para el mes.",
            "Mientras la sede no define su plan no hay denominador y el indicador queda sin datos.",
            "Se carga manualmente desde la planilla del plan; vigente desde 2026.",
            "Planilla de seguimiento del plan de coordinación (carga manual).",
        ),
    ),
    Indicator(
        code="PC01",
        program_code="PC",
        name="Proporción de casos confirmados entre las personas evaluadas",
        short_name="Casos confirmados",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=SEMIANNUAL,
        sort_order=1,
        sheet=_sheet(
            "Qué parte de las personas evaluadas por sospecha termina con el caso confirmado.",
            "Casos confirmados en el mes.",
            "Personas evaluadas por sospecha en el mes.",
            ZERO_REAL,
            "Indicador de línea base: se mide para fijar metas futuras y no tiene semáforo.",
        ),
    ),
    Indicator(
        code="PC02",
        program_code="PC",
        name="Personas que recuperan su participación comunitaria",
        short_name="Participación comunitaria",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=SEMIANNUAL,
        sort_order=2,
        sheet=_sheet(
            "Qué parte de las personas acompañadas recupera o fortalece su participación en la comunidad.",
            "Personas con participación comunitaria recuperada o fortalecida en el mes.",
            "Personas acompañadas evaluadas en el mes.",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="PC03",
        program_code="PC",
        name="Ingresos con plan de cuidado acordado",
        short_name="Plan de cuidado",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        cut_months=SEMIANNUAL,
        sort_order=3,
        sheet=_sheet(
            "Qué parte de las personas que ingresan al programa cuenta con un plan de cuidado acordado e informado.",
            "Ingresos del mes con plan de cuidado acordado.",
            "Ingresos al programa en el mes.",
            ZERO_NOT_REPORTED,
        ),
    ),
    Indicator(
        code="PC04",
        program_code="PC",
        name="Sesiones de acompañamiento por persona ingresada",
        short_name="Intensidad de sesiones",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.FLOW,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        cut_months=SEMIANNUAL,
        sort_order=4,
        sheet=_sheet(
            "Intensidad del acompañamiento: sesiones realizadas por cada persona ingresada.",
            "Sesiones de acompañamiento realizadas en el mes.",
            "Ingresos al programa en el mes.",
            ZERO_NOT_REPORTED,
            "Indicador de intensidad: supera el 100 % de forma legítima cuando cada persona recibe más de una "
            "sesión. En el índice ponderado cuenta como máximo 100 %.",
        ),
    ),
    Indicator(
        code="PC05",
        program_code="PC",
        name="Proporción del equipo capacitado",
        short_name="Equipo capacitado",
        aggregation=Aggregation.STOCK,
        denominator_type=DenominatorType.FIXED_SITE,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        stock_months=SEMIANNUAL,
        cut_months=SEMIANNUAL,
        sort_order=5,
        sheet=_sheet(
            "Qué parte del personal definido para capacitarse completó la capacitación del programa.",
            "Personas capacitadas al último corte semestral (stock).",
            "Personas definidas para capacitarse en la sede (meta fija por sede).",
            "Un cero en un corte informado indica que nadie ha completado la capacitación.",
            "En 2025 fue línea base; desde 2026 tiene meta.",
            "Registro semestral de capacitación del personal.",
        ),
    ),
    Indicator(
        code="IA01",
        program_code="IA",
        name="Cobertura de personas en seguimiento",
        short_name="Cobertura en seguimiento",
        aggregation=Aggregation.STOCK,
        denominator_type=DenominatorType.POPULATION,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        stock_months=QUARTERLY,
        cut_months=ACTIVITY_CUTS,
        sort_order=1,
        sheet=_sheet(
            "Qué parte de la población que se espera que necesite atención está en seguimiento.",
            "Personas en seguimiento al último corte trimestral (stock).",
            "Población de referencia de la sede multiplicada por la prevalencia esperada (parámetro).",
            "Un cero en un corte informado indica falta de registro.",
            "Si la sede no tiene población de referencia el valor no se publica.",
            STOCK_REGISTRY,
        ),
    ),
    Indicator(
        code="IA02",
        program_code="IA",
        name="Atenciones por persona en seguimiento",
        short_name="Atenciones por persona",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.STOCK,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.RATE,
        stock_months=QUARTERLY,
        cut_months=ACTIVITY_CUTS,
        sort_order=2,
        sheet=_sheet(
            "Cuántas atenciones recibe en el año cada persona en seguimiento.",
            "Atenciones de seguimiento realizadas en el mes; se suman los meses del año.",
            "Personas en seguimiento al último corte trimestral (stock; no se suma).",
            ZERO_NOT_REPORTED,
            "No se usa un umbral fijo para declarar un valor imposible: una concentración alta puede ser real.",
        ),
    ),
    Indicator(
        code="IA03",
        program_code="IA",
        name="Egresos por logro de objetivos",
        short_name="Egresos por logro",
        aggregation=Aggregation.FLOW,
        denominator_type=DenominatorType.STOCK,
        direction=Direction.HIGHER_IS_BETTER,
        scale=Scale.PROPORTION,
        natural_ceiling=1.0,
        stock_months=QUARTERLY,
        cut_months=ACTIVITY_CUTS,
        sort_order=3,
        sheet=_sheet(
            "Qué parte de las personas en seguimiento egresa en el año por haber logrado sus objetivos.",
            "Egresos por logro de objetivos en el mes; se suman los meses del año.",
            "Personas en seguimiento al último corte trimestral (stock; no se suma).",
            ZERO_REAL,
        ),
    ),
)

# Pesos por año dentro de cada programa (suman 100 %).
WEIGHTS_2026 = {
    "CG01": 0.055, "CG02": 0.05, "CG03": 0.065, "CG04": 0.16, "CG05": 0.08, "CG06": 0.075, "CG07": 0.06,
    "CG08": 0.075, "CG09": 0.065, "CG10": 0.055, "CG11": 0.045, "CG12": 0.02, "CG13": 0.03, "CG14": 0.065,
    "CG15": 0.04, "CG16": 0.06,
    "PC01": 0.12, "PC02": 0.23, "PC03": 0.27, "PC04": 0.22, "PC05": 0.16,
    "IA01": 0.45, "IA02": 0.33, "IA03": 0.22,
}  # fmt: skip
WEIGHTS_BEFORE_2026 = {
    "CG01": 0.065, "CG02": 0.055, "CG03": 0.075, "CG04": 0.17, "CG05": 0.085, "CG06": 0.08, "CG07": 0.065,
    "CG08": 0.08, "CG09": 0.06, "CG10": 0.05, "CG11": 0.05, "CG12": 0.025, "CG13": 0.035, "CG14": 0.065,
    "CG15": 0.04,
    "PC01": 0.12, "PC02": 0.23, "PC03": 0.27, "PC04": 0.22, "PC05": 0.16,
    "IA01": 0.45, "IA02": 0.33, "IA03": 0.22,
}  # fmt: skip

# Primer año de vigencia de cada indicador (sin regla antes de ese año).
FIRST_YEAR = {"CG16": 2026, "PC01": 2025, "PC02": 2025, "PC03": 2025, "PC04": 2025, "PC05": 2025}

WAITING_BANDS = (
    GoalBand(20.0, 1.0),
    GoalBand(30.0, 0.75),
    GoalBand(40.0, 0.5),
    GoalBand(50.0, 0.25),
    GoalBand(None, 0.0),
)

ABSOLUTE_GOALS = {
    "CG01": 0.87, "CG02": 0.95, "CG03": 0.76, "CG04": 0.84, "CG08": 0.78, "CG09": 0.83, "CG10": 0.86,
    "CG11": 0.82, "CG12": 0.74, "CG13": 0.85, "CG14": 0.77, "CG15": 5.5, "CG16": 0.84,
    "PC02": 0.27, "PC03": 0.71, "PC04": 1.10, "IA03": 0.12,
}  # fmt: skip
# Meta de aumento con tope (controles por persona) y de aumento relativo (intervenciones grupales).
CAPPED_FACTOR = 0.18
CAPPED_CEILING = 8.5
RELATIVE_FACTOR = 0.15
YEARLY_ABSOLUTE_GOALS = {
    "IA01": {2024: 0.22, 2025: 0.23, 2026: 0.24},
    "IA02": {2024: 10.0, 2025: 10.5, 2026: 11.0},
}

# Metas fijas por sede (compromisos anuales).
SITE_TARGETS = {
    "CG01": {"NOR": 2380, "SUR": 1790, "CEN": 2210, "ORI": 1640, "PON": 2020},
    "CG04": {"NOR": 760, "CEN": 650},
    "CG10": {"NOR": 34, "SUR": 24, "CEN": 31, "ORI": 19, "PON": 27},
    "CG14": {"NOR": 128, "SUR": 86, "CEN": 112, "ORI": 77, "PON": 101},
    "PC05": {"NOR": 15, "SUR": 10, "CEN": 13, "ORI": 9, "PON": 11},
}
PC05_GOAL = 0.88
# Los compromisos de 2024 eran algo menores.
TARGET_FACTOR = {2024: 0.95, 2025: 1.0, 2026: 1.0}


def _rule(code: str, year: int) -> GoalRule | None:
    if year < FIRST_YEAR.get(code, YEARS[0]):
        return None
    weight = (WEIGHTS_2026 if year >= 2026 else WEIGHTS_BEFORE_2026)[code]
    if code == "CG05":
        if year == 2024:
            return GoalRule(code, year, GoalRuleType.ABSOLUTE, weight, value=8.0)
        return GoalRule(code, year, GoalRuleType.CAPPED_INCREASE, weight, factor=CAPPED_FACTOR, ceiling=CAPPED_CEILING)
    if code == "CG06":
        return GoalRule(code, year, GoalRuleType.BANDS, weight, bands=WAITING_BANDS)
    if code == "CG07":
        if year == 2024:
            return GoalRule(code, year, GoalRuleType.ABSOLUTE, weight, value=0.08)
        return GoalRule(code, year, GoalRuleType.RELATIVE_INCREASE, weight, factor=RELATIVE_FACTOR)
    if code == "PC01" or (code == "PC05" and year == 2025):
        return GoalRule(code, year, GoalRuleType.BASELINE, weight)
    if code == "PC05":
        return GoalRule(code, year, GoalRuleType.ABSOLUTE, weight, value=PC05_GOAL)
    if code in YEARLY_ABSOLUTE_GOALS:
        return GoalRule(code, year, GoalRuleType.ABSOLUTE, weight, value=YEARLY_ABSOLUTE_GOALS[code][year])
    return GoalRule(code, year, GoalRuleType.ABSOLUTE, weight, value=ABSOLUTE_GOALS[code])


def _site_goals() -> list[SiteGoal]:
    goals = []
    for code, targets in SITE_TARGETS.items():
        for year in YEARS:
            if year < FIRST_YEAR.get(code, YEARS[0]):
                continue
            for site, target in targets.items():
                value = target if target < 100 else round(target * TARGET_FACTOR[year] / 10) * 10
                goals.append(SiteGoal(code, year, site, float(value)))
    return goals


def build_catalog() -> Catalog:
    """Catálogo completo de ejemplo."""
    rules = [rule for ind in INDICATORS for year in YEARS if (rule := _rule(ind.code, year)) is not None]
    populations = [SitePopulation(site.code, year, _population(site.code, year)) for site in SITES for year in YEARS]
    return Catalog(
        programs=PROGRAMS,
        sites=SITES,
        indicators=INDICATORS,
        rules=tuple(rules),
        site_goals=tuple(_site_goals()),
        populations=tuple(populations),
    )
