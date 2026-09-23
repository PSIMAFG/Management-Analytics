"""Enumeraciones del dominio.

Los valores se guardan tal cual en SQLite (códigos estables en inglés) y la
propiedad `label` entrega el texto en español que ve el usuario.
"""

from __future__ import annotations

from enum import StrEnum


class Aggregation(StrEnum):
    """Cómo se acumula el numerador dentro del año."""

    FLOW = "flow"
    STOCK = "stock"

    @property
    def label(self) -> str:
        return _AGGREGATION_LABELS[self]


class DenominatorType(StrEnum):
    """Origen del denominador de un indicador."""

    FLOW = "flow"
    FIXED_SITE = "fixed_site"
    STOCK = "stock"
    K_STOCK = "k_stock"
    POPULATION = "population"
    MANUAL = "manual"

    @property
    def label(self) -> str:
        return _DENOMINATOR_LABELS[self]


class Direction(StrEnum):
    """Sentido deseado del indicador. No tiene valor neutro."""

    HIGHER_IS_BETTER = "higher"
    LOWER_IS_BETTER = "lower"

    @property
    def label(self) -> str:
        return _DIRECTION_LABELS[self]


class Scale(StrEnum):
    """Escala en que se expresa el valor del indicador."""

    PROPORTION = "proportion"
    RATE = "rate"
    DAYS = "days"

    @property
    def label(self) -> str:
        return _SCALE_LABELS[self]


class GoalRuleType(StrEnum):
    """Tipo de regla con que se fija la meta de un año."""

    ABSOLUTE = "absolute"
    RELATIVE_INCREASE = "relative_increase"
    CAPPED_INCREASE = "capped_increase"
    BANDS = "bands"
    BASELINE = "baseline"

    @property
    def label(self) -> str:
        return _RULE_LABELS[self]

    @property
    def needs_reference(self) -> bool:
        """Indica si la meta depende del valor del año anterior."""
        return self in (GoalRuleType.RELATIVE_INCREASE, GoalRuleType.CAPPED_INCREASE)


class Status(StrEnum):
    """Estado de un indicador en el semáforo oficial."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    NO_DATA = "no_data"
    UNDETERMINED = "undetermined"
    BASELINE = "baseline"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]

    @property
    def is_evaluated(self) -> bool:
        """Verdadero para los tres colores del semáforo."""
        return self in (Status.GREEN, Status.YELLOW, Status.RED)


class Severity(StrEnum):
    """Gravedad de una alerta."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    @property
    def label(self) -> str:
        return _SEVERITY_LABELS[self]

    @property
    def rank(self) -> int:
        """Orden para listar primero lo más grave."""
        return _SEVERITY_RANK[self]


class AlertKind(StrEnum):
    """Tipos de alerta que genera el monitor."""

    AT_RISK = "at_risk"
    NO_REPORT = "no_report"
    UNDERREPORTING = "underreporting"
    IMPOSSIBLE_VALUE = "impossible_value"
    UNDETERMINED_GOAL = "undetermined_goal"
    MISSING_DENOMINATOR = "missing_denominator"

    @property
    def label(self) -> str:
        return _ALERT_LABELS[self]


class ProjectionMethod(StrEnum):
    """Método con que se obtuvo la proyección al cierre."""

    BOOTSTRAP = "bootstrap"
    CARRY_FORWARD = "carry_forward"
    INSUFFICIENT = "insufficient"
    CLOSED = "closed"
    NONE = "none"

    @property
    def label(self) -> str:
        return _PROJECTION_LABELS[self]


class Origin(StrEnum):
    """Procedencia de una observación."""

    SYNTHETIC = "synthetic"
    IMPORTED = "imported"
    MANUAL = "manual"

    @property
    def label(self) -> str:
        return _ORIGIN_LABELS[self]


_AGGREGATION_LABELS = {
    Aggregation.FLOW: "Flujo (se suma mes a mes)",
    Aggregation.STOCK: "Stock (último corte, no se suma)",
}
_DENOMINATOR_LABELS = {
    DenominatorType.FLOW: "Flujo mensual",
    DenominatorType.FIXED_SITE: "Meta fija por sede",
    DenominatorType.STOCK: "Stock al último corte",
    DenominatorType.K_STOCK: "Estándar por persona (k × stock)",
    DenominatorType.POPULATION: "Población de referencia × prevalencia",
    DenominatorType.MANUAL: "Carga manual",
}
_DIRECTION_LABELS = {
    Direction.HIGHER_IS_BETTER: "Mayor es mejor",
    Direction.LOWER_IS_BETTER: "Menor es mejor",
}
_SCALE_LABELS = {
    Scale.PROPORTION: "Porcentaje",
    Scale.RATE: "Tasa por persona",
    Scale.DAYS: "Promedio de días",
}
_RULE_LABELS = {
    GoalRuleType.ABSOLUTE: "Meta absoluta",
    GoalRuleType.RELATIVE_INCREASE: "Aumento sobre el año anterior",
    GoalRuleType.CAPPED_INCREASE: "Aumento sobre el año anterior con tope",
    GoalRuleType.BANDS: "Tramos",
    GoalRuleType.BASELINE: "Línea base",
}
_STATUS_LABELS = {
    Status.GREEN: "Verde",
    Status.YELLOW: "Amarillo",
    Status.RED: "Rojo",
    Status.NO_DATA: "Sin datos",
    Status.UNDETERMINED: "Meta no determinable",
    Status.BASELINE: "Línea base",
    Status.NOT_APPLICABLE: "No aplica",
    Status.PENDING: "Pendiente",
}
_SEVERITY_LABELS = {Severity.CRITICAL: "Crítica", Severity.WARNING: "Advertencia", Severity.INFO: "Información"}
_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
_ALERT_LABELS = {
    AlertKind.AT_RISK: "En riesgo",
    AlertKind.NO_REPORT: "Sin reporte",
    AlertKind.UNDERREPORTING: "Posible subregistro",
    AlertKind.IMPOSSIBLE_VALUE: "Dato imposible",
    AlertKind.UNDETERMINED_GOAL: "Meta no determinable",
    AlertKind.MISSING_DENOMINATOR: "Falta denominador",
}
_PROJECTION_LABELS = {
    ProjectionMethod.BOOTSTRAP: "Ritmo promedio con bootstrap",
    ProjectionMethod.CARRY_FORWARD: "Arrastre del último corte",
    ProjectionMethod.INSUFFICIENT: "Datos insuficientes",
    ProjectionMethod.CLOSED: "Año cerrado",
    ProjectionMethod.NONE: "Sin proyección",
}
_ORIGIN_LABELS = {Origin.SYNTHETIC: "Sintético", Origin.IMPORTED: "Importado", Origin.MANUAL: "Carga manual"}
