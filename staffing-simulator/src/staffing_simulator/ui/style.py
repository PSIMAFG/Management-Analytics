"""Paleta, hoja de estilo Qt y estilo de matplotlib compartidos por la interfaz."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import matplotlib as mpl
from PySide6.QtCore import QLocale
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
SELECTION = "#D6E6F5"

# Serie categórica validada (orden fijo; separación suficiente con daltonismo
# entre series vecinas). El color sigue a la entidad: se asigna por el orden
# estable de los identificadores de las entidades que muestra el gráfico (los
# tipos de contrato o los escenarios comparados), nunca por el ranking del
# momento; como un gráfico muestra menos de ocho, ningún color se repite.
SERIES = ("#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#008300", "#4A3AA7", "#E34948")

# Tinta de gráficos: una sola serie de montos, referencias y rejilla.
INK = PRIMARY
INK_SECONDARY = "#52514E"
GRID = "#E6E9EE"
AXIS = "#C3C2B7"

TONE_COLORS = {"neutral": TEXT, "good": GOOD, "warning": WARNING, "bad": BAD, "muted": TEXT_MUTED}

log = logging.getLogger(__name__)

# Íconos mínimos (flechas y marca de verificación) que la hoja de estilo necesita
# como archivo. Se generan al iniciar para no depender de recursos empaquetados.
ICONS = {
    "up": (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        f'<path d="M2 6.5 L5 3.5 L8 6.5" fill="none" stroke="{TEXT_MUTED}" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "down": (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        f'<path d="M2 3.5 L5 6.5 L8 3.5" fill="none" stroke="{TEXT_MUTED}" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "check": (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
        '<path d="M2.5 6.2 L5 8.6 L9.5 3.6" fill="none" stroke="#FFFFFF" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
}


def icon_paths(folder: Path | None = None) -> dict[str, str] | None:
    """Escribe los íconos en una carpeta temporal y devuelve sus rutas (None si no se pudo)."""
    target = folder or Path(tempfile.gettempdir()) / "staffing_simulator_ui"
    paths: dict[str, str] = {}
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name, svg in ICONS.items():
            path = target / f"{name}.svg"
            if not path.exists() or path.read_text(encoding="utf-8") != svg:
                path.write_text(svg, encoding="utf-8")
            paths[name] = path.as_posix()
    except OSError:
        log.warning("No se pudieron crear los íconos de la interfaz en %s", target)
        return None
    return paths


def icon_stylesheet(paths: dict[str, str] | None) -> str:
    """Flechas de los campos numéricos y listas desplegables, y casillas de verificación."""
    if paths is None:
        return ""
    return f"""
QAbstractSpinBox {{ padding-right: 20px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 18px; border: none;
                                                             background: transparent; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; margin-top: 2px; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 2px; }}
QAbstractSpinBox::up-arrow {{ image: url({paths["up"]}); width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ image: url({paths["down"]}); width: 10px; height: 10px; }}
QDateEdit::drop-down {{ subcontrol-origin: border; subcontrol-position: center right; width: 22px; border: none; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: border; subcontrol-position: center right; width: 22px; border: none; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url({paths["down"]}); width: 10px; height: 10px; }}
QCheckBox::indicator, QListWidget::indicator {{ width: 14px; height: 14px; border: 1px solid {NEUTRAL};
                                               border-radius: 3px; background: {SURFACE}; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY};
                                                               image: url({paths["check"]}); }}
QCheckBox::indicator:disabled {{ background: #F0F2F5; border-color: {BORDER}; }}
"""


STYLESHEET = f"""
QMainWindow, QWidget#central {{ background: {BACKGROUND}; }}
QFrame#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#kpiCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#kpiTitle {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#kpiValue {{ font-size: 15pt; font-weight: 600; }}
QLabel#kpiCaption {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#sectionTitle {{ color: {PRIMARY}; font-size: 10pt; font-weight: 600; }}
QLabel#pageTitle {{ color: {TEXT}; font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {TEXT_MUTED}; }}
QLabel#hint {{ color: {TEXT_MUTED}; font-size: 8.5pt; }}
QLabel#errorLabel {{ color: {BAD}; background: #FDECEA; border: 1px solid #F5C6C2; border-radius: 4px;
                     padding: 6px 8px; }}
QLabel#previewLabel {{ color: {TEXT}; background: #F3F6FA; border: 1px solid {BORDER}; border-radius: 4px;
                       padding: 6px 8px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {SURFACE}; border-radius: 4px; }}
QTabBar::tab {{ padding: 6px 14px; color: {TEXT_MUTED}; background: transparent; border: none; }}
QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 2px solid {PRIMARY}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabWidget#subTabs::pane {{ border: none; border-top: 1px solid {BORDER}; border-radius: 0; }}
QPushButton, QToolButton {{ padding: 5px 12px; border: 1px solid {BORDER}; border-radius: 4px;
                            background: {SURFACE}; color: {TEXT}; }}
QPushButton:hover, QToolButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {NEUTRAL}; background: #F7F8FA; }}
QPushButton#primary {{ background: {PRIMARY}; color: white; border-color: {PRIMARY}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #173E61; }}
QPushButton#primary:disabled {{ background: {NEUTRAL}; border-color: {NEUTRAL}; color: white; }}
QPushButton#warningLink {{ border: none; background: transparent; color: {WARNING}; padding: 5px 4px;
                           text-decoration: underline; }}
QPushButton#warningLink:disabled {{ color: {TEXT_MUTED}; text-decoration: none; }}
QToolButton::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
QToolButton#menuButton {{ padding-right: 22px; }}
QTableView {{ background: {SURFACE}; gridline-color: {BORDER}; selection-background-color: {SELECTION}; outline: 0;
              selection-color: {TEXT}; alternate-background-color: #F8F9FB; border: 1px solid {BORDER};
              font-size: 9pt; }}
QHeaderView {{ background: #EEF1F5; }}
QHeaderView::section {{ background: #EEF1F5; color: {TEXT}; padding: 4px 6px; border: none;
                        border-bottom: 1px solid {BORDER}; font-weight: 600; font-size: 9pt; }}
QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px; outline: none; }}
QListWidget::item {{ padding: 4px 6px; }}
QListWidget::item:selected {{ background: {SELECTION}; color: {TEXT}; }}
QListWidget#scenarioList::item:selected {{ border-left: 3px solid {PRIMARY}; font-weight: 600; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 6px; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus,
QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled {{
    color: {NEUTRAL}; background: #F7F8FA; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {TEXT}; selection-background-color: {SELECTION}; }}
QScrollArea#sideScroll {{ background: transparent; border: none; }}
QScrollArea#sideScroll > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QStatusBar {{ color: {TEXT_MUTED}; }}
QToolTip {{ color: {TEXT}; background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
"""


def configure_locale() -> None:
    """Formato chileno (coma decimal, punto de miles) en campos numéricos y fechas de Qt."""
    locale = QLocale(QLocale.Language.Spanish, QLocale.Country.Chile)
    QLocale.setDefault(locale)


def apply_style(app: QApplication) -> None:
    """Aplica el estilo Fusion con la paleta clara y la hoja de estilo común."""
    configure_locale()
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
    app.setStyleSheet(STYLESHEET + icon_stylesheet(icon_paths()))
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
            "axes.labelsize": 8.5,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 0.8,
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
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "legend.handlelength": 1.2,
            "legend.columnspacing": 1.4,
            "lines.linewidth": 2,
            "lines.solid_capstyle": "round",
            "lines.solid_joinstyle": "round",
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "text.color": TEXT,
        }
    )
