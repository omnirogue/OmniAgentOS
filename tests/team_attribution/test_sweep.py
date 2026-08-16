from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.team import ingest
from omniagentos.team.ingest import IngestUnavailable, load_cursors, save_cursors
from omniagentos.team.sweep import _github_slug, run_sweep


def _commit(repo: Path, message: str, *, filename: str = "change.py") -> str:
    path = repo / filename
    path.write_text(f"{message}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", filename], check=True)
    environment = os.environ.copy()
    environment["GIT_AUTHOR_DATE"] = "2021-01-01T12:00:00+00:00"
    environment["GIT_COMMITTER_DATE"] = "2021-01-01T12:00:00+00:00"
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


def _run(team_store, repo: Path, cursor: Path, **overrides) -> int:
    arguments = {
        "store": team_store,
        "repo_grok": str(repo),
        "repo_initech": str(repo),
        "since": "2020-01-01T00:00:00Z",
        "until": "2022-01-01T00:00:00Z",
        "cursor_path": str(cursor),
        "dry_run": False,
        "skip_prs": True,
        "now": "2026-08-10T12:00:00Z",
    }
    arguments.update(overrides)
    return run_sweep(**arguments)


def test_sweep_attributes_explicit_ref_and_records_matcher(
    team_store, make_card, git_repo: Path, tmp_path: Path
) -> None:
    task = make_card(ref="U3", status="open", title="Referenced work")
    sha = _commit(git_repo, "refs U3")

    assert _run(team_store, git_repo, tmp_path / "cursors.json") == 0

    evidence = team_store.list_evidence(task.id)
    assert len(evidence) == 1
    assert evidence[0]["ref"] == sha
    assert evidence[0]["attribution"] == "deterministic"
    assert evidence[0]["meta"]["matcher"] == "explicit_ref"


def test_sweep_keeps_refless_commit_unattributed(
    team_store, git_repo: Path, tmp_path: Path
) -> None:
    sha = _commit(git_repo, "unrelated maintenance")
    assert _run(team_store, git_repo, tmp_path / "cursors.json") == 0
    evidence = team_store.list_unattributed()
    assert len(evidence) == 1
    assert evidence[0]["ref"] == sha
    assert evidence[0]["task_id"] is None


def test_resweeping_same_window_is_idempotent(team_store, git_repo: Path, tmp_path: Path) -> None:
    _commit(git_repo, "unrelated maintenance")
    cursor = tmp_path / "cursors.json"
    assert _run(team_store, git_repo, cursor) == 0
    before = team_store._connection.execute("SELECT COUNT(*) FROM task_evidence").fetchone()[0]
    assert _run(team_store, git_repo, cursor) == 0
    after = team_store._connection.execute("SELECT COUNT(*) FROM task_evidence").fetchone()[0]
    assert before == after == 1


def test_dry_run_writes_no_evidence_or_cursor(team_store, git_repo: Path, tmp_path: Path) -> None:
    _commit(git_repo, "refs U3")
    cursor = tmp_path / "dry-cursors.json"
    assert _run(team_store, git_repo, cursor, dry_run=True) == 0
    count = team_store._connection.execute("SELECT COUNT(*) FROM task_evidence").fetchone()[0]
    assert count == 0
    assert not cursor.exists()


def test_success_advances_each_completed_source_cursor(
    team_store, git_repo: Path, tmp_path: Path
) -> None:
    _commit(git_repo, "maintenance")
    cursor = tmp_path / "cursors.json"
    assert _run(team_store, git_repo, cursor, since=None, until=None) == 0
    cursors = load_cursors(str(cursor))
    assert cursors["commits:grok"] == "2026-08-10T12:00:00Z"
    assert cursors["commits:initech"] == "2026-08-10T12:00:00Z"
    assert cursors["sessions"] == "2026-08-10T12:00:00Z"


def test_sweep_ingests_task_session_with_preset_task_id(
    team_store, make_card, git_repo: Path, tmp_path: Path
) -> None:
    task = make_card(status="in_progress", title="Session-backed work")
    team_store._connection.execute(
        "INSERT INTO task_sessions "
        "(id, board_task_id, seq, harness, model, started_at, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("tks_test", task.id, 0, "cli-codex", "gpt-test", "2021-01-01T12:00:00Z", ""),
    )
    team_store._connection.commit()

    assert _run(team_store, git_repo, tmp_path / "cursors.json") == 0
    evidence = team_store.list_evidence(task.id)
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "session"
    assert evidence[0]["ref"] == "tks_test"
    assert evidence[0]["attribution"] == "deterministic"


def test_manual_reattribution_survives_resweep(
    team_store, make_card, git_repo: Path, tmp_path: Path
) -> None:
    task_a = make_card(ref="U3", status="open", title="Initially matched")
    task_b = make_card(ref="S5", status="open", title="Human correction")
    _commit(git_repo, "refs U3")
    cursor = tmp_path / "cursors.json"
    assert _run(team_store, git_repo, cursor) == 0
    original = team_store.list_evidence(task_a.id)[0]

    corrected = team_store.reattribute_evidence(original["id"], task_b.id, actor="owner")
    assert corrected is not None
    assert _run(team_store, git_repo, cursor) == 0

    preserved = team_store.get_evidence(original["id"])
    assert preserved is not None
    assert preserved["task_id"] == task_b.id
    assert preserved["attribution"] == "manual"


def test_bad_candidate_is_visible_and_does_not_stop_later_candidates(
    team_store, git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    def candidates(_repo_path, _since, _until=None, *, repo_label=None):
        yield {"ref": f"bad-{repo_label}"}  # no kind: candidate-local failure
        yield {
            "kind": "commit",
            "ref": f"good-{repo_label}",
            "repo": str(repo_label),
            "actor": "",
            "title": "maintenance",
            "occurred_at": "2026-08-10T10:00:00Z",
            "files": [],
        }

    monkeypatch.setattr(ingest, "iter_commits", candidates)
    cursor = tmp_path / "candidate-cursors.json"

    assert _run(team_store, git_repo, cursor, since=None, until=None) == 0

    assert {row["ref"] for row in team_store.list_unattributed()} == {
        f"good-{git_repo}",
    }
    assert capsys.readouterr().err.count("candidate ?:bad-") == 2
    cursors = load_cursors(str(cursor))
    # A failed candidate with no parseable occurred_at cannot be re-found, so the
    # cursor still advances; the alert above is its only receipt.
    assert cursors["commits:grok"] == "2026-08-10T12:00:00Z"
    assert cursors["commits:initech"] == "2026-08-10T12:00:00Z"


def test_failed_candidate_with_timestamp_rewinds_the_cursor(
    team_store, git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """A timestamped write failure must stay collectable: the cursor rewinds to the
    oldest failed candidate instead of freezing it as never-collected (review NEW-1)."""

    def candidates(_repo_path, _since, _until=None, *, repo_label=None):
        yield {
            "kind": "commit",
            "ref": f"good-{repo_label}",
            "repo": str(repo_label),
            "actor": "",
            "title": "maintenance",
            "occurred_at": "2026-08-10T10:00:00Z",
            "files": [],
        }
        yield {
            "kind": "commit",
            "ref": None,  # unrepresentable ref: the store write raises
            "repo": str(repo_label),
            "actor": "",
            "title": "poison",
            "occurred_at": "2026-08-10T09:00:00Z",
            "files": [],
        }

    monkeypatch.setattr(ingest, "iter_commits", candidates)
    cursor = tmp_path / "rewind-cursors.json"

    assert _run(team_store, git_repo, cursor, since=None, until=None) == 0
    assert capsys.readouterr().err.count("failed") >= 1
    cursors = load_cursors(str(cursor))
    # Rewound to the failed candidate's own timestamp, not the sweep end.
    assert cursors["commits:grok"] == "2026-08-10T09:00:00+00:00"
    assert cursors["commits:initech"] == "2026-08-10T09:00:00+00:00"


def test_incomplete_source_iteration_does_not_advance_cursor(
    team_store, git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    def candidates(_repo_path, _since, _until=None, *, repo_label=None):
        if repo_label == "grok-label":
            yield {
                "kind": "commit",
                "ref": "partial",
                "repo": "grok-label",
                "actor": "",
                "title": "partial page",
                "occurred_at": "2026-08-10T10:00:00Z",
                "files": [],
            }
            raise IngestUnavailable("page two unavailable")

    monkeypatch.setattr(ingest, "iter_commits", candidates)
    cursor = tmp_path / "partial-cursors.json"
    save_cursors(str(cursor), {"commits:grok": "2026-08-09T00:00:00Z"})

    code = run_sweep(
        store=team_store,
        repo_grok="grok-label",
        repo_initech="omni-label",
        since=None,
        until=None,
        cursor_path=str(cursor),
        skip_prs=True,
        now="2026-08-10T12:00:00Z",
    )

    assert code == 0
    cursors = load_cursors(str(cursor))
    assert cursors["commits:grok"] == "2026-08-09T00:00:00Z"
    assert cursors["commits:initech"] == "2026-08-10T12:00:00Z"
    assert cursors["sessions"] == "2026-08-10T12:00:00Z"


def test_unresolvable_github_slug_is_alerted_once_and_counted_failed(
    team_store, tmp_path: Path, monkeypatch, capsys
) -> None:
    def unavailable_commits(*_args, **_kwargs):
        raise IngestUnavailable("commit source unavailable")

    def unavailable_sessions(*_args, **_kwargs):
        raise IngestUnavailable("session source unavailable")

    def slug(repo_path: str) -> str | None:
        return None if repo_path == "missing-remote" else "org/repo"

    def unavailable_preflight() -> None:
        raise IngestUnavailable("gh unavailable")

    monkeypatch.setattr(ingest, "iter_commits", unavailable_commits)
    monkeypatch.setattr(ingest, "iter_sessions", unavailable_sessions)
    monkeypatch.setattr("omniagentos.team.sweep._github_slug", slug)
    monkeypatch.setattr(ingest, "preflight_github", unavailable_preflight)
    monkeypatch.setattr("omniagentos.team.sweep.push_alert", None)

    code = run_sweep(
        store=team_store,
        repo_grok="missing-remote",
        repo_initech="good-remote",
        since="2026-08-09T00:00:00Z",
        until="2026-08-10T00:00:00Z",
        cursor_path=str(tmp_path / "unused.json"),
    )

    assert code == 2
    stderr = capsys.readouterr().err
    lines = [line for line in stderr.splitlines() if "source prs:grok unavailable" in line]
    assert len(lines) == 1
    assert "could not resolve GitHub slug for configured repo missing-remote" in lines[0]


def test_run_sweep_preflights_github_once_for_both_repositories(
    team_store, git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    preflights: list[None] = []
    pr_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "omniagentos.team.sweep._github_slug", lambda repo_path: f"org/{Path(repo_path).name}"
    )
    monkeypatch.setattr(ingest, "preflight_github", lambda: preflights.append(None))

    def prs(repo_slug: str, _since: str, *, preflight: bool = True):
        pr_calls.append((repo_slug, preflight))
        return iter(())

    monkeypatch.setattr(ingest, "iter_prs", prs)

    assert _run(
        team_store,
        git_repo,
        tmp_path / "unused.json",
        skip_prs=False,
    ) == 0
    assert preflights == [None]
    assert len(pr_calls) == 2
    assert all(preflight is False for _slug, preflight in pr_calls)


def test_explicit_until_run_never_reads_or_writes_cursors(
    team_store, git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("bounded replay touched live cursors")

    monkeypatch.setattr(ingest, "load_cursors", forbidden)
    monkeypatch.setattr(ingest, "save_cursors", forbidden)
    cursor = tmp_path / "must-not-exist.json"

    assert _run(team_store, git_repo, cursor) == 0
    assert not cursor.exists()


def test_sweep_attributes_evidence_to_card_done_within_window(
    team_store, collab_store, make_card, tmp_path: Path, monkeypatch
) -> None:
    task = make_card(ref="U3", status="open", title="Just completed")
    assert collab_store.update_board_task(task.id, {"status": "done"})
    team_store._connection.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND to_status = 'done'",
        ("2026-08-10T09:00:00Z", task.id),
    )
    team_store._connection.commit()

    def candidates(_repo_path, _since, _until=None, *, repo_label=None):
        if repo_label == "grok-label":
            yield {
                "kind": "commit",
                "ref": "done-card-sha",
                "repo": "grok-label",
                "actor": "",
                "title": "refs U3",
                "occurred_at": "2026-08-10T10:00:00Z",
                "files": [],
            }

    monkeypatch.setattr(ingest, "iter_commits", candidates)
    code = run_sweep(
        store=team_store,
        repo_grok="grok-label",
        repo_initech="omni-label",
        since="2026-08-10T00:00:00Z",
        until="2026-08-10T23:59:59Z",
        cursor_path=str(tmp_path / "unused.json"),
        skip_prs=True,
    )

    assert code == 0
    evidence = team_store.list_evidence(task.id)
    assert [row["ref"] for row in evidence] == ["done-card-sha"]
    assert evidence[0]["meta"]["matcher"] == "explicit_ref"


def test_github_slug_probe_oserror_is_ingest_unavailable(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise OSError("git executable missing")

    monkeypatch.setattr("omniagentos.team.sweep.subprocess.run", unavailable)
    with pytest.raises(IngestUnavailable, match="git remote probe failed.*git executable missing"):
        _github_slug("/configured/repo")
