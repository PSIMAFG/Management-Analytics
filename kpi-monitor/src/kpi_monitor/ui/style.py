"""Paleta, hoja de estilo Qt y estilo de matplotlib compartidos por la interfaz."""

from __future__ import annotations

import matplotlib as mpl
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from kpi_monitor.palette import (
    ACCENT,
    BACKGROUND,
    BAD,
    BORDER,
    GOOD,
    NEUTRAL,
    PRIMARY,
    SERIES,
    STATUS_COLORS,
    STATUS_FILLS,
    SURFACE,
    TEXT,
    TEXT_MUTED,
    WARNING,
)

__all__ = [
    "ACCENT",
    "BACKGROUND",
    "BAD",
    "BORDER",
    "GOOD",
    "GRID",
    "NEUTRAL",
    "PRIMARY",
    "SERIES",
    "STATUS_COLORS",
    "STATUS_FILLS",
    "SURFACE",
    "TEXT",
    "TEXT_MUTED",
    "TONE_COLORS",
    "WARNING",
    "apply_style",
    "configure_matplotlib",
]

TONE_COLORS = {"neutral": TEXT, "good": GOOD, "warning": WARNING, "bad": BAD, "muted": TEXT_MUTED}
GRID = "#E6E9EE"

STYLESHEET = f"""
QMainWindow, QWidget#central {{ background: {BACKGROUND}; }}
QFrame#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#sidePanel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#infoPanel {{ background: #F8F9FB; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#kpiCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#kpiTitle {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#kpiValue {{ font-size: 17pt; font-weight: 600; }}
QLabel#kpiCaption {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#appTitle {{ color: {PRIMARY}; font-size: 12pt; font-weight: 700; }}
QLabel#appSubtitle {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#scopeTitle {{ color: {TEXT}; font-size: 13pt; font-weight: 600; }}
QLabel#scopeDetail {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#sectionTitle {{ color: {PRIMARY}; font-size: 10pt; font-weight: 600; padding-top: 6px; }}
QLabel#fieldLabel {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#hint {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#errorText {{ color: {BAD}; font-size: 9pt; }}
QLabel#infoKey {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#infoValue {{ color: {TEXT}; font-size: 9pt; font-weight: 600; }}
QLabel#infoTitle {{ color: {PRIMARY}; font-size: 10pt; font-weight: 600; }}
QLabel#tableCaption {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#chip {{ border-radius: 4px; padding: 2px 8px; font-size: 9pt; font-weight: 600; }}
QLabel#legendText {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {SURFACE}; border-radius: 4px; top: -1px; }}
QTabBar::tab {{ padding: 6px 12px; color: {TEXT_MUTED}; background: transparent; border: none; }}
QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 2px solid {PRIMARY}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QPushButton {{ padding: 5px 12px; border: 1px solid {BORDER}; border-radius: 4px; background: {SURFACE};
               color: {TEXT}; text-align: center; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {NEUTRAL}; }}
QPushButton#segment {{ padding: 3px 14px; border: 1px solid {BORDER}; border-radius: 0; background: {SURFACE};
                       color: {TEXT_MUTED}; }}
QPushButton#segment[position="first"] {{ border-top-left-radius: 4px; border-bottom-left-radius: 4px; }}
QPushButton#segment[position="last"] {{ border-top-right-radius: 4px; border-bottom-right-radius: 4px;
                                        border-left: none; }}
QPushButton#segment:checked {{ background: #E3EDF7; color: {PRIMARY}; border-color: {ACCENT}; font-weight: 600; }}
QPushButton#primary {{ background: {PRIMARY}; color: white; border-color: {PRIMARY}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #24598A; }}
QPushButton#primary:disabled {{ background: {NEUTRAL}; border-color: {NEUTRAL}; }}
QTableView {{ background: {SURFACE}; gridline-color: {BORDER}; selection-background-color: #D6E6F5;
              selection-color: {TEXT}; alternate-background-color: #F8F9FB; border: none; }}
QHeaderView {{ font-size: 9pt; font-weight: 600; }}
QHeaderView::section {{ background: #EEF1F5; color: {TEXT}; padding: 4px 6px; border: none;
                        border-bottom: 1px solid {BORDER}; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 6px; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus {{ border-color: {ACCENT}; }}
QComboBox:disabled, QLineEdit:disabled {{ color: {NEUTRAL}; background: #F4F5F7; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {TEXT}; selection-background-color: #D6E6F5; }}
QTextBrowser {{ border: none; background: {SURFACE}; }}
QSplitter::handle {{ background: {BACKGROUND}; }}
QStatusBar {{ color: {TEXT_MUTED}; }}
QToolTip {{ color: {TEXT}; background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
"""


# Paleta clara completa. Los roles que no se fijan (bordes y sombras de Fusion, texto resaltado,
# marcadores de posición) se heredarían del sistema y, con Windows en modo oscuro, dejarían casillas
# y bordes casi invisibles sobre el fondo claro.
PALETTE_ROLES = {
    QPalette.ColorRole.Window: BACKGROUND,
    QPalette.ColorRole.WindowText: TEXT,
    QPalette.ColorRole.Base: SURFACE,
    QPalette.ColorRole.AlternateBase: "#F8F9FB",
    QPalette.ColorRole.Text: TEXT,
    QPalette.ColorRole.PlaceholderText: NEUTRAL,
    QPalette.ColorRole.Button: SURFACE,
    QPalette.ColorRole.ButtonText: TEXT,
    QPalette.ColorRole.BrightText: SURFACE,
    QPalette.ColorRole.Light: SURFACE,
    QPalette.ColorRole.Midlight: "#E9ECF0",
    QPalette.ColorRole.Mid: "#B8BFC9",
    QPalette.ColorRole.Dark: "#8A94A3",
    QPalette.ColorRole.Shadow: "#5F6B7A",
    QPalette.ColorRole.Highlight: ACCENT,
    QPalette.ColorRole.HighlightedText: SURFACE,
    QPalette.ColorRole.Link: PRIMARY,
    QPalette.ColorRole.LinkVisited: PRIMARY,
    QPalette.ColorRole.ToolTipBase: SURFACE,
    QPalette.ColorRole.ToolTipText: TEXT,
}


def light_palette() -> QPalette:
    """Paleta clara con todos los roles definidos, igual en cualquier configuración del sistema."""
    palette = QPalette()
    for role, color in PALETTE_ROLES.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(NEUTRAL))
    return palette


def apply_style(app: QApplication) -> None:
    """Aplica el estilo Fusion con la paleta clara y la hoja de estilo común."""
    # La interfaz se diseñó en claro: se pide el esquema claro aunque Windows esté en modo oscuro.
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")
    app.setPalette(light_palette())
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLESHEET)
    configure_matplotlib()


def configure_matplotlib() -> None:
    """Estilo de gráficos coherente con la interfaz: limpio, sin adornos."""
    mpl.rcParams.update(
        {
            "font.family": ["Segoe UI", "DejaVu Sans"],
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlepad": 8,
            "axes.titlecolor": TEXT,
            "axes.labelcolor": TEXT_MUTED,
            "axes.labelsize": 9,
            "axes.edgecolor": BORDER,
            "axes.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "grid.linestyle": "-",
            "axes.prop_cycle": mpl.cycler(color=list(SERIES)),
            "xtick.color": TEXT_MUTED,
            "ytick.color": TEXT_MUTED,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "lines.linewidth": 2,
            "lines.solid_capstyle": "round",
            "lines.solid_joinstyle": "round",
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
        }
    )
