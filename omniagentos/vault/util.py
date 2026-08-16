"""Small internal helpers shared by the note generators.

`run: dict` and `steps: list[dict]` (contracts/interfaces.md §p05) are
deliberately untyped: they are not one of the frozen pydantic contracts, so
callers may pass either a raw `Store` row (contracts/schema.sql column names)
or a lightly enriched version of one. The pick* helpers below read a value by
trying several plausible key names in order and are lenient about missing
data — a note generator must never crash on a partial dict, it should render
what it has and say "not reported" for the rest.
"""

from __future__ import annotations

from typing import Any


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present (and not None) value among `keys` in `data`."""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    if value is None:
        return default
    return bool(value)


def yyyy_mm(timestamp: str) -> tuple[str, str]:
    """Split a canonical `YYYY-MM-DDTHH:MM:SSZ` timestamp (contracts.utc_now_iso)
    into (yyyy, mm) for the runs/<yyyy>/<mm>/ ledger-style vault layout."""
    year = timestamp[0:4]
    month = timestamp[5:7]
    if len(year) == 4 and year.isdigit() and len(month) == 2 and month.isdigit():
        return year, month
    raise ValueError(f"timestamp {timestamp!r} is not YYYY-MM-DD...")
