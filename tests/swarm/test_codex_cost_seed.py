"""Codex unpriced spend must never look like a measured zero.

Live codex exec --json reports exact token counts and no cost field. Creation
paths still often seed cost_usd=0.0, but a tokens-only extract + record must
persist SQL NULL (not 0.0) and usage_source=SOURCE_TOKENS_ONLY so cost-to-green
cannot treat sol as free.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.adapters.codex import CodexAdapter
from omniagentos.collab.store import CollabStore
from omniagentos.swarm.costgreen import task_cost_to_green
from omniagentos.swarm.usage_capture import (
    SOURCE_CLI_REPORT,
    SOURCE_NONE,
    SOURCE_TOKENS_ONLY,
    extract,
)
from tests.support.db_template import migrated_db

# Verbatim final event from a live probe of `codex exec --json -m gpt-5.6-sol`.
# No cost field anywhere; no model name in any event.
LIVE_CODEX_TURN_COMPLETED = (
    '{"type":"turn.completed","usage":{"input_tokens":17464,'
    '"cached_input_tokens":0,"cache_write_input_tokens":0,'
    '"output_tokens":5,"reasoning_output_tokens":0}}'
)


@dataclass
class _Parsed:
    payload: Any


def _live_codex_stdout() -> str:
    """Minimal JSONL stream that ends with the live turn.completed payload."""
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_live"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "ok"},
                }
            ),
            LIVE_CODEX_TURN_COMPLETED,
        ]
    )


def _explicit_zero_payload() -> _Parsed:
    return _Parsed(
        {
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "total_cost_usd": 0.0,
        }
    )


def test_live_codex_payload_is_tokens_only_not_cli_report() -> None:
    parsed = CodexAdapter()._parse(_live_codex_stdout())
    reported = extract(parsed, wall_ms=1_200)

    assert reported.input_tokens == 17_464
    assert reported.output_tokens == 5
    assert reported.cost_usd is None
    assert reported.source == SOURCE_TOKENS_ONLY
    assert reported.source != SOURCE_CLI_REPORT
    assert reported.has_tokens is True


def test_explicit_zero_cost_stays_cli_report_and_differs_from_tokens_only() -> None:
    zero = extract(_explicit_zero_payload())
    tokens_only = extract(_Parsed({"usage": {"input_tokens": 17_464, "output_tokens": 5}}))

    assert zero.cost_usd == 0.0
    assert zero.source == SOURCE_CLI_REPORT
    assert tokens_only.cost_usd is None
    assert tokens_only.source == SOURCE_TOKENS_ONLY
    assert zero.source != tokens_only.source


def test_costgreen_marks_tokens_only_unmeasured_and_explicit_zero_measured() -> None:
    codex_row = {
        "seq": 0,
        "board_task_id": "task-codex",
        "effort": "medium",
        "model": "gpt-5.6-sol",
        "tier": None,
        "end_reason": "completed",
        "cost_usd": None,  # unpriced — not a measured free run
        "input_tokens": 17_464,
        "output_tokens": 5,
        "wall_ms": 1_200,
        "usage_source": SOURCE_TOKENS_ONLY,
    }
    zero_row = {
        "seq": 0,
        "board_task_id": "task-free",
        "effort": "medium",
        "model": "sonnet",
        "tier": None,
        "end_reason": "completed",
        "cost_usd": 0.0,
        "input_tokens": 100,
        "output_tokens": 10,
        "wall_ms": 500,
        "usage_source": SOURCE_CLI_REPORT,
    }

    codex_green = task_cost_to_green([codex_row])
    zero_green = task_cost_to_green([zero_row])

    assert codex_green.measured is False
    assert zero_green.measured is True
    # _num treats None as 0.0 for chain totals (floor), but measured=False
    # prevents presenting that floor as a complete figure.
    assert codex_green.cost_usd == 0.0
    assert zero_green.cost_usd == 0.0
    assert codex_row["cost_usd"] is None
    assert zero_row["cost_usd"] == 0.0
    assert codex_row["usage_source"] != zero_row["usage_source"]


def test_warning_fires_only_for_tokens_without_cost(caplog: Any) -> None:
    tokens_payload = _Parsed(
        {
            "usage": {
                "input_tokens": 17_464,
                "output_tokens": 5,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
            }
        }
    )
    cost_payload = _Parsed(
        {"usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": 0.01}
    )
    silent_payload = _Parsed({"result": "done"})

    with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.usage_capture"):
        extract(tokens_payload, model="gpt-5.6-sol")
        extract(cost_payload)
        extract(silent_payload)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "17464" in message
    assert "no cost" in message.lower() or "no provider price" in message.lower()
    assert "NULL" in message or "unpriced" in message.lower()


def test_cache_fields_captured_and_bools_rejected() -> None:
    live = extract(CodexAdapter()._parse(_live_codex_stdout()))
    assert live.cached_input_tokens == 0
    assert live.cache_write_input_tokens == 0

    bools = extract(
        _Parsed(
            {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cached_input_tokens": True,
                    "cache_write_input_tokens": True,
                }
            }
        )
    )
    assert bools.cached_input_tokens is None
    assert bools.cache_write_input_tokens is None
    # Bools must not become 1 under the strict int rule.
    assert bools.cached_input_tokens != 1
    assert bools.cache_write_input_tokens != 1


def _session_row(session_id: str = "ses_codex", *, cost_usd: float | None = 0.0) -> dict[str, Any]:
    """Mirrors provider_exec's creation seed (cost_usd: 0.0) unless overridden."""
    return {
        "id": session_id,
        "source": "bridge",
        "project_dir": "/tmp/x",
        "provider": "codex",
        "state": "starting",
        "model": "gpt-5.6-sol",
        "created_at": "2026-07-23T10:00:00Z",
        "updated_at": "2026-07-23T10:00:00Z",
        "cost_usd": cost_usd,
    }


def _dal(tmp_path: Path) -> Any:
    from omniagentos.sessions.dal import SessionsDal

    db = str(tmp_path / "t.db")
    db = migrated_db(CollabStore, db)
    return SessionsDal(db)


def test_codex_tokens_only_does_not_persist_false_zero(tmp_path: Path) -> None:
    """(a) tokens-but-no-cost must NOT persist 0.0 — seed clears to NULL."""
    dal = _dal(tmp_path)
    dal.create_session(_session_row(cost_usd=0.0))  # provider_exec-style seed

    parsed = CodexAdapter()._parse(_live_codex_stdout())
    reported = extract(parsed, wall_ms=1_200)
    assert reported.source == SOURCE_TOKENS_ONLY
    assert reported.cost_usd is None

    dal.record_session_usage(
        "ses_codex",
        cost_usd=reported.cost_usd,
        input_tokens=reported.input_tokens,
        output_tokens=reported.output_tokens,
        wall_ms=reported.wall_ms,
        usage_source=reported.source,
    )

    row = dal.get_session("ses_codex")
    assert row is not None
    assert row["input_tokens"] == 17_464
    assert row["output_tokens"] == 5
    assert row["cost_usd"] is None  # not 0.0 — unpriced, not free
    assert row["cost_usd"] != 0.0
    assert row["usage_source"] == SOURCE_TOKENS_ONLY
    assert row["usage_source"] != SOURCE_CLI_REPORT
    assert row["usage_source"] != SOURCE_NONE

    # Budget half unchanged: missing cost still coerces to 0.0 for caps.
    assert float(row.get("cost_usd") or 0.0) == 0.0


def test_explicit_zero_cost_persists_and_differs_from_unpriced(tmp_path: Path) -> None:
    """(b) genuine zero stays distinguishable from unknown cost."""
    dal = _dal(tmp_path)
    dal.create_session(_session_row("ses_free", cost_usd=None))
    dal.create_session(_session_row("ses_unpriced", cost_usd=0.0))

    dal.record_session_usage(
        "ses_free",
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=10,
        wall_ms=500,
        usage_source=SOURCE_CLI_REPORT,
    )
    reported = extract(CodexAdapter()._parse(_live_codex_stdout()), wall_ms=1_200)
    dal.record_session_usage(
        "ses_unpriced",
        cost_usd=reported.cost_usd,
        input_tokens=reported.input_tokens,
        output_tokens=reported.output_tokens,
        wall_ms=reported.wall_ms,
        usage_source=reported.source,
    )

    free = dal.get_session("ses_free")
    unpriced = dal.get_session("ses_unpriced")
    assert free is not None and unpriced is not None
    assert free["cost_usd"] == 0.0
    assert free["usage_source"] == SOURCE_CLI_REPORT
    assert unpriced["cost_usd"] is None
    assert unpriced["usage_source"] == SOURCE_TOKENS_ONLY
    assert free["cost_usd"] != unpriced["cost_usd"] or free["usage_source"] != unpriced[
        "usage_source"
    ]


def test_create_session_default_seed_is_null_not_zero(tmp_path: Path) -> None:
    """Callers that omit cost_usd get NULL, not a false free claim."""
    dal = _dal(tmp_path)
    row = _session_row("ses_default")
    del row["cost_usd"]
    dal.create_session(row)
    stored = dal.get_session("ses_default")
    assert stored is not None
    assert stored["cost_usd"] is None


def test_add_cost_from_null_seed_accumulates(tmp_path: Path) -> None:
    """Stream/budget path: NULL + reported cost must not stay NULL."""
    dal = _dal(tmp_path)
    dal.create_session(_session_row("ses_stream", cost_usd=None))
    assert dal.add_cost("ses_stream", 0.05) is True
    stored = dal.get_session("ses_stream")
    assert stored is not None
    assert stored["cost_usd"] == 0.05


def test_manifest_keeps_null_cost_for_unpriced_session(tmp_path: Path) -> None:
    """Manifest telemetry must not re-introduce false 0.0 for NULL cost."""
    import json

    from omniagentos.sessions.manifest import SessionManifest

    manifest = SessionManifest(tmp_path)
    session = _session_row("ses_manifest", cost_usd=None)
    session.update(
        {
            "input_tokens": 17_464,
            "output_tokens": 5,
            "wall_ms": 1_200,
            "usage_source": SOURCE_TOKENS_ONLY,
            "state": "completed",
        }
    )
    path = manifest.write(session, [])
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["cost_usd"] is None
    assert record["usage_source"] == SOURCE_TOKENS_ONLY
