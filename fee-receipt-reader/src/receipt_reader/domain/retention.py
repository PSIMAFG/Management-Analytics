"""Retención de honorarios y coherencia de la tripleta bruto, retención y líquido.

La tasa legal depende del año de la fecha de emisión y se guarda como tabla
parametrizable en puntos básicos (1450 = 14,50 %). Para un año posterior al
último de la tabla rige la última tasa conocida ("desde 2028, 17 %").

Identidades que debe cumplir una boleta:

    retención = redondeo(bruto × tasa del año de emisión), con tolerancia de 1 peso
    líquido = bruto - retención
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from receipt_reader.domain.dates import Period
from receipt_reader.domain.money import round_half_up

# Tabla pública de retención de boletas de honorarios (Ley 21.133 y su modificación).
LEGAL_RATES_BP: dict[int, int] = {
    2020: 1075,
    2021: 1150,
    2022: 1225,
    2023: 1300,
    2024: 1375,
    2025: 1450,
    2026: 1525,
    2027: 1600,
    2028: 1700,
}


class RetentionTable:
    """Tasa de retención por año."""

    def __init__(self, rates_bp: Mapping[int, int]) -> None:
        self._rates = dict(sorted(rates_bp.items()))

    def rate_for(self, year: int) -> int | None:
        """Tasa del año o, si no está, la del último año anterior; None antes del primer año."""
        if year in self._rates:
            return self._rates[year]
        earlier = [y for y in self._rates if y < year]
        return self._rates[earlier[-1]] if earlier else None

    def years(self) -> list[int]:
        return list(self._rates)

    def as_dict(self) -> dict[int, int]:
        return dict(self._rates)


def expected_retention(gross: int, rate_bp: int) -> int:
    """Retención que corresponde a un bruto con la tasa indicada, redondeada al peso."""
    return round_half_up(Decimal(gross) * Decimal(rate_bp) / Decimal(10_000))


@dataclass(frozen=True)
class CompletedTriple:
    """Tripleta de montos con los valores deducidos marcados."""

    gross: int | None
    retention: int | None
    net: int | None
    derived: tuple[str, ...] = ()


def complete_triple(gross: int | None, retention: int | None, net: int | None) -> CompletedTriple:
    """Con el bruto y uno de los otros dos montos, deduce el tercero por la identidad.

    El bruto nunca se deduce: una boleta sin bruto leído queda pendiente.
    """
    if gross is None:
        return CompletedTriple(gross, retention, net)
    if retention is None and net is not None:
        return CompletedTriple(gross, gross - net, net, ("retention",))
    if net is None and retention is not None:
        return CompletedTriple(gross, retention, gross - retention, ("net",))
    return CompletedTriple(gross, retention, net)


def implied_hourly_rate(gross: int | None, hours: Decimal | None, weeks_factor: int | None) -> int | None:
    """Valor hora implícito: bruto / (horas × semanas por mes). None si falta algún dato."""
    if gross is None or hours is None or weeks_factor is None or hours <= 0:
        return None
    return round_half_up(Decimal(gross) / (hours * weeks_factor))


def reference_year(service_period: Period | None, issue_date: date | None) -> int | None:
    """Año cuyo valor hora de referencia se aplica a una boleta.

    Es el año del servicio prestado; si la glosa no lo indica, el de la emisión.
    Un servicio de diciembre facturado en enero se compara con el año del servicio.
    """
    if service_period is not None:
        return service_period.year
    return issue_date.year if issue_date is not None else None
