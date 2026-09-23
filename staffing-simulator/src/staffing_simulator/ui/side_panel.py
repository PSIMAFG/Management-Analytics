"""Panel lateral: escenarios, supuestos del escenario, selección para comparar y acciones de Excel."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from staffing_simulator.domain.comparison import MAX_COMPARED
from staffing_simulator.domain.models import PartialMonthMethod, Program, Scenario, ScenarioDraft
from staffing_simulator.ui.forms import (
    combo,
    int_spin,
    percent_spin,
    scenario_choices,
    select_data,
    spin_fraction,
)
from staffing_simulator.ui.widgets import muted_label, panel, section_title

PANEL_WIDTH = 292
MIN_VISIBLE_ROWS = 3
# Con más escenarios la lista se desplaza por dentro: así el panel cabe en alto con la ventana a 1366x860.
MAX_VISIBLE_ROWS = 4

METHOD_TOOLTIP = (
    "Proporcional: el mes de ingreso o de término se paga en proporción a lo vigente. Honorarios con jornada: "
    "bruto x horas programadas vigentes / horas programadas del mes (calendario real). Honorarios por horas: "
    "por días hábiles vigentes. Contratos dependientes: por días corridos vigentes.\n"
    "Mes completo: cualquier mes con vigencia se paga entero."
)
ABSENCE_TOOLTIP = (
    "Porcentaje de las horas contratadas del mes que no se trabajan (horas semanales x semanas por mes, o las "
    "horas mensuales estimadas). Se descuenta a los honorarios al valor hora, con tope en el bruto. No reduce "
    "el costo de los contratos dependientes."
)


def _button(text: str, tooltip: str = "", *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setToolTip(tooltip)
    if primary:
        button.setObjectName("primary")
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return button


def fit_list_height(view: QListWidget, rows: int) -> None:
    """Alto de la lista para mostrar entre MIN_VISIBLE_ROWS y MAX_VISIBLE_ROWS filas; el resto se desplaza."""
    view.ensurePolished()
    visible = max(MIN_VISIBLE_ROWS, min(rows, MAX_VISIBLE_ROWS))
    row_height = max(view.sizeHintForRow(0) if view.count() else 0, view.fontMetrics().height() + 10)
    view.setFixedHeight(visible * row_height + 2 * view.frameWidth() + 2)


class SidePanel(QScrollArea):
    """Controles del escenario seleccionado y acciones generales.

    El contenido tiene siempre el ancho PANEL_WIDTH y el panel reserva a su
    derecha el ancho de la barra de desplazamiento vertical: si la barra
    aparece (ventana baja o muchos escenarios), ocupa esa franja en vez de
    angostar los controles, y nunca se cortan textos. El ancho no cambia
    mientras se usa, así que el contenido no se vuelve a acomodar.
    """

    scenario_changed = Signal()
    program_changed = Signal()
    new_requested = Signal()
    duplicate_requested = Signal()
    rename_requested = Signal()
    delete_requested = Signal()
    apply_requested = Signal()
    compare_changed = Signal()
    import_positions_requested = Signal()
    import_execution_requested = Signal()
    export_requested = Signal()
    positions_template_requested = Signal()
    execution_template_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sideScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._scenario: Scenario | None = None
        self._all_scenarios: list[Scenario] = []
        self._loading = False

        frame = panel()
        frame.setFixedWidth(PANEL_WIDTH)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)
        self._build_scenarios(layout)
        layout.addSpacing(4)
        self._build_program_filter(layout)
        layout.addSpacing(4)
        self._build_assumptions(layout)
        layout.addSpacing(4)
        self._build_compare(layout)
        layout.addSpacing(4)
        self._build_excel(layout)
        layout.addStretch(1)
        self.setWidget(frame)
        scrollbar = self.verticalScrollBar()
        scrollbar.ensurePolished()
        self.setFixedWidth(PANEL_WIDTH + scrollbar.sizeHint().width())
        self._update_dirty()

    # Construcción por secciones

    def _build_scenarios(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_title("Escenarios"))
        self.scenario_list = QListWidget(objectName="scenarioList")
        fit_list_height(self.scenario_list, MIN_VISIBLE_ROWS)
        self.scenario_list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self.scenario_list)
        buttons = QGridLayout()
        buttons.setSpacing(6)
        self.new_button = _button("Nuevo", "Crear un escenario vacío")
        self.duplicate_button = _button("Duplicar", "Copiar el escenario con todas sus posiciones")
        self.rename_button = _button("Renombrar", "Cambiar el nombre y la descripción")
        self.delete_button = _button("Eliminar", "Eliminar el escenario y sus posiciones")
        actions = (
            (self.new_button, self.new_requested),
            (self.duplicate_button, self.duplicate_requested),
            (self.rename_button, self.rename_requested),
            (self.delete_button, self.delete_requested),
        )
        for index, (button, signal) in enumerate(actions):
            button.clicked.connect(signal)
            buttons.addWidget(button, index // 2, index % 2)
        layout.addLayout(buttons)

    def _build_program_filter(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_title("Convenio"))
        self.program_combo = combo([])
        self.program_combo.setToolTip(
            "Filtra los totales, tablas y gráficos por convenio (en Comparación de escenarios, "
            "compara solo ese convenio)."
        )
        self.program_combo.currentIndexChanged.connect(self._on_program_changed)
        layout.addWidget(self.program_combo)

    def set_programs(self, programs: Sequence[Program]) -> None:
        """Rellena el filtro de convenio (Todos y cada programa) conservando la selección si sigue existiendo."""
        current = self.program_combo.currentData()
        self._loading = True
        try:
            self.program_combo.clear()
            self.program_combo.addItem("Todos", None)
            for program in programs:
                self.program_combo.addItem(program.name, program.id)
            if not select_data(self.program_combo, current):
                self.program_combo.setCurrentIndex(0)
        finally:
            self._loading = False

    def program_id(self) -> int | None:
        value = self.program_combo.currentData()
        return None if value is None else int(value)

    def _on_program_changed(self) -> None:
        if not self._loading:
            self.program_changed.emit()

    def _build_assumptions(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_title("Supuestos del escenario"))
        form = QFormLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.year_spin = int_spin(2000, 2100, 2026)
        self.method_combo = combo([(item.label, item.value) for item in PartialMonthMethod])
        self.method_combo.setToolTip(METHOD_TOOLTIP)
        self.absence_spin = percent_spin(Decimal(0))
        self.absence_spin.setToolTip(ABSENCE_TOOLTIP)
        self.base_combo = combo(scenario_choices([]))
        self.base_combo.setToolTip("Escenario contra el que se calcula la diferencia del costo anual.")
        compact = QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        for box in (self.method_combo, self.base_combo):
            box.setSizeAdjustPolicy(compact)
            box.setMinimumContentsLength(8)
        form.addRow("Año", self.year_spin)
        form.addRow("Meses parciales", self.method_combo)
        form.addRow("Ausentismo", self.absence_spin)
        form.addRow("Base", self.base_combo)
        layout.addLayout(form)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.apply_button = _button("Aplicar cambios", primary=True)
        self.discard_button = _button("Descartar")
        self.apply_button.clicked.connect(self.apply_requested)
        self.discard_button.clicked.connect(self.discard_changes)
        actions.addWidget(self.apply_button, 3)
        actions.addWidget(self.discard_button, 2)
        layout.addLayout(actions)
        for signal in (
            self.year_spin.valueChanged,
            self.method_combo.currentIndexChanged,
            self.absence_spin.valueChanged,
            self.base_combo.currentIndexChanged,
        ):
            signal.connect(self._update_dirty)

    def _build_compare(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_title("Comparar escenarios"))
        layout.addWidget(muted_label(f"Marque entre 2 y {MAX_COMPARED} escenarios.", wrap=True))
        self.compare_list = QListWidget()
        fit_list_height(self.compare_list, MIN_VISIBLE_ROWS)
        self.compare_list.itemChanged.connect(self._on_compare_item_changed)
        layout.addWidget(self.compare_list)

    def _build_excel(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_title("Excel"))
        self.import_positions_button = _button(
            "Importar posiciones...", "Agregar posiciones al escenario desde una planilla"
        )
        self.import_execution_button = _button(
            "Importar ejecución...", "Cargar montos ejecutados por programa y mes desde una planilla"
        )
        self.export_button = _button(
            "Exportar informe...",
            "Informe completo del escenario, con la comparación de los escenarios marcados",
            primary=True,
        )
        self.templates_button = QToolButton(objectName="menuButton")
        self.templates_button.setText("Plantillas")
        self.templates_button.setToolTip("Descargar planillas en blanco para importar")
        self.templates_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.templates_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        menu = QMenu(self.templates_button)
        menu.addAction("Plantilla de posiciones...", lambda: self.positions_template_requested.emit())
        menu.addAction("Plantilla de ejecución...", lambda: self.execution_template_requested.emit())
        self.templates_button.setMenu(menu)
        self.import_positions_button.clicked.connect(self.import_positions_requested)
        self.import_execution_button.clicked.connect(self.import_execution_requested)
        self.export_button.clicked.connect(self.export_requested)
        excel = QGridLayout()
        excel.setSpacing(6)
        excel.addWidget(self.import_positions_button, 0, 0, 1, 2)
        excel.addWidget(self.import_execution_button, 1, 0, 1, 2)
        excel.addWidget(self.templates_button, 2, 0)
        excel.addWidget(self.export_button, 2, 1)
        layout.addLayout(excel)

    # Ancho y diseño

    def layout_problems(self, *, measure_text: bool = True) -> list[str]:
        """Problemas de diseño del panel (para la autoprueba).

        El ancho útil se revisa siempre; que el contenido quepa depende de las
        fuentes y solo se mide con pantalla real (`measure_text`).
        """
        content = self.widget()
        if content is None:
            return []
        problems = []
        if content.width() < PANEL_WIDTH or self.viewport().width() < content.width():
            problems.append("El panel lateral perdió ancho por su barra de desplazamiento.")
        if measure_text and content.minimumSizeHint().width() > content.width():
            problems.append("El contenido del panel lateral no cabe a lo ancho.")
        return problems

    # Escenarios

    def set_scenarios(self, scenarios: Sequence[Scenario], current_id: int | None, compared_ids: Sequence[int]) -> None:
        """Rellena las listas sin emitir señales; selecciona `current_id` (o el primero)."""
        self._loading = True
        try:
            self.scenario_list.clear()
            self.compare_list.clear()
            for scenario in scenarios:
                tooltip = f"{scenario.name} ({scenario.year})"
                if scenario.description:
                    tooltip += f"\n{scenario.description}"
                item = QListWidgetItem(scenario.name)
                item.setData(Qt.ItemDataRole.UserRole, scenario.id)
                item.setToolTip(tooltip)
                self.scenario_list.addItem(item)
                check = QListWidgetItem(scenario.name)
                check.setData(Qt.ItemDataRole.UserRole, scenario.id)
                check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = scenario.id in compared_ids
                check.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                check.setToolTip(tooltip)
                self.compare_list.addItem(check)
            target = 0
            for index in range(self.scenario_list.count()):
                if self.scenario_list.item(index).data(Qt.ItemDataRole.UserRole) == current_id:
                    target = index
            if self.scenario_list.count():
                self.scenario_list.setCurrentRow(target)
        finally:
            self._loading = False
        fit_list_height(self.scenario_list, self.scenario_list.count())
        fit_list_height(self.compare_list, self.compare_list.count())
        has_scenarios = self.scenario_list.count() > 0
        for button in (
            self.duplicate_button,
            self.rename_button,
            self.delete_button,
            self.import_positions_button,
            self.export_button,
        ):
            button.setEnabled(has_scenarios)

    def current_id(self) -> int | None:
        item = self.scenario_list.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    def select_scenario(self, scenario_id: int) -> None:
        """Selecciona un escenario de la lista sin emitir `scenario_changed`."""
        self._loading = True
        try:
            for index in range(self.scenario_list.count()):
                if self.scenario_list.item(index).data(Qt.ItemDataRole.UserRole) == scenario_id:
                    self.scenario_list.setCurrentRow(index)
        finally:
            self._loading = False

    def shown_scenario(self) -> Scenario | None:
        """Escenario cuyos supuestos muestran los campos (puede diferir de la selección mientras se cambia)."""
        return self._scenario

    def checked_ids(self) -> list[int]:
        return [
            int(self.compare_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.compare_list.count())
            if self.compare_list.item(index).checkState() == Qt.CheckState.Checked
        ]

    def set_checked(self, scenario_id: int, checked: bool) -> None:
        for index in range(self.compare_list.count()):
            item = self.compare_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == scenario_id:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    # Supuestos

    def show_assumptions(
        self, scenario: Scenario | None, scenarios: Sequence[Scenario], *, force: bool = False
    ) -> None:
        """Muestra los supuestos del escenario en los campos editables.

        Si el escenario ya estaba a la vista y tiene cambios sin aplicar (por
        ejemplo, al recalcular tras agregar una posición o cambiar un
        parámetro), se conservan los valores editados y solo se actualiza la
        lista de escenarios base. `force` descarta los cambios pendientes.
        """
        same = scenario is not None and self._scenario is not None and scenario.id == self._scenario.id
        edited = self.assumptions_draft() if same and not force and self.is_dirty() else None
        self._scenario = scenario
        self._all_scenarios = list(scenarios)
        self._loading = True
        try:
            self.base_combo.clear()
            exclude = None if scenario is None else scenario.id
            for label, value in scenario_choices(scenarios, exclude):
                self.base_combo.addItem(label, value)
            if scenario is not None:
                values = edited or scenario.to_draft()
                self.year_spin.setValue(values.year)
                select_data(self.method_combo, PartialMonthMethod(values.partial_month_method).value)
                self.absence_spin.setValue(float(values.expected_absence * 100))
                if not select_data(self.base_combo, values.base_scenario_id):
                    select_data(self.base_combo, scenario.base_scenario_id)
        finally:
            self._loading = False
        for widget in (self.year_spin, self.method_combo, self.absence_spin, self.base_combo):
            widget.setEnabled(scenario is not None)
        self._update_dirty()

    def assumptions_draft(self) -> ScenarioDraft | None:
        """Supuestos editados sobre el escenario mostrado (nombre y descripción no cambian)."""
        if self._scenario is None:
            return None
        return ScenarioDraft(
            name=self._scenario.name,
            year=self.year_spin.value(),
            description=self._scenario.description,
            partial_month_method=PartialMonthMethod(self.method_combo.currentData()),
            expected_absence=spin_fraction(self.absence_spin),
            base_scenario_id=self.base_combo.currentData(),
        )

    def is_dirty(self) -> bool:
        draft = self.assumptions_draft()
        if draft is None or self._scenario is None:
            return False
        return draft != self._scenario.to_draft()

    def discard_changes(self) -> None:
        self.show_assumptions(self._scenario, self._all_scenarios, force=True)

    def _update_dirty(self) -> None:
        if self._loading:
            return
        dirty = self.is_dirty()
        self.apply_button.setEnabled(dirty)
        self.discard_button.setEnabled(dirty)

    def _on_current_changed(self) -> None:
        if not self._loading:
            self.scenario_changed.emit()

    def _on_compare_item_changed(self, _item: QListWidgetItem) -> None:
        if not self._loading:
            self.compare_changed.emit()
