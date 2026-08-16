"""Operator privacy policy: inference never reads back into past days."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.team import inference, sweep
from omniagentos.team.ingest import save_cursors
from omniagentos.team.store import TeamStore


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[TeamStore, CollabStore]:
    collab = CollabStore(str(tmp_path / "db.sqlite3"))
    return TeamStore(collab._store), collab


def _run_capturing_since(
    monkeypatch: pytest.MonkeyPatch,
    stores: tuple[TeamStore, CollabStore],
    tmp_path: Path,
    cursor_seed: str | None,
) -> str:
    team, collab = stores
    captured: dict[str, Any] = {}

    def fake_collect(**kwargs: Any) -> list[dict[str, Any]]:
        captured["since"] = kwargs["since"]
        return []

    monkeypatch.setattr(inference, "collect_activity", fake_collect)
    monkeypatch.setattr(
        inference,
        "run_inference",
        lambda **kwargs: inference.InferenceSummary(map_loaded=True),
    )
    monkeypatch.setenv("OMNIAGENTOS_TEAM_INFERENCE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "seed"],
        check=True,
    )
    cursor = tmp_path / "cursor.json"
    if cursor_seed is not None:
        save_cursors(f"{cursor}{sweep._INFERENCE_CURSOR_SUFFIX}", {"inference": cursor_seed})
    sweep.run_sweep(
        store=team,
        repo_grok=str(repo),
        repo_initech=str(repo),
        since=None,
        until=None,
        cursor_path=str(cursor),
        collab_store=collab,
        skip_prs=False,  # the inference gate requires PR mode; no remote = no network
        dry_run=True,
    )
    assert "since" in captured, "inference block never ran"
    return str(captured["since"])


def _parse(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def test_fresh_install_looks_back_at_most_one_hour(
    monkeypatch: pytest.MonkeyPatch,
    stores: tuple[TeamStore, CollabStore],
    tmp_path: Path,
) -> None:
    since = _parse(_run_capturing_since(monkeypatch, stores, tmp_path, None))
    now = dt.datetime.now(dt.UTC)
    assert since >= now - dt.timedelta(hours=1, minutes=2)
    assert since.date() == now.date()  # never yesterday


def test_stale_cursor_never_crosses_midnight_backwards(
    monkeypatch: pytest.MonkeyPatch,
    stores: tuple[TeamStore, CollabStore],
    tmp_path: Path,
) -> None:
    two_days_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    since = _parse(_run_capturing_since(monkeypatch, stores, tmp_path, two_days_ago))
    now = dt.datetime.now(dt.UTC)
    assert since.date() == now.date()
    assert since == now.replace(hour=0, minute=0, second=0, microsecond=0)
