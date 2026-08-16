"""parse_events projects the agent-written ledger shape onto the schema shape.

The loop agents hand-write `{"at": …, "producer": {"role": …, "actor": …}}`
lines; the keyed readers (integrity liveness, janitor retention aging,
pr_reconcile ownership) read only top-level `ts`/`role`/`actor`. Measured
2026-08-10: 30 of the last 199 live events were invisible to all of them.
These tests pin the projection and its non-authority: existing top-level keys
always win and nothing is invented.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge.ledger_read import OK, normalize_event, parse_events  # noqa: E402


def _roundtrip(obj: dict) -> dict:
    events, status = parse_events(json.dumps(obj))
    assert status == OK
    assert len(events) == 1
    return events[0]


def test_agent_shape_is_projected_to_schema_shape():
    ev = _roundtrip({
        "contract": "v1.1", "at": "2026-08-10T11:04:00Z", "event": "observed",
        "id": "sha256:" + "a" * 64,
        "producer": {"role": "planner", "actor": "planner-loop@claude-planner"},
    })
    assert ev["ts"] == "2026-08-10T11:04:00Z"
    assert ev["role"] == "planner"
    assert ev["actor"] == "planner-loop@claude-planner"
    assert ev["producer"]["actor"] == "planner-loop@claude-planner"  # untouched


def test_existing_top_level_keys_always_win():
    ev = _roundtrip({
        "ts": "2026-08-10T00:00:00Z", "at": "2026-08-09T00:00:00Z",
        "role": "implementer", "actor": "gate-loop-daemon", "event": "merged",
        "producer": {"role": "planner", "actor": "someone-else"},
    })
    assert ev["ts"] == "2026-08-10T00:00:00Z"
    assert ev["role"] == "implementer"
    assert ev["actor"] == "gate-loop-daemon"


def test_nothing_is_invented():
    ev = _roundtrip({"ts": "2026-08-10T00:00:00Z", "role": "planner",
                     "event": "observed"})
    assert "actor" not in ev
    ev2 = _roundtrip({"event": "observed", "producer": "not-a-dict",
                      "at": 12345})
    assert "ts" not in ev2 and "role" not in ev2 and "actor" not in ev2


def test_schema_shape_passes_through_byte_identical():
    src = {"ts": "2026-08-10T00:00:00Z", "role": "external", "event": "found",
           "id": "sha256:" + "b" * 64, "actor": "integrity-check",
           "detail": {"x": 1}}
    assert _roundtrip(dict(src)) == src


def test_normalize_event_is_idempotent():
    ev = {"at": "2026-08-10T11:04:00Z", "event": "observed",
          "producer": {"role": "planner", "actor": "p"}}
    once = normalize_event(dict(ev))
    twice = normalize_event(dict(once))
    assert once == twice
