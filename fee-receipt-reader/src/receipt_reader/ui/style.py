"""Paleta, hoja de estilo Qt y estilo de matplotlib compartidos por la interfaz."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import matplotlib as mpl
from PySide6.QtCore import QLibraryInfo, QLocale, QPointF, Qt, QTranslator
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

BACKGROUND = "#F4F5F7"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#F8F9FB"
BORDER = "#D9DDE3"
GRID = "#E6E9EE"
TEXT = "#1F2933"
TEXT_MUTED = "#5F6B7A"
PRIMARY = "#1F4E79"
ACCENT = "#2E86C1"
SELECTION = "#D6E6F5"
# Relleno de la barra de progreso: claro para que el texto oscuro del avance se lea encima (contraste 7:1).
PROGRESS_CHUNK = "#8FBDE3"
GOOD = "#2E7D32"
GOOD_LIGHT = "#6FA872"
WARNING = "#B7791F"
BAD = "#C62828"
NEUTRAL = "#8A94A3"

# Paleta categórica para identificar programas en los gráficos. Se validó con
# un verificador de daltonismo (separación entre vecinos y entre todos los
# pares) y queda lejos de los colores de estado. El orden es fijo: el color
# sigue al programa según su posición en el catálogo, nunca a su ranking.
SERIES = ("#2A78D6", "#1BAF7A", "#4A3AA7", "#E87BA4")
OTHER_SERIES = NEUTRAL

TONE_COLORS = {"neutral": TEXT, "good": GOOD, "warning": WARNING, "bad": BAD, "muted": TEXT_MUTED}

# Colores de estado (semáforo). Solo se usan cuando el color significa el estado.
STATUS_COLORS = {
    "approved": GOOD,
    "corrected": GOOD_LIGHT,
    "pending": WARNING,
    "error": BAD,
    "discarded": NEUTRAL,
}


def _line_image(
    name: str, points: tuple[tuple[float, float], ...], *, size: tuple[int, int] = (20, 12), color: str = TEXT_MUTED
) -> str:
    """Dibuja un trazo (flecha o visto) en un archivo temporal: las hojas de estilo Qt solo aceptan rutas de imagen."""
    folder = Path(tempfile.gettempdir()) / "lector_boletas_ui"
    path = folder / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        pixmap = QPixmap(*size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline([QPointF(x, y) for x, y in points])
        painter.end()
        if not pixmap.save(str(path), "PNG"):
            return ""
    except OSError:
        log.warning("No se pudo crear una imagen de la interfaz", exc_info=True)
        return ""
    return path.as_posix()


def _image_rules(down: str, up: str, check: str) -> str:
    """Flechas de combos, fechas y números y el visto de las casillas (sin imagen, Qt dibuja recuadros vacíos)."""
    rules = ""
    if down and up:
        rules += f"""QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url({down}); width: 10px; height: 6px; }}
QSpinBox::up-arrow {{ image: url({up}); width: 9px; height: 5px; }}
QSpinBox::down-arrow {{ image: url({down}); width: 9px; height: 5px; }}
"""
    if check:
        rules += f"QCheckBox::indicator:checked {{ image: url({check}); }}\n"
    return rules


def build_stylesheet(arrow_down: str = "", arrow_up: str = "", check: str = "") -> str:
    arrow_rule = _image_rules(arrow_down, arrow_up, check)
    return f"""
QMainWindow, QWidget#central, QDialog {{ background: {BACKGROUND}; }}
QFrame#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#kpiCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#kpiTitle {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#kpiValue {{ font-size: 16pt; font-weight: 600; }}
QLabel#kpiCaption {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#sectionTitle {{ color: {PRIMARY}; font-size: 10pt; font-weight: 600; }}
QLabel#subsectionTitle {{ color: {TEXT}; font-size: 9pt; font-weight: 600; }}
QLabel#muted {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#small {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#headline {{ color: {TEXT}; font-size: 11pt; font-weight: 600; }}
QLabel#fieldLabel {{ color: {TEXT}; }}
QLabel#fieldSource {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QLabel#fieldSourceLow {{ color: {WARNING}; font-size: 8pt; font-weight: 600; }}
QLabel#fieldError {{ color: {BAD}; font-size: 8pt; }}
QLabel#fieldNote {{ color: {WARNING}; font-size: 8pt; }}
QLabel#blockers {{ color: {BAD}; font-size: 9pt; }}
QLabel#chip {{ border-radius: 3px; padding: 1px 6px; font-size: 8pt; font-weight: 600; color: white; }}
QFrame#issueCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px; }}
QFrame#suggestion {{ background: #EEF5FC; border: 1px solid #C9DDF2; border-radius: 4px; }}
QFrame#separator {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {SURFACE}; border-radius: 4px; }}
QTabBar::tab {{ padding: 6px 14px; color: {TEXT_MUTED}; background: transparent; border: none; }}
QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 2px solid {PRIMARY}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}
QPushButton {{ padding: 5px 12px; border: 1px solid {BORDER}; border-radius: 4px; background: {SURFACE};
              color: {TEXT}; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #A5AEB9; border-color: #E3E6EA; background: {SURFACE_ALT}; }}
QPushButton#primary {{ background: {PRIMARY}; color: white; border-color: {PRIMARY}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #255C8D; }}
QPushButton#primary:disabled {{ background: {NEUTRAL}; border-color: {NEUTRAL}; color: #EEF1F5; }}
QPushButton#link {{ border: none; background: transparent; color: {ACCENT}; padding: 0px 2px; text-align: left; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QPushButton#link:disabled {{ color: #A5AEB9; background: transparent; }}
QToolButton {{ padding: 3px 8px; border: 1px solid {BORDER}; border-radius: 4px; background: {SURFACE};
              color: {TEXT}; }}
QToolButton:hover {{ border-color: {ACCENT}; }}
QToolButton:disabled {{ color: #A5AEB9; }}
QTableView {{ background: {SURFACE}; gridline-color: {BORDER}; selection-background-color: {SELECTION};
              selection-color: {TEXT}; alternate-background-color: {SURFACE_ALT}; border: 1px solid {BORDER}; }}
QHeaderView::section {{ background: #EEF1F5; color: {TEXT}; padding: 4px 6px; border: none;
                        border-bottom: 1px solid {BORDER}; font-weight: 600; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 6px; }}
QComboBox, QDateEdit, QSpinBox {{ padding-right: 22px; }}
QComboBox::drop-down, QDateEdit::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
                                             width: 22px; border: none; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 20px; border: none;
                                             background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
{arrow_rule}QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus,
QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox:disabled, QLineEdit:disabled, QPlainTextEdit:disabled {{ color: #8792A0; background: {SURFACE_ALT}; }}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{ background: {SURFACE_ALT}; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {TEXT}; selection-background-color: {SELECTION};
                               selection-color: {TEXT}; }}
*[state="error"] {{ border: 1px solid {BAD}; background: #FDF3F3; }}
*[state="flag"] {{ border: 1px solid {WARNING}; background: #FFF9EE; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 4px; background: #EEF1F5; text-align: center;
                color: {TEXT}; font-size: 8pt; max-height: 16px; }}
QProgressBar::chunk {{ background: {PROGRESS_CHUNK}; border-radius: 3px; }}
QScrollArea#plain {{ border: none; background: transparent; }}
QWidget#plainViewport {{ background: transparent; }}
QScrollArea#preview {{ border: 1px solid {BORDER}; background: #E9ECF0; }}
QWidget#previewViewport {{ background: #E9ECF0; }}
QSplitter::handle {{ background: transparent; }}
QStatusBar {{ color: {TEXT_MUTED}; }}
QStatusBar QLabel {{ color: {TEXT_MUTED}; padding: 0px 6px; }}
QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #8C97A6; border-radius: 3px;
                       background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
"""


def apply_locale(app: QApplication) -> None:
    """Español de Chile para calendarios, números y los textos propios de Qt (menús contextuales, diálogos)."""
    locale = QLocale(QLocale.Language.Spanish, QLocale.Country.Chile)
    QLocale.setDefault(locale)
    translator = QTranslator(app)
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(locale, "qtbase", "_", folder):
        app.installTranslator(translator)
    else:
        log.info("No se encontraron las traducciones de Qt en %s; se usan los textos propios", folder)


def apply_style(app: QApplication) -> None:
    """Aplica el idioma, el estilo Fusion con la paleta clara y la hoja de estilo común."""
    apply_locale(app)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE_ALT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(NEUTRAL))
    app.setPalette(palette)
    app.setFont(QFont("Segoe UI", 10))
    down = _line_image("flecha_abajo.png", ((3, 3), (10, 9), (17, 3)))
    up = _line_image("flecha_arriba.png", ((3, 9), (10, 3), (17, 9)))
    check = _line_image("visto.png", ((3.5, 8.5), (7, 12), (14.5, 4.5)), size=(18, 16), color=SURFACE)
    app.setStyleSheet(build_stylesheet(down, up, check))
    configure_matplotlib()


def configure_matplotlib() -> None:
    """Estilo de gráficos coherente con la interfaz: limpio, sin adornos."""
    mpl.rcParams.update(
        {
            "font.family": ["Segoe UI", "DejaVu Sans"],
            "font.size": 9,
            "axes.titlesize": 10.5,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlecolor": TEXT,
            "axes.labelcolor": TEXT_MUTED,
            "axes.labelsize": 8.5,
            "axes.edgecolor": BORDER,
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
            "text.color": TEXT,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
        }
    )
