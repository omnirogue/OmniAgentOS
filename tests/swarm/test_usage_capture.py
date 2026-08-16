"""Provider-reported usage capture, and the estimate/measurement boundary.

The single most important property here is a negative one: when a CLI reports
nothing, we must record nothing. `adapters.common.estimated_usage` exists and
would gladly supply a plausible token count from a character heuristic — correct
for budget guards, poison for the effort dataset, where an estimate sitting
beside a measurement with no way to tell them apart would answer "is xhigh worth
it" with fabricated numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.collab.store import CollabStore
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.swarm.usage_capture import (
    SOURCE_CLI_REPORT,
    SOURCE_NONE,
    SOURCE_TOKENS_ONLY,
    extract,
)
from tests.support.db_template import migrated_db


@dataclass
class _Parsed:
    """Stand-in for an adapter's ParsedResponse (only `payload` is read)."""

    payload: Any


def test_claude_shape_is_captured_as_a_measurement() -> None:
    """Mirrors adapters/claude.py:_usage — the documented real payload."""
    parsed = _Parsed(
        {"usage": {"input_tokens": 1200, "output_tokens": 340}, "total_cost_usd": 0.42}
    )

    reported = extract(parsed, wall_ms=9_500)

    assert reported.input_tokens == 1200
    assert reported.output_tokens == 340
    assert reported.cost_usd == 0.42
    assert reported.wall_ms == 9_500
    assert reported.source == SOURCE_CLI_REPORT
    assert reported.has_tokens is True


def test_silence_is_recorded_as_silence_never_estimated() -> None:
    reported = extract(_Parsed({"result": "done"}), wall_ms=1_000)

    assert reported.input_tokens is None
    assert reported.output_tokens is None
    assert reported.cost_usd is None
    assert reported.source == SOURCE_NONE
    assert reported.has_tokens is False
    # Wall-clock IS ours to measure, so it survives even when the CLI says nothing.
    assert reported.wall_ms == 1_000


def test_cost_only_report_still_counts_as_a_report() -> None:
    reported = extract(_Parsed({"total_cost_usd": 0.05}))

    assert reported.source == SOURCE_CLI_REPORT
    assert reported.has_tokens is False
    assert reported.cost_usd == 0.05


def test_booleans_are_not_mistaken_for_counts() -> None:
    """`True` is an int in Python; a flag must not become 1 token."""
    reported = extract(_Parsed({"usage": {"input_tokens": True, "output_tokens": False}}))

    assert reported.input_tokens is None
    assert reported.output_tokens is None
    assert reported.source == SOURCE_NONE


def test_nonfinite_and_negative_usage_are_not_measurements() -> None:
    """±Inf / NaN / negative cost or tokens must not become reported usage.

    Upstream of costgreen: provider payloads are not a trusted validation
    boundary. A negative or infinite cost accepted here would later certify as
    measured cost_per_green of -1.0 or Infinity.
    """
    for bad_cost in (float("inf"), float("-inf"), float("nan"), -1.0):
        reported = extract(
            _Parsed(
                {
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "total_cost_usd": bad_cost,
                }
            )
        )
        assert reported.cost_usd is None, f"cost={bad_cost!r} must be unknown"
        # Tokens still valid when only cost is malformed.
        assert reported.input_tokens == 10
        assert reported.output_tokens == 5
        assert reported.source == SOURCE_TOKENS_ONLY

    neg_tokens = extract(
        _Parsed(
            {
                "usage": {"input_tokens": -10, "output_tokens": -5},
                "total_cost_usd": 0.5,
            }
        )
    )
    assert neg_tokens.input_tokens is None
    assert neg_tokens.output_tokens is None
    assert neg_tokens.cost_usd == 0.5
    assert neg_tokens.source == SOURCE_CLI_REPORT


def test_malformed_payloads_never_raise() -> None:
    for payload in (None, "not a dict", [], {"usage": "nope"}, {"usage": {"input_tokens": "12"}}):
        reported = extract(_Parsed(payload))
        assert reported.source == SOURCE_NONE
        assert reported.input_tokens is None


def test_object_without_a_payload_attribute_is_tolerated() -> None:
    assert extract(object()).source == SOURCE_NONE


# ------------------------------------------------------------- persistence


def _session_row(session_id: str = "ses_x") -> dict[str, Any]:
    return {
        "id": session_id,
        "source": "bridge",
        "project_dir": "/tmp/x",
        "provider": "claude",
        "state": "starting",
        "model": "opus",
        "created_at": "2026-07-23T10:00:00Z",
        "updated_at": "2026-07-23T10:00:00Z",
        "cost_usd": 0.0,
    }


def _dal(tmp_path: Path) -> Any:
    from omniagentos.sessions.dal import SessionsDal

    db = str(tmp_path / "t.db")
    db = migrated_db(CollabStore, db)
    return SessionsDal(db)


def test_record_session_usage_writes_only_what_was_reported(tmp_path: Path) -> None:
    dal = _dal(tmp_path)
    dal.create_session(_session_row())
    dal.record_session_usage("ses_x", cost_usd=1.5, effort="xhigh")

    # A later tokens-only report must not blank the cost recorded above.
    dal.record_session_usage("ses_x", input_tokens=10, usage_source=SOURCE_CLI_REPORT)

    row = dal.get_session("ses_x")
    assert row["cost_usd"] == 1.5
    assert row["effort"] == "xhigh"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] is None  # never reported, never invented


def test_record_session_usage_with_nothing_to_say_is_a_noop(tmp_path: Path) -> None:
    dal = _dal(tmp_path)
    dal.create_session(_session_row())

    assert dal.record_session_usage("ses_x") is False


def test_record_session_usage_on_unknown_session_returns_false(tmp_path: Path) -> None:
    dal = _dal(tmp_path)

    assert dal.record_session_usage("ses_missing", wall_ms=5) is False


def test_manifest_emits_the_telemetry_fields(tmp_path: Path) -> None:
    import json

    manifest = SessionManifest(tmp_path)
    session = _session_row()
    session.update(
        {
            "effort": "medium",
            "input_tokens": 900,
            "output_tokens": 120,
            "wall_ms": 45_000,
            "usage_source": SOURCE_CLI_REPORT,
        }
    )

    path = manifest.write(session, [])
    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["effort"] == "medium"
    assert record["input_tokens"] == 900
    assert record["output_tokens"] == 120
    assert record["wall_ms"] == 45_000
    assert record["usage_source"] == SOURCE_CLI_REPORT


def test_manifest_keeps_unreported_usage_null_rather_than_zero(tmp_path: Path) -> None:
    """A 0 would read as 'spent nothing'; the truth is 'did not say'."""
    import json

    manifest = SessionManifest(tmp_path)

    path = manifest.write(_session_row("ses_quiet"), [])
    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["effort"] is None
    assert record["usage_source"] is None
