"""Unit tests for omniagentos.sessions.transcript (live CLI transcript resolution).

The resolution rule: <config_dir>/projects/<slug(project_dir)>/<session_ref>.jsonl,
config_dir from the session's claude_accounts row (default ~/.claude), slug =
project_dir with every non-alphanumeric char munged to '-'. The ledger
sessions/<id>.jsonl manifest is the terminal audit record, never the live
transcript.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions.transcript import (
    config_dir_candidates,
    final_reply_text,
    is_provider_session,
    live_transcript_path,
    project_dir_slug,
)

SESSION: dict[str, Any] = {
    "id": "ses_x",
    "provider": "claude",
    "session_ref": "ref-123",
    "project_dir": "/work/my proj",
    "account_id": "acct_x",
}


def test_project_dir_slug_munges_every_non_alphanumeric() -> None:
    assert project_dir_slug("/work/my proj") == "-work-my-proj"
    assert project_dir_slug("/Users/owner.sb/repo_1") == "-Users-owner-sb-repo-1"


def test_is_provider_session() -> None:
    assert not is_provider_session(SESSION)
    assert not is_provider_session({"provider": ""})
    assert is_provider_session({"provider": "codex"})
    assert is_provider_session({"provider": "Gemini"})


def test_live_path_uses_account_config_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "claude-account-4"
    path = live_transcript_path(
        SESSION, account_lookup=lambda account_id: {"config_dir": str(config_dir)}
    )
    assert path == config_dir / "projects" / "-work-my-proj" / "ref-123.jsonl"


def test_live_path_explicit_config_dir_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    path = live_transcript_path(
        SESSION,
        account_lookup=lambda account_id: {"config_dir": str(tmp_path / "other")},
        config_dir=str(explicit),
    )
    assert path == explicit / "projects" / "-work-my-proj" / "ref-123.jsonl"


def test_live_path_defaults_to_home_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    session = {**SESSION, "account_id": None}
    path = live_transcript_path(session, account_lookup=lambda account_id: None)
    assert path == tmp_path / ".claude" / "projects" / "-work-my-proj" / "ref-123.jsonl"


def test_live_path_unknown_account_degrades_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = live_transcript_path(SESSION, account_lookup=lambda account_id: None)
    assert path == tmp_path / ".claude" / "projects" / "-work-my-proj" / "ref-123.jsonl"


def test_live_path_lookup_fault_degrades_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def boom(account_id: str) -> None:
        raise RuntimeError("db gone")

    path = live_transcript_path(SESSION, account_lookup=boom)
    assert path == tmp_path / ".claude" / "projects" / "-work-my-proj" / "ref-123.jsonl"


def test_live_path_none_for_provider_sessions() -> None:
    assert live_transcript_path({**SESSION, "provider": "codex"}) is None


def test_live_path_none_without_refs() -> None:
    assert live_transcript_path({**SESSION, "session_ref": None}) is None
    assert live_transcript_path({**SESSION, "session_ref": ""}) is None
    assert live_transcript_path({**SESSION, "project_dir": None}) is None
    assert live_transcript_path(None) is None


def test_config_dir_candidates_precedence_and_dedupe(tmp_path: Path) -> None:
    dirs = config_dir_candidates(
        SESSION,
        account_lookup=lambda account_id: {"config_dir": "/acct/dir"},
        extra_config_dirs=("/handle/dir", "/acct/dir", None, ""),
    )
    assert dirs[:2] == ["/handle/dir", "/acct/dir"]
    assert dirs[-1].endswith(".claude")
    assert len(dirs) == len(set(dirs))


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def test_final_reply_text_prefers_result(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    transcript = config_dir / "projects" / "-work-my-proj" / "ref-123.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "interim"}]}},
            {"type": "result", "result": "final answer"},
        ],
    )
    text = final_reply_text(
        SESSION, account_lookup=lambda account_id: {"config_dir": str(config_dir)}
    )
    assert text == "final answer"


def test_final_reply_text_falls_back_to_last_assistant(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    transcript = config_dir / "projects" / "-work-my-proj" / "ref-123.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
            {"not": "json is skipped before this line"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "last"}]}},
        ],
    )
    # An unparseable raw line must not break the harvest.
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write("{broken json\n")
    text = final_reply_text(
        SESSION, account_lookup=lambda account_id: {"config_dir": str(config_dir)}
    )
    assert text == "last"


def test_final_reply_text_none_when_unresolvable(tmp_path: Path) -> None:
    assert final_reply_text({**SESSION, "provider": "codex"}) is None
    assert (
        final_reply_text(
            SESSION, account_lookup=lambda account_id: {"config_dir": str(tmp_path / "nope")}
        )
        is None
    )
