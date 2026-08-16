"""Wire 2 -- learn from session transcripts.

`curator.curate_sessions()` mines the Session Bridge's own terminal artifact
(`<ledger_dir>/sessions/<session_id>.jsonl`, `SessionManifest`) for reusable
discoveries (explicit corrections, preferred methods, failures->fixes),
routing captures/proposals through the SAME skills path `curate()` (the
run-ledger miner) already uses -- see tests/selfimprove/test_curator.py for
that sibling suite and the `MockSkills` shape this mirrors."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.selfimprove import curator
from omniagentos.selfimprove.paths import skill_relpath


@dataclass
class MockSkills:
    """Small in-memory stand-in for S-A's skills service contract (mirrors
    tests/selfimprove/test_curator.py's MockSkills -- duplicated rather than
    imported so this suite stays isolated from that file)."""

    seeded: list[dict[str, Any]] = field(default_factory=list)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposed: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    upserts: list[dict[str, Any]] = field(default_factory=list)

    def list_tree(self) -> list[dict[str, Any]]:
        return list(self.seeded)

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        return self.records[skill_id]

    def propose_update(self, skill_id: str, **kwargs: Any) -> str:
        self.proposed.append({"skill_id": skill_id, **kwargs})
        return f"proposal-{len(self.proposed)}"

    def decide_proposal(self, proposal_id: str, **kwargs: Any) -> dict[str, str]:
        self.decisions.append({"proposal_id": proposal_id, **kwargs})
        return {"state": "approved" if kwargs["approve"] else "rejected"}

    def upsert_skill(self, data: dict[str, Any]) -> str:
        self.upserts.append(data)
        # A real skills backend makes an upserted skill immediately gettable --
        # matters when two mined sessions in the SAME curate_sessions() scan
        # fuzzy-match each other (the second's propose_update path calls
        # get_skill() on the first's freshly-upserted id).
        self.records[str(data["id"])] = data
        return str(data["id"])


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ledger"
    d.mkdir()
    return d


def _write_transcript(
    ledger_dir: Path,
    session_id: str,
    *,
    source: str = "bridge",
    final_state: str = "completed",
    project_dir: str = "/work/demo",
    model: str | None = "sonnet/high",
    approvals_requested: int = 0,
    approvals_granted: int = 0,
    extra_events: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a `<ledger_dir>/sessions/<session_id>.jsonl` fixture: the real
    SessionManifest summary line, plus any additional per-turn transcript
    events a richer capture would append to the same file (see
    `curator._read_jsonl_events`'s schema-agnostic reader)."""
    sessions_dir = ledger_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "session_id": session_id,
        "source": source,
        "project_dir": project_dir,
        "provider": "claude",
        "session_ref": f"ref-{session_id}",
        "final_state": final_state,
        "model": model,
        "started_at": "2026-07-20T12:00:00Z",
        "finished_at": "2026-07-20T12:30:00Z",
        "cost_usd": 0.05,
        "approvals_requested": approvals_requested,
        "approvals_granted": approvals_granted,
        "approvals_denied": 0,
        "killed_by": None,
    }
    lines = [json.dumps(summary)]
    for event in extra_events or []:
        lines.append(json.dumps(event))
    path = sessions_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _chatter_events() -> list[dict[str, Any]]:
    """Routine, unremarkable transcript turns -- no correction/fix markers."""
    return [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Reading the file now."}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Tests pass, all good."}]},
        },
    ]


def _discovery_events(snippet: str) -> list[dict[str, Any]]:
    return [
        *_chatter_events(),
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": snippet}]},
        },
    ]


def test_curate_sessions_captures_skill_from_explicit_fix(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_fix1",
        extra_events=_discovery_events(
            "The fix was to always close the file handle explicitly after writing."
        ),
    )

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.scanned == 1
    assert result.captured == ["ses_fix1"]
    assert result.unverified == []
    assert result.errors == {}
    relpath = skill_relpath(curator._skill_id_for_session("ses_fix1"))
    note = vault_dir / relpath
    assert note.is_file()
    content = note.read_text(encoding="utf-8")
    assert "fix" in content.lower()
    assert "close the file handle explicitly" in content


def test_curate_sessions_captures_skill_from_explicit_correction(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_corr1",
        extra_events=_discovery_events(
            "Remember to always validate the schema before writing to disk."
        ),
    )

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == ["ses_corr1"]
    note = vault_dir / skill_relpath(curator._skill_id_for_session("ses_corr1"))
    assert note.is_file()
    assert "validate the schema" in note.read_text(encoding="utf-8")


def test_curate_sessions_ignores_routine_chatter(ledger_dir: Path, vault_dir: Path) -> None:
    _write_transcript(ledger_dir, "ses_chatter1", extra_events=_chatter_events())

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.scanned == 1
    assert result.captured == []
    assert result.proposals == {}
    assert result.unverified == []
    assert result.errors == {}
    assert not (vault_dir / "playbook").exists()


def test_curate_sessions_ignores_sessions_with_no_transcript_events_beyond_summary(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(ledger_dir, "ses_bare1")  # no extra_events at all

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == []
    assert not (vault_dir / "playbook").exists()


def test_curate_sessions_never_mines_external_non_bridge_sessions(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_ext1",
        source="external",
        extra_events=_discovery_events("The fix was to restart the daemon."),
    )

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == []
    assert result.unverified == []
    assert not (vault_dir / "playbook").exists()


def test_curate_sessions_preserves_verification_gate_for_non_completed_sessions(
    ledger_dir: Path, vault_dir: Path
) -> None:
    """Even a session with a crystal-clear discovery is refused (HARD RULE)
    when its own final_state was not "completed" -- the same treatment
    curate() gives a FAILED/CANCELLED RunManifest."""
    _write_transcript(
        ledger_dir,
        "ses_failed1",
        final_state="failed",
        extra_events=_discovery_events("The fix was to retry with backoff."),
    )

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == []
    assert result.unverified == ["ses_failed1"]
    assert not (vault_dir / "playbook").exists()


def test_curate_sessions_is_idempotent_across_repeated_invocations(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_dup1",
        extra_events=_discovery_events("The fix was to use pathlib instead of raw strings."),
    )

    first = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))
    second = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert first.captured == ["ses_dup1"]
    assert second.captured == []
    assert second.already_captured == ["ses_dup1"]
    assert second.errors == {}


def test_curate_sessions_no_double_mining_when_matched_to_existing_skill(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_match1",
        extra_events=_discovery_events("The fix was to close the file handle explicitly."),
    )
    existing = {
        "id": "skill-existing-session",
        "slug": "unrelated-slug",
        "title": "Session learning (fix) for /work/demo",
        "discipline": "session",
        "tags": ["session", "fix"],
        "preferred_method": "sonnet/high",
        "content": "# Existing session learning\nSummary: previous wording.",
    }
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)
    second = curator.curate_sessions(
        ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills
    )

    assert len(skills.proposed) == 1
    assert second.already_captured == ["ses_match1"]


def test_curate_sessions_matches_learning_to_existing_skill_and_proposes_update(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_match2",
        extra_events=_discovery_events("The fix was to close the file handle explicitly."),
    )
    existing = {
        "id": "skill-existing-session",
        "slug": "unrelated-slug",
        "title": "Session learning (fix) for /work/demo",
        "discipline": "session",
        "tags": ["session", "fix"],
        "preferred_method": "sonnet/high",
        "content": "# Existing session learning\nSummary: previous wording.",
    }
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate_sessions(
        ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills
    )

    assert result.captured == []
    assert result.proposals == {"ses_match2": "proposal-1"}
    assert skills.proposed[0]["skill_id"] == "skill-existing-session"
    assert skills.proposed[0]["linked_execution"] == "ses_match2"


def test_curate_sessions_captures_new_skill_when_no_match(
    ledger_dir: Path, vault_dir: Path
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_new1",
        extra_events=_discovery_events("The fix was to close the file handle explicitly."),
    )
    existing = {
        "id": "skill-unrelated",
        "slug": "unrelated",
        "title": "A completely separate marketing workflow",
        "discipline": "marketing",
        "tags": ["marketing"],
        "preferred_method": "gpt",
        "content": "# marketing\nSummary: unrelated.",
    }
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate_sessions(
        ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills
    )

    assert result.captured == ["ses_new1"]
    assert result.proposals == {}
    assert len(skills.upserts) == 1
    assert (vault_dir / skill_relpath(curator._skill_id_for_session("ses_new1"))).is_file()


def test_curate_sessions_records_per_session_errors_without_aborting_the_scan(
    ledger_dir: Path, vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_ok1",
        extra_events=_discovery_events("The fix was to add a retry."),
    )
    _write_transcript(
        ledger_dir,
        "ses_boom1",
        extra_events=_discovery_events("The fix was to add a timeout."),
    )

    real_capture_skill = curator.capture_skill

    def _flaky(metadata: Any, gate: Any, vault_dir_arg: Any, **kwargs: Any) -> Any:
        if gate.source_run_id == "ses_boom1":
            raise RuntimeError("boom")
        return real_capture_skill(metadata, gate, vault_dir_arg, **kwargs)

    monkeypatch.setattr(curator, "capture_skill", _flaky)

    result = curator.curate_sessions(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == ["ses_ok1"]
    assert "ses_boom1" in result.errors
    assert "boom" in result.errors["ses_boom1"]


def test_curate_sessions_defaults_to_env_ledger_and_vault_dirs_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    import shutil

    shutil.copy(Path(__file__).resolve().parents[2] / "vault" / "Home.md", vault_dir / "Home.md")
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))
    _write_transcript(
        ledger_dir,
        "ses_env1",
        extra_events=_discovery_events("The fix was to add input validation."),
    )

    result = curator.curate_sessions()

    assert result.captured == ["ses_env1"]


def test_curate_sessions_scans_newest_first_up_to_limit(ledger_dir: Path, vault_dir: Path) -> None:
    import os
    import time

    skills = MockSkills()
    for i in range(5):
        path = _write_transcript(
            ledger_dir,
            f"ses_lim{i}",
            project_dir=f"/work/demo-{i}",
            extra_events=_discovery_events(f"The fix was to patch case {i}."),
        )
        mtime = time.time() + i
        os.utime(path, (mtime, mtime))

    result = curator.curate_sessions(
        ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), limit=2, skills_api=skills
    )

    # Only the 2 newest (by mtime) transcripts are scanned at all; each was
    # processed into a skill -- captured as new, or proposed as a fuzzy-matched
    # update (the >=70% title-similarity matcher is curate()'s existing,
    # deliberately lenient behaviour -- not something this test re-asserts).
    assert result.scanned == 2
    assert result.errors == {}
    handled = set(result.captured) | set(result.proposals.keys())
    assert handled == {"ses_lim4", "ses_lim3"}


def test_main_runs_both_run_and_session_curation(
    ledger_dir: Path, vault_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_cli1",
        extra_events=_discovery_events("The fix was to add a health check."),
    )

    exit_code = curator.main(["--ledger-dir", str(ledger_dir), "--vault-dir", str(vault_dir)])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["runs"]["scanned"] == 0
    assert printed["sessions"]["captured"] == ["ses_cli1"]


def test_main_skip_sessions_flag_only_runs_run_curation(
    ledger_dir: Path, vault_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_transcript(
        ledger_dir,
        "ses_skip1",
        extra_events=_discovery_events("The fix was to add a health check."),
    )

    exit_code = curator.main(
        ["--ledger-dir", str(ledger_dir), "--vault-dir", str(vault_dir), "--skip-sessions"]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert "sessions" not in printed
    assert not (vault_dir / skill_relpath(curator._skill_id_for_session("ses_skip1"))).is_file()
