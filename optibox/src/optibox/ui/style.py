"""Paleta, hoja de estilo Qt y estilo de matplotlib compartidos por la interfaz."""

from __future__ import annotations

import warnings

import matplotlib as mpl
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

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

# Serie categórica validada para daltonismo (orden fijo: el color sigue a la entidad, nunca a su posición).
SERIES = ("#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#008300", "#4A3AA7", "#E34948")

# Elementos de apoyo de los gráficos: grilla, pistas de fondo (capacidad o requerido) y horario cerrado.
GRID = "#E6E9EE"
TRACK = "#E1E5EA"
BASELINE = "#A9B0BA"
CLOSED = "#F1F3F5"
EDITED = "#FFF3D6"

# Rampas secuenciales de un solo tono (claro = poco, oscuro = mucho).
BLUE_RAMP = ("#F2F7FD", "#CDE2FB", "#9EC5F4", "#6DA7EC", "#3987E5", "#256ABF", "#184F95", "#0D366B")
ORANGE_RAMP = ("#FDF6F1", "#FADCC8", "#F5B993", "#EE945F", "#DD6F36", "#B9531F", "#8C3C14")

TONE_COLORS = {"neutral": TEXT, "good": GOOD, "warning": WARNING, "bad": BAD, "muted": TEXT_MUTED}

STYLESHEET = f"""
QMainWindow, QWidget#central {{ background: {BACKGROUND}; }}
QFrame#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#kpiCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#kpiTitle {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#kpiValue {{ font-size: 16pt; font-weight: 600; }}
QLabel#kpiCaption {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#sectionTitle {{ color: {PRIMARY}; font-size: 10pt; font-weight: 600; }}
QLabel#hint {{ color: {TEXT_MUTED}; font-size: 8.5pt; }}
QLabel#runInfo {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#formError {{ color: {BAD}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {SURFACE}; border-radius: 4px; }}
QTabBar::tab {{ padding: 6px 14px; color: {TEXT_MUTED}; background: transparent; border: none; }}
QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 2px solid {PRIMARY}; font-weight: 600; }}
QPushButton {{ padding: 5px 12px; border: 1px solid {BORDER}; border-radius: 4px; background: {SURFACE}; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {NEUTRAL}; }}
QPushButton#primary {{ background: {PRIMARY}; color: white; border-color: {PRIMARY}; font-weight: 600; }}
QPushButton#primary:disabled {{ background: {NEUTRAL}; border-color: {NEUTRAL}; color: white; }}
QTableView {{ background: {SURFACE}; gridline-color: {BORDER}; selection-background-color: #D6E6F5;
              selection-color: {TEXT}; alternate-background-color: #F8F9FB; }}
QHeaderView::section {{ background: #EEF1F5; color: {TEXT}; padding: 4px 5px; border: none;
                        border-bottom: 1px solid {BORDER}; font-weight: 600; }}
QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px; }}
QListWidget::item {{ padding: 4px 2px; border-bottom: 1px solid #EEF1F5; }}
QListWidget::item:selected {{ background: #D6E6F5; color: {TEXT}; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 6px; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {TEXT}; selection-background-color: #D6E6F5; }}
QComboBox::drop-down, QDateEdit::drop-down {{ width: 18px; border-left: 1px solid {BORDER}; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ width: 0; height: 0; margin-right: 6px;
    border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid {TEXT_MUTED}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    width: 16px; border-left: 1px solid {BORDER}; background: {SURFACE}; }}
QAbstractSpinBox::up-arrow {{ width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent; border-bottom: 5px solid {TEXT_MUTED}; }}
QAbstractSpinBox::down-arrow {{ width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid {TEXT_MUTED}; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 4px; background: {SURFACE}; text-align: center;
                height: 16px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QSplitter::handle {{ background: transparent; }}
QStatusBar {{ color: {TEXT_MUTED}; }}
"""


def apply_style(app: QApplication) -> None:
    """Aplica el estilo Fusion con la paleta clara y la hoja de estilo común."""
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#F8F9FB"))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLESHEET)
    configure_matplotlib()


def configure_matplotlib() -> None:
    """Estilo de gráficos coherente con la interfaz: limpio, sin adornos."""
    # Mientras una pestaña oculta aún no tiene su tamaño final el diseño no cabe; se ajusta al mostrarse.
    warnings.filterwarnings("ignore", message="constrained_layout not applied", category=UserWarning)
    mpl.rcParams.update(
        {
            "font.family": ["Segoe UI", "DejaVu Sans"],
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlepad": 8,
            "axes.labelcolor": TEXT_MUTED,
            "axes.labelsize": 9,
            "axes.edgecolor": BORDER,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.prop_cycle": mpl.cycler(color=list(SERIES)),
            "xtick.color": TEXT_MUTED,
            "ytick.color": TEXT_MUTED,
            "xtick.labelcolor": TEXT,
            "ytick.labelcolor": TEXT,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "text.color": TEXT,
        }
    )
