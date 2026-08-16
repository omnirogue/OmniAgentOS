from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.lab.curator.__main__ import main


def test_dry_run_cli_prints_json_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault_dir = tmp_path / "vault"
    exit_code = main(
        [
            "--dry-run",
            "--db-path",
            ":memory:",
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--vault-dir",
            str(vault_dir),
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert output["subjects"] == []
    assert output["leaderboard"] == {}
    assert output["notes_written"] == []
    assert "agent_status" not in output  # live agent is opt-in only
    assert not vault_dir.exists()


def test_non_dry_run_cli_persists_via_monkeypatched_l08(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omniagentos.lab.contracts import Elo, Tournament
    from omniagentos.lab.db import LabStore

    db_path = str(tmp_path / "lab.db")
    seed_store = LabStore(db_path)
    seed_store.create_tournament(
        Tournament(id="tnm_1", subject="s1", discipline="d1", arena_task_hash="h", config_ids=["a"])
    )
    seed_store.upsert_elo(Elo(subject="s1", config_id="a", rating=1000))

    written: list[str] = []
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_leaderboard_note",
        lambda subject, rows: (f"leaderboard/{subject}.md", "content"),
        raising=False,
    )
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_playbook_note",
        lambda discipline, entries: (f"playbook/{discipline}.md", "content"),
        raising=False,
    )

    def _write_note(vault_dir: str, relpath: str, content: str) -> str:
        written.append(relpath)
        return relpath

    monkeypatch.setattr("omniagentos.vault.write_note", _write_note, raising=False)

    exit_code = main(
        [
            "--db-path",
            db_path,
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--vault-dir",
            str(tmp_path / "vault"),
        ]
    )

    assert exit_code == 0
    assert written == ["leaderboard/s1.md"]
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is False
    assert output["notes_written"] == ["leaderboard/s1.md"]


def test_main_never_invokes_the_live_agent_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even a REAL (non-dry-run) invocation must not touch the adapter
    registry unless the operator explicitly opted in (OMNIAGENTOS_CURATOR_LIVE_AGENT=1)."""
    monkeypatch.delenv("OMNIAGENTOS_CURATOR_LIVE_AGENT", raising=False)
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_leaderboard_note",
        lambda subject, rows: (f"leaderboard/{subject}.md", "content"),
        raising=False,
    )
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_playbook_note",
        lambda discipline, entries: (f"playbook/{discipline}.md", "content"),
        raising=False,
    )
    monkeypatch.setattr("omniagentos.vault.write_note", lambda *a, **k: a[1], raising=False)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("main() must not resolve a live adapter without opt-in")

    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", _boom, raising=False)

    exit_code = main(
        [
            "--db-path",
            ":memory:",
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--vault-dir",
            str(tmp_path / "vault"),
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is False
    assert "agent_status" not in output
