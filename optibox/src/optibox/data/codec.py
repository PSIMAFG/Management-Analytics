"""Serialización JSON de objetos de dominio (dataclasses inmutables) guiada por sus anotaciones.

Se usa para guardar con cada corrida una copia exacta de la instancia
planificada, de modo que una corrida antigua se pueda volver a mostrar con
sus métricas originales aunque los datos maestros cambien después.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from collections.abc import Mapping
from datetime import date
from enum import Enum
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

from optibox.errors import DataError

T = TypeVar("T")


def to_jsonable(value: Any) -> Any:
    """Convierte dataclasses, fechas, enumeraciones y colecciones en tipos JSON."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return [[to_jsonable(k), to_jsonable(v)] for k, v in value.items()]
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(to_jsonable(item) for item in value)
    return value


def _decode(hint: Any, data: Any) -> Any:
    origin = get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        options = [arg for arg in get_args(hint) if arg is not type(None)]
        if data is None:
            return None
        return _decode(options[0], data)
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], item) for item in data)
        return tuple(_decode(arg, item) for arg, item in zip(args, data, strict=True))
    if origin is frozenset:
        return frozenset(_decode(get_args(hint)[0], item) for item in data)
    if origin is list:
        return [_decode(get_args(hint)[0], item) for item in data]
    if origin in (dict, Mapping):
        key_type, value_type = get_args(hint)
        return {_decode(key_type, k): _decode(value_type, v) for k, v in data}
    if isinstance(hint, type):
        if dataclasses.is_dataclass(hint):
            return from_jsonable(hint, data)
        if issubclass(hint, Enum):
            return hint(data)
        if hint is date:
            return date.fromisoformat(data)
        if hint is float:
            return float(data)
        return data
    return data


def from_jsonable(cls: type[T], data: dict[str, Any]) -> T:
    """Reconstruye una dataclass a partir del resultado de `to_jsonable`."""
    hints = get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in data:
            kwargs[f.name] = _decode(hints[f.name], data[f.name])
    return cls(**kwargs)


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def loads(cls: type[T], text: str) -> T:
    try:
        return from_jsonable(cls, json.loads(text))
    except (ValueError, TypeError, KeyError) as error:
        raise DataError("No se pudo leer la copia de la instancia guardada con la corrida.") from error
