"""Pestaña Posiciones: tabla de la dotación del escenario con alta, edición y baja."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from staffing_simulator.domain.text import plural
from staffing_simulator.ui.formatting import format_clp, format_date
from staffing_simulator.ui.style import TEXT_MUTED
from staffing_simulator.ui.widgets import (
    Column,
    RecordTableModel,
    make_table_view,
    muted_label,
    proxy_of,
    selected_records,
)

EMPTY_SCENARIO_HINT = (
    "El escenario no tiene posiciones. Use Agregar posición o Importar posiciones desde el panel lateral."
)


def _holder_tone(value: Any) -> str | None:
    return TEXT_MUTED if str(value).endswith(("Vacante", "vacantes")) else None


POSITION_COLUMNS = (
    Column("role", "Cargo", tooltip_key="tooltip"),
    Column("contract", "Tipo de contrato", tooltip_key="tooltip"),
    Column("site", "Sede", tooltip_key="tooltip"),
    Column("program", "Programa", tooltip_key="tooltip"),
    Column("holder", "Persona", tone=_holder_tone, tooltip_key="tooltip"),
    Column("workload", "Jornada", numeric=True, sort_key="workload_sort", tooltip_key="tooltip"),
    Column("start", "Inicio", format_date, tooltip_key="tooltip"),
    Column("end_text", "Término", sort_key="end_sort", tooltip_key="tooltip"),
    Column("total", "Costo anual", format_clp, numeric=True, tooltip_key="tooltip"),
)


class PositionsTab(QWidget):
    add_requested = Signal()
    edit_requested = Signal(int)
    delete_requested = Signal(list)
    warnings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel("Posiciones", objectName="pageTitle")
        self.description_label = muted_label("", wrap=False)
        self.description_label.setMinimumWidth(40)
        header.addWidget(self.title_label)
        header.addSpacing(8)
        header.addWidget(self.description_label, 1)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Buscar por cargo, persona, sede o programa")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(340)
        self.add_button = QPushButton("Agregar posición", objectName="primary")
        self.edit_button = QPushButton("Editar")
        self.delete_button = QPushButton("Eliminar")
        self.warnings_button = QPushButton("Sin advertencias", objectName="warningLink")
        self.warnings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.warnings_button.setToolTip(
            "Jornada por persona, posiciones sin vigencia en el año y parámetros faltantes del escenario"
        )
        toolbar.addWidget(self.search_edit, 1)
        toolbar.addSpacing(6)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.warnings_button)
        layout.addLayout(toolbar)

        self.model = RecordTableModel(POSITION_COLUMNS, self)
        self.view = make_table_view(self.model, self, stretch_column=4)
        self.view.doubleClicked.connect(self._edit_current)
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        layout.addWidget(self.view, 1)
        self.summary_label = muted_label("", wrap=False)
        layout.addWidget(self.summary_label)

        self.search_edit.textChanged.connect(proxy_of(self.view).setFilterFixedString)
        self.search_edit.textChanged.connect(self._update_summary)
        self.add_button.clicked.connect(self.add_requested)
        self.edit_button.clicked.connect(self._edit_current)
        self.delete_button.clicked.connect(self._delete_selected)
        self.warnings_button.clicked.connect(self.warnings_requested)
        # Supr solo actúa con el foco en la tabla: en otro control (por ejemplo, las listas del panel
        # lateral) no debe pedir eliminar la posición seleccionada.
        self.delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self.view, self._delete_selected)
        self.delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._summary = ""
        self._update_buttons()

    def set_scenario(self, name: str, description: str) -> None:
        self.title_label.setText(f"Posiciones de «{name}»")
        self.description_label.setText(description)
        self.description_label.setToolTip(description)

    def set_rows(self, rows: Sequence[dict[str, Any]], summary: str, warnings: int) -> None:
        self.model.set_rows(rows)
        self._summary = summary
        self._update_summary()
        text = plural(warnings, "advertencia", "advertencias") if warnings else "Sin advertencias"
        self.warnings_button.setText(text)
        self.warnings_button.setEnabled(bool(warnings))
        self._update_buttons()

    def set_enabled_actions(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        self._update_buttons()

    def selected_ids(self) -> list[int]:
        return [int(row["id"]) for row in selected_records(self.view, self.model)]

    def _update_summary(self) -> None:
        visible = proxy_of(self.view).rowCount()
        total = self.model.rowCount()
        if total == 0:
            # Sin escenario no hay nada que agregar; con un escenario vacío se indica cómo empezar.
            self.summary_label.setText(EMPTY_SCENARIO_HINT if self.add_button.isEnabled() else self._summary)
            return
        text = self._summary
        if visible != total:
            text = f"Se muestran {visible} de {total} registros. {text}"
        self.summary_label.setText(f"{text} Doble clic en una fila para editarla.".strip())

    def _update_buttons(self) -> None:
        count = len(self.view.selectionModel().selectedRows())
        self.edit_button.setEnabled(count == 1 and self.add_button.isEnabled())
        self.delete_button.setEnabled(count >= 1 and self.add_button.isEnabled())

    def _edit_current(self) -> None:
        ids = self.selected_ids()
        if len(ids) == 1:
            self.edit_requested.emit(ids[0])

    def _delete_selected(self) -> None:
        ids = self.selected_ids()
        if ids and self.add_button.isEnabled():
            self.delete_requested.emit(ids)
