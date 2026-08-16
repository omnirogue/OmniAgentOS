"""Small helpers shared by the H2 lab note generators (contracts/lab-interfaces.md
§L08-labvault). A row handed to a `render_*_note` function may be either a raw
LabStore row (contracts/schema.sql `*_json` columns are JSON-encoded strings)
or an already-decoded pydantic `model_dump()` (plain nested dict/list) — every
helper here tolerates both shapes, mirroring the never-crash-on-partial-data
philosophy of `omniagentos.vault.util` (H1).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

_EXPECTED_MARKER = "expected"


def maybe_json(value: Any) -> Any:
    """Decode `value` if it is a JSON-encoded string (a raw Store `*_json`
    column); pass dicts/lists/None/other types through unchanged so callers
    may hand either a raw Store row or a pre-decoded dict/list."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def scrub_held_out(value: Any) -> Any:
    """Recursively drop any mapping key that IS or CONTAINS "expected"
    (case-insensitive), at any nesting depth.

    Structurally, none of L08's inputs ever carry a held-out `expected` value
    — contracts/lab-interfaces.md L02 is explicit that it lives ONLY in the
    protected store (`var/eval_protected.db`), never in the shared DB's
    `eval_results`/`Scorecard` shapes this package renders. This scrub is
    defense-in-depth so a lab vault note can NEVER become the leak path
    (Section 11.7 reward-hacking invariant) even if a caller's dict is
    malformed or a future field is added carelessly.
    """
    if isinstance(value, dict):
        return {
            key: scrub_held_out(val)
            for key, val in value.items()
            if _EXPECTED_MARKER not in str(key).lower()
        }
    if isinstance(value, list):
        return [scrub_held_out(item) for item in value]
    return value


def fmt_metrics(metrics: Any) -> str:
    """Render a metrics mapping (raw JSON string or dict) as a compact, stable
    `key=value, ...` list, scrubbed of any held-out `expected` key."""
    decoded = scrub_held_out(maybe_json(metrics))
    if not isinstance(decoded, dict) or not decoded:
        return "_no metrics recorded_"
    return ", ".join(f"{key}={_fmt_number(val)}" for key, val in sorted(decoded.items()))


def _fmt_number(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def fmt_list(values: Any) -> list[str]:
    """Render a list field (raw JSON string or list) as a list of strings."""
    decoded = maybe_json(values)
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


def common_value(values: Iterable[str | None]) -> str | None:
    """Return the single shared non-empty value across `values`, or None if
    they are empty, absent, or mixed."""
    present = {value for value in values if value}
    if len(present) == 1:
        return next(iter(present))
    return None


def latest_timestamp(values: Iterable[str | None]) -> str | None:
    """Return the lexicographically latest (== chronologically latest; every
    timestamp is canonical UTC ISO-8601 — `contracts.utc_now_iso`) non-empty
    value in `values`, or None if there are none."""
    candidates = sorted(value for value in values if value)
    return candidates[-1] if candidates else None
