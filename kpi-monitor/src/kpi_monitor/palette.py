"""Paleta de colores compartida por la interfaz y los reportes (sin dependencias de Qt)."""

from __future__ import annotations

from kpi_monitor.domain.enums import Status

BACKGROUND = "#F4F5F7"
SURFACE = "#FFFFFF"
BORDER = "#D9DDE3"
TEXT = "#1F2933"
TEXT_MUTED = "#5F6B7A"
PRIMARY = "#1F4E79"
ACCENT = "#2E86C1"
GOOD = "#2E7D32"
WARNING = "#B7791F"
BAD = "#C62828"
NEUTRAL = "#8A94A3"

# Serie categórica sobria para gráficos (orden estable entre proyectos).
SERIES = ("#1F4E79", "#2E86C1", "#7FB3D5", "#5D6D7E", "#A9CCE3", "#34495E", "#85929E", "#D4E6F1")

# Colores del semáforo: se usan solo para estados.
STATUS_COLORS = {
    Status.GREEN: GOOD,
    Status.YELLOW: WARNING,
    Status.RED: BAD,
    Status.NO_DATA: "#B0B8C4",
    Status.UNDETERMINED: "#7B8794",
    Status.BASELINE: "#D5DBE3",
    Status.NOT_APPLICABLE: "#EEF1F5",
    Status.PENDING: "#E4E7EB",
}
# Fondos suaves para celdas de tablas y hojas de cálculo.
STATUS_FILLS = {
    Status.GREEN: "#DCEFDC",
    Status.YELLOW: "#FBEBC8",
    Status.RED: "#F6D5D5",
    Status.NO_DATA: "#E9ECF0",
    Status.UNDETERMINED: "#E1E5EA",
    Status.BASELINE: "#F1F3F6",
    Status.NOT_APPLICABLE: "#F7F8FA",
    Status.PENDING: "#F4F5F7",
}
