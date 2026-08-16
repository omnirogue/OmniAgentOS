# schema.py (seed version)
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool


VERSION = 1

FIELDS: tuple[Field, ...] = (
    Field(name="id", kind="int", required=True),
    Field(name="name", kind="str", required=True),
)


def field_names() -> tuple[str, ...]:
    return tuple(f.name for f in FIELDS)


def field_by_name(name: str) -> Field:
    for f in FIELDS:
        if f.name == name:
            return f
    raise KeyError(name)
