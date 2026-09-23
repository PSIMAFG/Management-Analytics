"""Grilla de tiempo: minutos desde medianoche, slots de 15 minutos y bloques de demanda.

Todo el tiempo del dominio se expresa en minutos enteros desde la medianoche.
La hora que se muestra se deriva siempre del minuto (`format_minute`), nunca
de un índice de slot: así el almuerzo es un hueco real y las horas de la tarde
quedan correctas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from optibox.errors import ValidationError

SLOT_MINUTES = 15
BLOCK_MINUTES = 60
MINUTES_PER_DAY = 24 * 60
WEEK_DAYS = 5
WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes")
WEEKDAY_SHORT = ("Lun", "Mar", "Mié", "Jue", "Vie")

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def format_minute(minute: int) -> str:
    """540 -> '09:00'; 840 -> '14:00'."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def format_range(start: int, end: int) -> str:
    """(540, 585) -> '09:00-09:45'."""
    return f"{format_minute(start)}-{format_minute(end)}"


def parse_hhmm(text: str, field: str = "hora") -> int:
    """Convierte 'HH:MM' en minutos desde medianoche validando el formato.

    Acepta horas con o sin cero inicial ('8:00' o '08:00'). Rechaza valores
    fuera de rango en lugar de interpretarlos de otra forma.
    """
    match = _HHMM.match(str(text))
    if match is None:
        raise ValidationError(f"El valor '{text}' del campo {field} no tiene el formato HH:MM.")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if minutes >= 60 or hours > 24 or (hours == 24 and minutes > 0):
        raise ValidationError(f"El valor '{text}' del campo {field} no es una hora válida.")
    return hours * 60 + minutes


def require_aligned(minute: int, field: str) -> int:
    """Exige que el minuto esté alineado a la grilla de 15 minutos."""
    if minute % SLOT_MINUTES != 0:
        raise ValidationError(
            f"El campo {field} ({format_minute(minute)}) debe estar alineado a {SLOT_MINUTES} minutos."
        )
    return minute


def require_range(start: int, end: int, field: str) -> None:
    """Exige un intervalo no vacío dentro del día y alineado a la grilla."""
    if not 0 <= start < end <= MINUTES_PER_DAY:
        raise ValidationError(
            f"El intervalo de {field} ({format_range(start, end)}) no es válido: el inicio debe ser anterior al fin."
        )
    require_aligned(start, f"inicio de {field}")
    require_aligned(end, f"fin de {field}")


def week_monday(day: date) -> date:
    """Lunes de la semana que contiene la fecha."""
    return day - timedelta(days=day.weekday())


def week_dates(monday: date) -> tuple[date, ...]:
    """Fechas de lunes a viernes de la semana que empieza en `monday`."""
    return tuple(monday + timedelta(days=offset) for offset in range(WEEK_DAYS))


def block_of(minute: int) -> int:
    """Bloque de demanda (hora alineada) al que pertenece un minuto de inicio."""
    return minute // BLOCK_MINUTES * BLOCK_MINUTES


def slot_mask(start: int, end: int) -> int:
    """Máscara de bits de los slots completos entre `start` y `end` (redondeo hacia adentro)."""
    first = -(-start // SLOT_MINUTES)
    last = end // SLOT_MINUTES
    if last <= first:
        return 0
    return ((1 << (last - first)) - 1) << first


def slot_mask_outward(start: int, end: int) -> int:
    """Máscara de bits de los slots que tocan el intervalo (redondeo hacia afuera).

    Se usa para bloqueos y ausencias: un bloqueo de 09:10 a 09:20 ocupa el
    slot de 09:00 a 09:15 y el de 09:15 a 09:30, en vez de ignorarse.
    """
    first = start // SLOT_MINUTES
    last = -(-end // SLOT_MINUTES)
    if last <= first:
        return 0
    return ((1 << (last - first)) - 1) << first


def mask_minutes(mask: int) -> int:
    """Minutos cubiertos por una máscara de slots."""
    return mask.bit_count() * SLOT_MINUTES


def mask_slots(mask: int) -> tuple[int, ...]:
    """Minutos de inicio de cada slot marcado en la máscara, en orden."""
    starts: list[int] = []
    index = 0
    while mask:
        if mask & 1:
            starts.append(index * SLOT_MINUTES)
        mask >>= 1
        index += 1
    return tuple(starts)


def mask_intervals(mask: int) -> tuple[tuple[int, int], ...]:
    """Agrupa los slots contiguos de una máscara en intervalos (inicio, fin)."""
    intervals: list[tuple[int, int]] = []
    for start in mask_slots(mask):
        if intervals and intervals[-1][1] == start:
            intervals[-1] = (intervals[-1][0], start + SLOT_MINUTES)
        else:
            intervals.append((start, start + SLOT_MINUTES))
    return tuple(intervals)


@dataclass(frozen=True)
class DayHours:
    """Horario del centro para un día de la semana (0 = lunes).

    El almuerzo es un hueco: ninguna sesión puede cruzarlo. Si el inicio y el
    fin del almuerzo coinciden, el día no tiene pausa.
    """

    weekday: int
    open_min: int
    close_min: int
    lunch_start: int
    lunch_end: int

    def __post_init__(self) -> None:
        name = WEEKDAY_NAMES[self.weekday] if 0 <= self.weekday < WEEK_DAYS else str(self.weekday)
        if not 0 <= self.weekday < WEEK_DAYS:
            raise ValidationError(f"El día {name} no es un día hábil (lunes a viernes).")
        require_range(self.open_min, self.close_min, f"horario del {name}")
        require_aligned(self.lunch_start, f"inicio del almuerzo del {name}")
        require_aligned(self.lunch_end, f"fin del almuerzo del {name}")
        if not self.open_min <= self.lunch_start <= self.lunch_end <= self.close_min:
            raise ValidationError(f"El almuerzo del {name} debe quedar dentro del horario de atención.")

    @property
    def has_lunch(self) -> bool:
        return self.lunch_end > self.lunch_start

    @property
    def intervals(self) -> tuple[tuple[int, int], ...]:
        """Tramos abiertos del día: mañana y tarde, o un solo tramo si el día no tiene almuerzo."""
        if not self.has_lunch:
            return ((self.open_min, self.close_min),)
        parts = ((self.open_min, self.lunch_start), (self.lunch_end, self.close_min))
        return tuple((start, end) for start, end in parts if end > start)

    @property
    def open_minutes(self) -> int:
        return sum(end - start for start, end in self.intervals)

    @property
    def open_mask(self) -> int:
        mask = 0
        for start, end in self.intervals:
            mask |= slot_mask(start, end)
        return mask

    def contains(self, start: int, end: int) -> bool:
        """Indica si el intervalo cabe completo en un tramo abierto (sin cruzar el almuerzo)."""
        return any(open_start <= start and end <= open_end for open_start, open_end in self.intervals)

    def slot_starts(self) -> tuple[int, ...]:
        """Inicios de todos los slots abiertos del día."""
        return mask_slots(self.open_mask)

    def blocks(self) -> tuple[int, ...]:
        """Bloques de demanda del día: horas alineadas con al menos un slot abierto."""
        return tuple(sorted({block_of(start) for start in self.slot_starts()}))


DEFAULT_HOURS = (
    DayHours(0, 480, 1020, 780, 840),
    DayHours(1, 480, 1020, 780, 840),
    DayHours(2, 480, 1020, 780, 840),
    DayHours(3, 480, 1020, 780, 840),
    DayHours(4, 480, 960, 780, 840),
)
