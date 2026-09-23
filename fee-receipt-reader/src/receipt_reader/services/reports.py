"""Consultas de totales, datos para gráficos y exportación de informes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from receipt_reader.data import catalog_repo, db, receipt_repo
from receipt_reader.data.excel_export import WorkbookData, write_individual_reports, write_workbook
from receipt_reader.domain.dates import Period
from receipt_reader.domain.models import REVIEW_STATUSES, VALID_STATUSES, PeriodAxis, ReceiptStatus
from receipt_reader.domain.records import ReceiptRow
from receipt_reader.domain.reporting import ReceiptFilter, SummaryTable, Totals
from receipt_reader.services.context import Clock, system_clock


@dataclass(frozen=True)
class Overview:
    """Totales para la franja superior de la ventana."""

    total_receipts: int
    counts: dict[ReceiptStatus, int]
    valid: Totals

    @property
    def approved(self) -> int:
        return sum(self.counts.get(status, 0) for status in VALID_STATUSES)

    @property
    def pending(self) -> int:
        return sum(self.counts.get(status, 0) for status in REVIEW_STATUSES)

    @property
    def discarded(self) -> int:
        return self.counts.get(ReceiptStatus.DISCARDED, 0)


@dataclass(frozen=True)
class MonthlySeries:
    """Bruto mensual por programa, listo para barras apiladas."""

    periods: tuple[Period, ...]
    series: dict[str, tuple[int, ...]]
    without_period: int


@dataclass(frozen=True)
class ExportResult:
    """Rutas efectivas de la exportación (pueden diferir de la pedida si el archivo estaba en uso)."""

    requested_path: Path
    workbook_path: Path
    individual_paths: tuple[Path, ...]
    sheet_count: int
    receipt_count: int

    @property
    def saved_with_other_name(self) -> bool:
        return self.workbook_path != self.requested_path


class ReportService:
    """Casos de uso de consulta e informes."""

    def __init__(self, db_path: Path, *, clock: Clock = system_clock) -> None:
        self.db_path = db_path
        self._clock = clock

    def overview(self, filt: ReceiptFilter | None = None) -> Overview:
        """Conteos por estado y totales de las boletas válidas.

        Usa el programa, el período y el eje del filtro; el filtro de estado no cambia los
        conteos (la franja muestra siempre todos los estados).
        """
        query = filt or ReceiptFilter()
        with db.session(self.db_path) as conn:
            counts = {item.status: item.count for item in receipt_repo.status_counts(conn, query)}
            valid = receipt_repo.grand_totals(conn, query)
        return Overview(sum(counts.values()), counts, valid)

    def rows(self, filt: ReceiptFilter | None = None) -> list[ReceiptRow]:
        with db.session(self.db_path) as conn:
            return receipt_repo.list_rows(conn, filt or ReceiptFilter())

    def valid_rows(self, filt: ReceiptFilter | None = None) -> list[ReceiptRow]:
        """Boletas aprobadas o corregidas con el programa, el período y el eje del filtro (sin su estado)."""
        with db.session(self.db_path) as conn:
            return receipt_repo.list_rows(conn, filt or ReceiptFilter(), valid_only=True)

    def summary(self, filt: ReceiptFilter | None = None) -> SummaryTable:
        """Tabla programa por período (solo boletas válidas), con subtotales y total general en SQL."""
        query = filt or ReceiptFilter()
        with db.session(self.db_path) as conn:
            return SummaryTable(
                axis=query.axis,
                rows=tuple(receipt_repo.summary_rows(conn, query)),
                program_totals=tuple(receipt_repo.program_totals(conn, query)),
                grand_total=receipt_repo.grand_totals(conn, query),
            )

    def monthly_gross_by_program(self, filt: ReceiptFilter | None = None) -> MonthlySeries:
        summary = self.summary(filt)
        periods = tuple(sorted({row.period for row in summary.rows if row.period is not None}))
        values: dict[str, dict[Period, int]] = defaultdict(dict)
        without = 0
        for row in summary.rows:
            if row.period is None:
                without += row.gross
            else:
                values[row.program_name][row.period] = row.gross
        series = {name: tuple(by_period.get(p, 0) for p in periods) for name, by_period in values.items()}
        return MonthlySeries(periods, series, without)

    def periods(self, axis: PeriodAxis = PeriodAxis.SERVICE) -> list[Period]:
        with db.session(self.db_path) as conn:
            return receipt_repo.distinct_periods(conn, axis)

    def _workbook_data(self, filt: ReceiptFilter) -> WorkbookData:
        with db.session(self.db_path) as conn:
            settings = catalog_repo.load_settings(conn)
            everything = ReceiptFilter(program_id=filt.program_id, period=filt.period, axis=filt.axis)
            rows = receipt_repo.list_rows(conn, everything)
            summary = SummaryTable(
                axis=filt.axis,
                rows=tuple(receipt_repo.summary_rows(conn, everything)),
                program_totals=tuple(receipt_repo.program_totals(conn, everything)),
                grand_total=receipt_repo.grand_totals(conn, everything),
            )
            return WorkbookData(
                generated_at=self._clock(),
                axis=filt.axis,
                organization_name=settings.organization_name,
                rows=rows,
                valid_rows=[row for row in rows if row.status in VALID_STATUSES],
                pending_rows=[row for row in rows if row.status in REVIEW_STATUSES],
                summary=summary,
                programs=catalog_repo.list_programs(conn),
                legal_rates=catalog_repo.load_retention_table(conn),
                traces=receipt_repo.all_traces(conn),
                issue_messages=receipt_repo.all_issue_messages(conn),
                provider_names={p.rut: p.canonical_name for p in catalog_repo.list_providers(conn)},
            )

    def export_workbook(
        self, target: Path, filt: ReceiptFilter | None = None, *, individual_folder: Path | None = None
    ) -> ExportResult:
        """Escribe el Excel principal (y opcionalmente los informes por prestador) desde la base.

        Los informes individuales van a una subcarpeta nueva por exportación, para no mezclar
        archivos de corridas anteriores.
        """
        data = self._workbook_data(filt or ReceiptFilter())
        path = write_workbook(target, data)
        individual: list[Path] = []
        if individual_folder is not None:
            run_folder = individual_folder / f"informes_prestadores_{data.generated_at:%Y%m%d_%H%M%S}"
            individual = write_individual_reports(run_folder, data)
        sheets = 3 + len({row.program_id for row in data.valid_rows if row.program_id is not None})
        return ExportResult(target, path, tuple(individual), sheets, len(data.rows))
