"""Pestaña de turnos sin cubrir: faltante por bloque y causa, exclusiones por regla y tabla de detalle."""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from optibox.domain.run import RunDetail
from optibox.domain.timegrid import BLOCK_MINUTES, format_range
from optibox.ui.charts import draw_unmet
from optibox.ui.formatting import format_int, format_pct
from optibox.ui.presentation import day_label, unmet_by_block
from optibox.ui.tab_base import TabPage, check_charts, splitter, vbox
from optibox.ui.widgets import (
    ChartCanvas,
    Column,
    RecordTableModel,
    hint_label,
    make_table_view,
    section_title,
    wrap_rows,
)


def unmet_rows(detail: RunDetail) -> list[dict[str, Any]]:
    """Filas de la tabla de turnos sin cubrir con nombres legibles."""
    names = {service.code: service.name for service in detail.instance.service_types}
    dates = {day.index: day.date for day in detail.instance.days}
    return [
        {
            "day": item.day,
            "day_label": day_label(item.day, dates.get(item.day)),
            "block": item.block,
            "service": names.get(item.service, item.service),
            "required": item.required,
            "covered": item.covered,
            "shortfall": item.shortfall,
            "cause": item.cause_label,
            "detail": item.detail,
        }
        for item in detail.unmet
    ]


def exclusion_rows(detail: RunDetail) -> list[dict[str, Any]]:
    """Filas de exclusiones por regla con su participación en el total excluido."""
    total = sum(exclusion.excluded for exclusion in detail.exclusions)
    return [
        {
            "rule": f"{exclusion.position}. {exclusion.description}",
            "excluded": exclusion.excluded,
            "share": exclusion.excluded / total if total else None,
        }
        for exclusion in detail.exclusions
    ]


class UnmetTab(TabPage):
    title = "Turnos sin cubrir"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chart = ChartCanvas(self, min_height=240)
        self.exclusion_model = RecordTableModel(
            [
                Column("rule", "Regla (en orden de la cadena)"),
                Column("excluded", "Excluidos", format_int, numeric=True),
                Column("share", "Participación", format_pct, numeric=True),
            ]
        )
        self.exclusion_table = make_table_view(self.exclusion_model, self, stretch_column=0)
        wrap_rows(self.exclusion_table)
        self.exclusion_hint = hint_label()
        right, right_layout = vbox(self)
        right_layout.addWidget(section_title("Exclusiones de la etapa 1 por regla"))
        right_layout.addWidget(self.exclusion_hint)
        right_layout.addWidget(self.exclusion_table, 1)

        self.model = RecordTableModel(
            [
                Column("day_label", "Día"),
                Column("block", "Bloque", lambda b: format_range(b, b + BLOCK_MINUTES)),
                Column("service", "Tipo de atención"),
                Column("required", "Requeridas", format_int, numeric=True),
                Column("covered", "Cubiertas", format_int, numeric=True),
                Column("shortfall", "Faltante", format_int, numeric=True),
                Column("cause", "Causa"),
                Column("detail", "Detalle"),
            ]
        )
        self.table = make_table_view(self.model, self)
        wrap_rows(self.table)
        bottom, bottom_layout = vbox(self)
        bottom_layout.addWidget(section_title("Detalle de los turnos sin cubrir"))
        bottom_layout.addWidget(self.table, 1)

        top = splitter(Qt.Orientation.Horizontal, [self.chart, right], [1, 1])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter(Qt.Orientation.Vertical, [top, bottom], [3, 2]), 1)

    def charts(self) -> tuple[ChartCanvas, ...]:
        return (self.chart,)

    def set_detail(self, detail: RunDetail | None) -> None:
        if detail is None:
            self.model.set_rows([])
            self.exclusion_model.set_rows([])
            self.exclusion_hint.setText("")
            self.chart.show_message("Aún no hay corridas.")
            return
        self.model.set_rows(unmet_rows(detail))
        self.exclusion_model.set_rows(exclusion_rows(detail))
        excluded = sum(exclusion.excluded for exclusion in detail.exclusions)
        accepted = detail.summary.candidates
        self.exclusion_hint.setText(
            f"Candidatos aceptados: {format_int(accepted)} de {format_int(accepted + excluded)} evaluados. "
            "Cada descarte se cuenta en la primera regla que no cumple, en el orden de la cadena."
        )
        bars = unmet_by_block(detail.unmet)
        if not bars:
            self.chart.show_message("Toda la demanda de la semana quedó cubierta.")
            return
        names = {service.code: service.name for service in detail.instance.service_types}
        self.chart.render(partial(draw_unmet, bars=bars, days=detail.instance.days, names=names))

    def self_check(self) -> list[str]:
        problems: list[str] = []
        if self.model.rowCount():
            problems += check_charts([("turnos sin cubrir", self.chart)])
        elif not self.chart.has_message:
            problems.append("el gráfico de turnos sin cubrir no muestra datos ni aviso")
        if self.exclusion_model.rowCount() == 0:
            problems.append("la tabla de exclusiones por regla está vacía")
        return problems
