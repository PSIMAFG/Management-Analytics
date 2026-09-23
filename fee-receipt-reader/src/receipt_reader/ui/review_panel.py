"""Pestaña de revisión: cola filtrable, incidencias, vista previa y formulario de corrección.

Aprobar, guardar una corrección, descartar o restaurar llaman al servicio de
revisión y luego avisan a la ventana (señal `data_changed`) para que
actualice los totales. Nunca se toca más de una boleta a la vez: el servicio
no propaga montos a otras boletas.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPersistentModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from receipt_reader.domain.models import ReadStatus, ReceiptStatus, Severity
from receipt_reader.domain.records import Correction, ReceiptRow
from receipt_reader.domain.reporting import ReceiptFilter
from receipt_reader.domain.rut import format_rut
from receipt_reader.errors import UNEXPECTED_ERROR, AppError, FormError
from receipt_reader.services.app_services import AppServices
from receipt_reader.services.review import ReceiptDetail, field_label
from receipt_reader.ui.dialogs import DiscardDialog, ask_confirmation, show_error
from receipt_reader.ui.formatting import format_datetime, keep_amounts_together, plural
from receipt_reader.ui.preview import PagePreview, PreviewTarget
from receipt_reader.ui.receipt_form import ReceiptForm, normalize_form_text
from receipt_reader.ui.style import BAD, GRID, NEUTRAL, SELECTION, STATUS_COLORS, SURFACE, TEXT, TEXT_MUTED, WARNING
from receipt_reader.ui.widgets import ROW_ROLE, Column, ElidedLabel, RecordTableModel, SearchProxyModel, chip
from receipt_reader.ui.workers import TaskRunner

log = logging.getLogger(__name__)

SCOPES: tuple[tuple[str, str], ...] = (
    ("review", "Por revisar"),
    ("discarded", "Descartadas"),
    ("all", "Todas las boletas"),
)
SCOPE_STATUSES: dict[str, frozenset[ReceiptStatus] | None] = {
    "review": None,
    "discarded": frozenset({ReceiptStatus.DISCARDED}),
    "all": frozenset(ReceiptStatus),
}
EMPTY_QUEUE = "No hay boletas por revisar con estos filtros."
HOVER = "#F2F6FB"


def queue_search_text(row: ReceiptRow) -> str:
    """Texto en que busca el cuadro de búsqueda de la cola."""
    parts = [str(row.id), row.issuer_name or "", row.issuer_rut or "", str(row.folio or ""), row.relative_path]
    return " ".join([*parts, row.issues_text])


def approval_hint(detail: ReceiptDetail) -> tuple[str, str]:
    """Explica por qué se puede o no aprobar la boleta. Devuelve el texto y su tono ('bad', 'warning', 'muted')."""
    record = detail.record
    if record.status is ReceiptStatus.DISCARDED:
        reason = (record.discard_reason or "sin motivo registrado").rstrip(". ")
        return f"Descartada: {reason}. Restáurela para volver a revisarla.", "muted"
    if record.status.is_valid:
        when = f" el {format_datetime(record.accepted_at)}" if record.accepted_at else ""
        return f"{record.status.label}{when}. Cuenta en los totales.", "muted"
    if record.read_status is not ReadStatus.OK and not record.edited:
        if not record.file.page_count:
            return (
                "El archivo no se pudo abrir: revíselo con Abrir original y complete los datos a mano, o descártelo.",
                "bad",
            )
        return (
            "No se pudieron leer los datos: complételos mirando la página y guarde la corrección, o descártela.",
            "bad",
        )
    if detail.approval_blockers:
        titles = ", ".join(issue.title.lower() for issue in detail.approval_blockers)
        return f"Para aprobar, corrija: {titles}.", "bad"
    accepted = [issue.title.lower() for issue in record.blocking_issues if issue.overridable]
    if accepted:
        return f"Al aprobar se darán por revisadas: {', '.join(accepted)}.", "warning"
    return "Lista para aprobar.", "muted"


def correction_text(correction: Correction) -> str:
    """Línea del historial: fecha, campo y valores antes y después."""
    before = correction.old_value if correction.old_value not in (None, "") else "vacío"
    after = correction.new_value if correction.new_value not in (None, "") else "vacío"
    if correction.field == "status":
        before = _status_label(before)
        after = _status_label(after)
    return f"{format_datetime(correction.corrected_at)}. {field_label(correction.field)}: de {before} a {after}."


def _status_label(value: str) -> str:
    try:
        return ReceiptStatus(value).label
    except ValueError:
        return value


class QueueDelegate(QStyledItemDelegate):
    """Dibuja cada boleta de la cola en tres líneas: prestador, incidencias y archivo."""

    ROW_HEIGHT = 64

    def sizeHint(self, _option: QStyleOptionViewItem, _index: QModelIndex | QPersistentModelIndex) -> QSize:
        # El ancho real lo fija la vista (modo lista): así no aparece una barra horizontal.
        return QSize(120, self.ROW_HEIGHT)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        row = index.data(ROW_ROLE)
        if not isinstance(row, ReceiptRow):
            super().paint(painter, option, index)
            return
        painter.save()
        rect = QRect(option.rect)
        state = option.state
        if state & QStyle.StateFlag.State_Selected:
            background = SELECTION
        elif state & QStyle.StateFlag.State_MouseOver:
            background = HOVER
        else:
            background = SURFACE
        painter.fillRect(rect, QColor(background))
        painter.fillRect(
            QRect(rect.left() + 4, rect.top() + 8, 3, rect.height() - 16), QColor(STATUS_COLORS[row.status.value])
        )
        painter.setPen(QColor(GRID))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        inner = rect.adjusted(16, 7, -10, -7)
        base = QFont(option.font)
        bold = QFont(base)
        bold.setBold(True)
        small = QFont(base)
        small.setPointSizeF(max(base.pointSizeF() * 0.86, 7.5))
        line_height = QFontMetrics(base).height()
        small_height = QFontMetrics(small).height()

        status_text = row.status.label
        small_metrics = QFontMetrics(small)
        status_width = small_metrics.horizontalAdvance(status_text) + 2
        painter.setFont(small)
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(
            QRect(inner.right() - status_width, inner.top(), status_width, line_height),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            status_text,
        )
        title = f"N° {row.id}   {row.issuer_name or 'Prestador sin leer'}"
        painter.setFont(bold)
        painter.setPen(QColor(TEXT))
        title_width = max(inner.width() - status_width - 8, 20)
        painter.drawText(
            QRect(inner.left(), inner.top(), title_width, line_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(bold).elidedText(title, Qt.TextElideMode.ElideRight, title_width),
        )
        issues = row.issues_text or "Sin incidencias"
        painter.setFont(base)
        painter.setPen(QColor(BAD if row.blocking_count else TEXT_MUTED))
        painter.drawText(
            QRect(inner.left(), inner.top() + line_height, inner.width(), line_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(base).elidedText(issues, Qt.TextElideMode.ElideRight, inner.width()),
        )
        painter.setFont(small)
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(
            QRect(inner.left(), inner.top() + 2 * line_height, inner.width(), small_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            small_metrics.elidedText(row.relative_path, Qt.TextElideMode.ElideMiddle, inner.width()),
        )
        painter.restore()


class IssueCard(QFrame):
    """Incidencia de la boleta: severidad, título y mensaje."""

    def __init__(
        self, title: str, message: str, severity: Severity, overridable: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("issueCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        if severity is Severity.BLOCKING:
            head.addWidget(chip("Bloqueante", BAD))
        else:
            head.addWidget(chip("Advertencia", WARNING))
        if overridable:
            head.addWidget(chip("Aceptable al aprobar", NEUTRAL))
        head.addStretch(1)
        layout.addLayout(head)
        title_label = QLabel(title, objectName="subsectionTitle")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)
        body = QLabel(keep_amounts_together(message))
        body.setWordWrap(True)
        body.setObjectName("muted")
        layout.addWidget(body)


class ReviewPanel(QWidget):
    """Cola de revisión con la página exacta y el formulario de la boleta seleccionada."""

    data_changed = Signal()
    message = Signal(str)

    def __init__(self, services: AppServices, preview_tasks: TaskRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.unattended = False
        self._base = ReceiptFilter()
        self._detail: ReceiptDetail | None = None
        self._current_id: int | None = None
        self._last_position = 0
        self._processing = False
        settings = services.catalog.settings()

        self.queue_model = RecordTableModel([Column("id", "N°", str, numeric=True)], self)
        self.queue_proxy = SearchProxyModel(queue_search_text, self)
        self.queue_proxy.setSourceModel(self.queue_model)
        self.queue_model.modelAboutToBeReset.connect(self.queue_proxy.clear_cache)
        self.queue_proxy.sort(0, Qt.SortOrder.AscendingOrder)

        self.preview = PagePreview(services.review.page_image, preview_tasks, self)
        self.form = ReceiptForm(
            services.catalog.programs(),
            services.catalog.retention_rates(),
            ocr_threshold=settings.ocr_min_confidence,
            retention_tolerance=settings.retention_tolerance,
            parent=self,
        )
        self.form.state_changed.connect(self._update_actions)
        self.form.confirm_name_requested.connect(self.confirm_provider_name)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.addWidget(self._build_queue_side())
        splitter.addWidget(self._wrap(self.preview))
        splitter.addWidget(self._build_form_side())
        splitter.setStretchFactor(0, 28)
        splitter.setStretchFactor(1, 34)
        splitter.setStretchFactor(2, 38)
        splitter.setSizes([280, 330, 400])
        self.splitter = splitter
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(splitter)
        self._install_shortcuts()
        self._reload_issue_options()
        self._show(None)

    # Construcción

    @staticmethod
    def _wrap(widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        container.setMinimumWidth(240)
        return container

    def _build_queue_side(self) -> QWidget:
        side = QSplitter(Qt.Orientation.Vertical)
        side.setChildrenCollapsible(False)
        side.setHandleWidth(10)
        side.setMinimumWidth(240)

        queue_box = QWidget()
        layout = QVBoxLayout(queue_box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel("Cola de revisión", objectName="sectionTitle"))
        head.addStretch(1)
        self.count_label = QLabel("", objectName="muted")
        head.addWidget(self.count_label)
        layout.addLayout(head)
        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.scope_combo = QComboBox()
        for key, label in SCOPES:
            self.scope_combo.addItem(label, key)
        self.scope_combo.setToolTip("Qué boletas mostrar en la cola")
        self.scope_combo.currentIndexChanged.connect(self.reload)
        filters.addWidget(self.scope_combo)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Buscar...")
        self.search_edit.setToolTip("Busca por número, nombre, RUT, folio, archivo o incidencia")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search)
        filters.addWidget(self.search_edit, 1)
        layout.addLayout(filters)
        self.issue_combo = QComboBox()
        self.issue_combo.setToolTip("Filtrar la cola por tipo de incidencia")
        self.issue_combo.currentIndexChanged.connect(self.reload)
        layout.addWidget(self.issue_combo)

        self.queue_view = QListView()
        self.queue_view.setModel(self.queue_proxy)
        self.queue_view.setItemDelegate(QueueDelegate(self.queue_view))
        self.queue_view.setUniformItemSizes(True)
        self.queue_view.setMouseTracking(True)
        self.queue_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.queue_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.queue_view.selectionModel().currentChanged.connect(self._on_current_changed)
        layout.addWidget(self.queue_view, 1)
        self.empty_label = QLabel(EMPTY_QUEUE, objectName="muted")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)
        side.addWidget(queue_box)

        details = QScrollArea(objectName="plain")
        details.setWidgetResizable(True)
        details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        details.viewport().setObjectName("plainViewport")
        content = QWidget()
        content.setObjectName("plainViewport")
        details_layout = QVBoxLayout(content)
        details_layout.setContentsMargins(0, 4, 6, 0)
        details_layout.setSpacing(6)
        details_layout.addWidget(QLabel("Incidencias de la boleta", objectName="sectionTitle"))
        self.issues_layout = QVBoxLayout()
        self.issues_layout.setSpacing(6)
        details_layout.addLayout(self.issues_layout)
        details_layout.addWidget(QLabel("Historial de cambios", objectName="sectionTitle"))
        self.history_label = QLabel("", objectName="small")
        self.history_label.setWordWrap(True)
        self.history_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_layout.addWidget(self.history_label)
        details_layout.addStretch(1)
        details.setWidget(content)
        side.addWidget(details)
        side.setStretchFactor(0, 58)
        side.setStretchFactor(1, 42)
        side.setSizes([380, 280])
        return side

    def _build_form_side(self) -> QWidget:
        side = QWidget()
        side.setMinimumWidth(340)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.headline = QLabel("", objectName="headline")
        head.addWidget(self.headline)
        self.status_chip = chip("", NEUTRAL)
        head.addWidget(self.status_chip)
        head.addStretch(1)
        layout.addLayout(head)
        self.file_label = ElidedLabel("", object_name="small")
        layout.addWidget(self.file_label)
        self.hint_label = QLabel("", objectName="muted")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.form, 1)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(6)
        buttons.setVerticalSpacing(6)
        self.approve_button = QPushButton("Aprobar", objectName="primary")
        self.approve_button.clicked.connect(self.approve)
        self.save_button = QPushButton("Guardar corrección")
        self.save_button.clicked.connect(self.save_correction)
        self.discard_button = QPushButton("Descartar...")
        self.discard_button.clicked.connect(self.discard)
        self.restore_button = QPushButton("Restaurar")
        self.restore_button.clicked.connect(self.restore)
        self.next_button = QPushButton("Siguiente")
        self.next_button.clicked.connect(lambda: self.select_relative(1))
        buttons.addWidget(self.approve_button, 0, 0)
        buttons.addWidget(self.save_button, 0, 1)
        buttons.addWidget(self.discard_button, 1, 0)
        buttons.addWidget(self.restore_button, 1, 0)
        buttons.addWidget(self.next_button, 1, 1)
        layout.addLayout(buttons)
        self.approve_button.setToolTip("Aprobar la boleta (Ctrl+Enter)")
        self.save_button.setToolTip("Guardar los campos editados y volver a validar (Ctrl+S)")
        self.discard_button.setToolTip("Descartar la boleta indicando el motivo")
        self.next_button.setToolTip("Pasar a la siguiente boleta de la cola sin cambios (Ctrl+flecha abajo)")
        return side

    def _install_shortcuts(self) -> None:
        bindings: tuple[tuple[str, Callable[[], None]], ...] = (
            ("Ctrl+Return", self.approve),
            ("Ctrl+Enter", self.approve),
            ("Ctrl+S", self.save_correction),
            ("Ctrl+Down", lambda: self.select_relative(1)),
            ("Ctrl+Up", lambda: self.select_relative(-1)),
        )
        for keys, slot in bindings:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

    # Filtros y carga de la cola

    def _scope(self) -> str:
        return str(self.scope_combo.currentData() or "review")

    def set_base_filter(self, filt: ReceiptFilter) -> None:
        """Programa, período y eje que vienen del panel lateral; recarga la cola."""
        self._base = filt
        self.reload()

    def queue_filter(self) -> ReceiptFilter:
        issue_code = self.issue_combo.currentData()
        return ReceiptFilter(
            program_id=self._base.program_id,
            statuses=SCOPE_STATUSES[self._scope()],
            period=self._base.period,
            axis=self._base.axis,
            issue_code=str(issue_code) if issue_code else None,
        )

    def _reload_issue_options(self) -> None:
        current = self.issue_combo.currentData()
        try:
            options = self.services.review.issue_options(self._base)
        except AppError as error:
            log.warning("No se pudieron leer las incidencias: %s", error.user_message)
            options = []
        self.issue_combo.blockSignals(True)
        self.issue_combo.clear()
        self.issue_combo.addItem("Todas las incidencias", None)
        for option in options:
            self.issue_combo.addItem(f"{option.title} ({option.count})", option.code)
        index = self.issue_combo.findData(current) if current else 0
        self.issue_combo.setCurrentIndex(max(index, 0))
        self.issue_combo.blockSignals(False)

    def reload(self) -> None:
        """Vuelve a leer la cola conservando la boleta seleccionada (o la que quedó en su lugar)."""
        self._reload_issue_options()
        try:
            rows = self.services.review.queue(self.queue_filter())
        except AppError as error:
            self._error(error.user_message)
            return
        keep_id = self._current_id
        position = self._last_position
        with_edits = self.form.is_dirty()
        self.queue_view.selectionModel().blockSignals(True)
        self.queue_model.set_rows(rows)
        self.queue_proxy.sort(0, Qt.SortOrder.AscendingOrder)
        self.queue_view.selectionModel().blockSignals(False)
        visible = self.queue_proxy.rowCount()
        self._update_count()
        if keep_id is not None and self._proxy_row_of(keep_id) is not None:
            self._select_id(keep_id, silent=True)
            if not with_edits:
                self._show(keep_id)
        elif visible:
            self._select_proxy_row(min(position, visible - 1))
        else:
            self._show(None)

    def _update_count(self) -> None:
        visible = self.queue_proxy.rowCount()
        total = self.queue_model.rowCount()
        text = plural(total, "boleta")
        if visible != total:
            text = f"{visible} de {text}"
        self.count_label.setText(text)
        self.empty_label.setVisible(visible == 0)
        self.empty_label.setText(EMPTY_QUEUE if total == 0 else "Ninguna boleta coincide con la búsqueda.")

    def _on_search(self, text: str) -> None:
        self.queue_proxy.set_search(text)
        self._update_count()
        if self._current_id is None or self._proxy_row_of(self._current_id) is None:
            if self.queue_proxy.rowCount():
                self._select_proxy_row(0)
            else:
                self._show(None)

    def _proxy_row_of(self, receipt_id: int) -> int | None:
        for row in range(self.queue_proxy.rowCount()):
            if self.queue_proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) == receipt_id:
                return row
        return None

    def _select_proxy_row(self, row: int) -> None:
        index = self.queue_proxy.index(row, 0)
        if index.isValid():
            self.queue_view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.queue_view.scrollTo(index)

    def _select_id(self, receipt_id: int, *, silent: bool = False) -> bool:
        row = self._proxy_row_of(receipt_id)
        if row is None:
            return False
        selection = self.queue_view.selectionModel()
        if silent:
            selection.blockSignals(True)
        self._select_proxy_row(row)
        if silent:
            selection.blockSignals(False)
            self._last_position = row
        return True

    def select_receipt(self, receipt_id: int) -> bool:
        """Abre una boleta cualquiera en la revisión (si no está en la cola, muestra todas las boletas)."""
        if not self._confirm_leave():
            return False
        self.search_edit.clear()
        if self._proxy_row_of(receipt_id) is None:
            self.issue_combo.blockSignals(True)
            self.issue_combo.setCurrentIndex(0)
            self.issue_combo.blockSignals(False)
            self.scope_combo.blockSignals(True)
            self.scope_combo.setCurrentIndex(self.scope_combo.findData("all"))
            self.scope_combo.blockSignals(False)
            self._current_id = None
            self.reload()
        found = self._select_id(receipt_id, silent=True)
        if found:
            self._show(receipt_id)
        return found

    def select_relative(self, step: int) -> None:
        current = self.queue_view.currentIndex()
        row = (current.row() if current.isValid() else -1) + step
        if 0 <= row < self.queue_proxy.rowCount():
            self._select_proxy_row(row)

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        receipt_id = current.data(Qt.ItemDataRole.UserRole) if current.isValid() else None
        if receipt_id == self._current_id:
            return
        if not self._confirm_leave():
            if self._current_id is not None:
                self._select_id(self._current_id, silent=True)
            return
        self._last_position = current.row() if current.isValid() else 0
        self._show(receipt_id)

    def _confirm_leave(self) -> bool:
        if self.unattended or not self.form.is_dirty() or self._current_id is None:
            return True
        return ask_confirmation(
            self,
            f"La boleta N° {self._current_id} tiene cambios sin guardar. ¿Desea descartarlos?",
            "Cambios sin guardar",
        )

    # Detalle

    def _show(self, receipt_id: int | None) -> None:
        if receipt_id is None:
            self._detail = None
            self._current_id = None
            self.form.clear()
            self.preview.show_target(None)
            self._render_header()
            return
        try:
            detail = self.services.review.detail(receipt_id)
        except AppError as error:
            self._error(error.user_message)
            return
        self._current_id = receipt_id
        self._apply_detail(detail)

    def _apply_detail(self, detail: ReceiptDetail) -> None:
        self._detail = detail
        self.form.load(detail)
        self._render_header()
        record = detail.record
        target = self.preview.target
        if target is None or target.receipt_id != record.id:
            if record.file.page_count:
                caption = f"Página {record.page_index + 1} de {record.file.page_count}. {record.file.relative_path}"
            else:
                caption = f"Archivo sin páginas legibles. {record.file.relative_path}"
            self.preview.show_target(
                PreviewTarget(record.id, Path(record.file.path), record.page_index, record.file.page_count, caption)
            )

    def _render_header(self) -> None:
        detail = self._detail
        while self.issues_layout.count():
            item = self.issues_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Se oculta y se desprende ya: deleteLater solo actúa al volver al ciclo de eventos.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        if detail is None:
            self.headline.setText("Sin boleta seleccionada")
            self.status_chip.hide()
            self.file_label.set_full_text("")
            self.hint_label.setText("Elija una boleta de la cola para revisarla.")
            self.history_label.setText("")
            self.issues_layout.addWidget(QLabel("-", objectName="muted"))
            self._update_actions()
            return
        record = detail.record
        self.headline.setText(f"Boleta N° {record.id}")
        self.status_chip.setText(record.status.label)
        self.status_chip.setStyleSheet(f"background: {STATUS_COLORS[record.status.value]};")
        self.status_chip.show()
        kind = record.text_kind.label
        confidence = ""
        if record.ocr_confidence is not None:
            confidence = f", confianza {round(record.ocr_confidence * 100)} %"
        self.file_label.set_full_text(f"{record.file.file_name} ({kind}{confidence})")
        text, tone = approval_hint(detail)
        self.hint_label.setText(text)
        color = {"bad": BAD, "warning": WARNING}.get(tone, TEXT_MUTED)
        self.hint_label.setStyleSheet(f"color: {color};")
        issues = sorted(record.issues, key=lambda issue: (not issue.blocking, issue.title))
        if not issues:
            self.issues_layout.addWidget(QLabel("La boleta no tiene incidencias.", objectName="muted"))
        for issue in issues:
            self.issues_layout.addWidget(IssueCard(issue.title, issue.message, issue.severity, issue.overridable))
        if detail.corrections:
            lines = [correction_text(item) for item in sorted(detail.corrections, key=lambda c: c.corrected_at)]
            self.history_label.setText("\n".join(lines))
        else:
            self.history_label.setText("Sin cambios manuales.")
        self._update_actions()

    def _update_actions(self) -> None:
        detail = self._detail
        has = detail is not None
        discarded = has and detail.record.status is ReceiptStatus.DISCARDED
        dirty = self.form.is_dirty()
        errors = self.form.has_errors() if dirty else False
        can_approve = bool(has and detail.can_approve and not dirty and not self._processing)
        self.approve_button.setEnabled(can_approve)
        self.save_button.setEnabled(bool(has and dirty and not errors and not discarded and not self._processing))
        self.discard_button.setVisible(not discarded)
        self.discard_button.setEnabled(bool(has and not discarded and not self._processing))
        self.restore_button.setVisible(bool(discarded))
        self.restore_button.setEnabled(bool(discarded and not self._processing))
        current = self.queue_view.currentIndex()
        self.next_button.setEnabled(current.isValid() and current.row() < self.queue_proxy.rowCount() - 1)
        if has and not can_approve:
            if dirty:
                self.approve_button.setToolTip("Guarde la corrección antes de aprobar.")
            else:
                self.approve_button.setToolTip(approval_hint(detail)[0])
        else:
            self.approve_button.setToolTip("Aprobar la boleta (Ctrl+Enter)")

    def set_processing(self, processing: bool) -> None:
        """Mientras se procesa una carpeta se suspenden las vistas previas y las acciones de revisión."""
        self._processing = processing
        self.preview.set_blocked(
            "La vista previa se mostrará cuando termine el procesamiento de la carpeta." if processing else None
        )
        self._update_actions()

    def reload_catalog(self) -> None:
        settings = self.services.catalog.settings()
        self.form.set_catalog(
            self.services.catalog.programs(),
            self.services.catalog.retention_rates(),
            settings.ocr_min_confidence,
            settings.retention_tolerance,
        )

    # Acciones

    def _error(self, message: str) -> None:
        log.warning("%s", message)
        if not self.unattended:
            show_error(self, message)

    def _run(self, action: Callable[[], ReceiptDetail], done: Callable[[ReceiptDetail], str]) -> bool:
        try:
            detail = action()
        except AppError as error:
            self._error(error.user_message)
            return False
        except Exception:
            log.exception("Error inesperado en la revisión")
            self._error(UNEXPECTED_ERROR)
            return False
        self._apply_detail(detail)
        self.message.emit(done(detail))
        self.data_changed.emit()
        return True

    def approve(self) -> None:
        detail = self._detail
        if detail is None or not self.approve_button.isEnabled():
            return
        accepted = [issue.title.lower() for issue in detail.record.blocking_issues if issue.overridable]
        if accepted and not self.unattended:
            question = (
                f"Se aprobará la boleta N° {detail.record.id} dando por revisado: {', '.join(accepted)}. ¿Continuar?"
            )
            if not ask_confirmation(self, question, "Aprobar boleta"):
                return
        receipt_id = detail.record.id
        self._run(lambda: self.services.review.approve(receipt_id), lambda _d: f"Boleta N° {receipt_id} aprobada.")

    def save_correction(self) -> None:
        detail = self._detail
        if detail is None or self._processing:
            return
        errors = self.form.errors()
        if errors:
            self._error("Corrija los campos marcados en rojo: " + "; ".join(errors.values()))
            return
        values = self.form.changed_values()
        if not values:
            self.message.emit("No hay cambios que guardar.")
            return
        receipt_id = detail.record.id
        try:
            updated = self.services.review.save_correction(receipt_id, values)
        except FormError as error:
            self.form.apply_errors(error.field_errors)
            self._error(error.user_message)
            return
        except AppError as error:
            self._error(error.user_message)
            return
        except Exception:
            log.exception("Error inesperado al guardar la corrección")
            self._error(UNEXPECTED_ERROR)
            return
        fields = ", ".join(field_label(name).lower() for name in values)
        self._apply_detail(updated)
        status = updated.record.status.label.lower()
        self.message.emit(f"Corrección guardada en la boleta N° {receipt_id} ({fields}). Estado: {status}.")
        self.data_changed.emit()

    def discard(self) -> None:
        detail = self._detail
        if detail is None or self._processing or detail.record.status is ReceiptStatus.DISCARDED:
            return
        dialog = DiscardDialog(f"La boleta N° {detail.record.id}", self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        receipt_id = detail.record.id
        dialog.accepted.connect(lambda: self.discard_with_reason(receipt_id, dialog.reason()))
        dialog.open()

    def discard_with_reason(self, receipt_id: int, reason: str) -> bool:
        return self._run(
            lambda: self.services.review.discard(receipt_id, reason),
            lambda _d: f"Boleta N° {receipt_id} descartada: {reason}.",
        )

    def restore(self) -> None:
        detail = self._detail
        if detail is None or detail.record.status is not ReceiptStatus.DISCARDED:
            return
        receipt_id = detail.record.id
        self._run(
            lambda: self.services.review.restore(receipt_id),
            lambda d: f"Boleta N° {receipt_id} restaurada. Estado: {d.record.status.label.lower()}.",
        )

    def confirm_provider_name(self, rut: str, name: str) -> None:
        try:
            provider = self.services.review.confirm_provider_name(rut, name)
        except AppError as error:
            self._error(error.user_message)
            return
        self.message.emit(f"Nombre confirmado para el RUT {format_rut(provider.rut)}: {provider.canonical_name}.")

    # Consultas y autoprueba

    @property
    def detail(self) -> ReceiptDetail | None:
        return self._detail

    def queue_rows(self) -> list[Any]:
        return self.queue_model.rows()

    def autotest(self, wait: Callable[[], None]) -> list[str]:
        """Selecciona la primera boleta de la cola y revisa detalle, formulario, validación y vista previa."""
        problems: list[str] = []
        if self.queue_proxy.rowCount() == 0:
            return ["La cola de revisión está vacía."]
        self._select_proxy_row(0)
        detail = self._detail
        if detail is None:
            return ["No se pudo abrir el detalle de la primera boleta de la cola."]
        values = self.form.values()
        mismatched = [
            name
            for name, text in detail.form_values.items()
            if normalize_form_text(name, values.get(name)) != normalize_form_text(name, text)
        ]
        if mismatched:
            problems.append(f"El formulario no muestra los datos de la boleta: {', '.join(mismatched)}.")
        original = values["gross"]
        self.form.set_field_text("gross", "12,5")
        if not self.form.field_error_text("gross"):
            problems.append("El formulario no marcó como inválido un monto con decimales.")
        self.form.set_field_text("gross", original)
        if self.form.is_dirty():
            problems.append("El formulario quedó con cambios después de restaurar el valor original.")
        if detail.record.read_status is ReadStatus.OK:
            wait()
            QApplication.processEvents()
            if not self.preview.has_image():
                problems.append("No se pudo mostrar la vista previa de la primera boleta de la cola.")
        return problems
