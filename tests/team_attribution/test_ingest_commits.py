from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.team.ingest import IngestUnavailable, iter_commits


def _commit(repo: Path, filename: str, message: str, timestamp: str) -> str:
    (repo / filename).write_text(f"{message}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", filename], check=True)
    environment = os.environ.copy()
    environment["GIT_AUTHOR_DATE"] = timestamp
    environment["GIT_COMMITTER_DATE"] = timestamp
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        check=True,
        env=environment,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_iter_commits_parses_multiple_commits_and_subject_pipes(git_repo: Path) -> None:
    first = _commit(git_repo, "first.txt", "first | subject", "2020-01-01T12:00:00+00:00")
    second = _commit(git_repo, "second.txt", "refs U3", "2021-01-01T12:00:00+00:00")

    commits = list(iter_commits(str(git_repo), "1970-01-01T00:00:00Z", repo_label="grok"))
    by_sha = {item["ref"]: item for item in commits}
    assert set(by_sha) == {first, second}
    assert by_sha[first]["files"] == ["first.txt"]
    assert by_sha[first]["title"] == "first | subject"
    assert by_sha[second]["files"] == ["second.txt"]
    assert by_sha[second]["actor"] == "owner@initech.example"
    assert by_sha[second]["repo"] == "grok"
    assert by_sha[second]["branch_hint"]


def test_iter_commits_respects_since_window(git_repo: Path) -> None:
    _commit(git_repo, "old.txt", "old", "2020-01-01T12:00:00+00:00")
    recent = _commit(git_repo, "new.txt", "new", "2021-01-01T12:00:00+00:00")

    commits = list(iter_commits(str(git_repo), "2020-06-01T00:00:00Z"))
    assert [item["ref"] for item in commits] == [recent]


def test_iter_commits_wraps_branch_probe_oserror_as_unavailable(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise OSError("git executable missing")

    monkeypatch.setattr("omniagentos.team.ingest.subprocess.run", unavailable)
    with pytest.raises(IngestUnavailable, match="git branch probe failed.*git executable missing"):
        list(iter_commits("/configured/repo", "2026-08-05T00:00:00Z"))
