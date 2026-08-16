from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team import inference
from omniagentos.team.store import TeamStore

MAP = {"bob-dev": "emp_bob", "alice-dev": "emp_alice"}


def commit(
    *,
    ref: str = "a" * 40,
    login: str = "bob-dev",
    title: str = "Implement inference",
) -> dict[str, Any]:
    return {
        "kind": "commit",
        "ref": ref,
        "repo": "omnios",
        "actor": login,
        "title": title,
        "occurred_at": "2026-08-11T12:00:00Z",
        "meta": {"author_name": login},
    }


def pull_request(
    *,
    number: int = 1,
    activity: str = "opened",
    login: str = "bob-dev",
    title: str = "Implement inference",
    branch: str = "feat/GH-1-inference",
) -> dict[str, Any]:
    return {
        "kind": "pr",
        "ref": f"owner/repo#{number}",
        "repo": "owner/repo",
        "actor": login,
        "title": title,
        "occurred_at": "2026-08-11T12:00:00Z",
        "quality_gate": "pass",
        "meta": {
            "activity": activity,
            "body": "Ship the inference loop\nMore detail",
            "head_branch": branch,
        },
    }


def run(
    team_store: TeamStore, collab_store: CollabStore, *activities: dict[str, Any]
) -> inference.InferenceSummary:
    return inference.run_inference(
        team=team_store,
        collab=collab_store,
        activities=activities,
        github_map=MAP,
    )


def card(
    collab_store: CollabStore,
    *,
    title: str = "Implement inference",
    ref: str = "GH-1",
    owner: str = "emp_bob",
    status: BoardTaskStatus = BoardTaskStatus.OPEN,
) -> BoardTask:
    value = BoardTask(
        title=title,
        ref=ref,
        owner_employee_id=owner,
        acceptance_criteria="Land it",
        status=status,
    )
    collab_store.create_board_task(value)
    return value


def test_mapping_and_unknown_login_pass_through(
    team_store: TeamStore, collab_store: CollabStore, employees: dict[str, str]
) -> None:
    summary = run(team_store, collab_store, commit(login="someone-else"))

    assert summary.ignored_unmapped == 1
    assert collab_store.list_board_tasks() == []
    assert team_store.list_unattributed() == []
    assert inference.load_github_map()["bob-dev"] == "emp_bob"


def test_github_map_marks_unknown_employee_id_for_runtime_roster_validation(tmp_path: Path) -> None:
    path = tmp_path / "github.yaml"
    path.write_text("someone: emp_unknown\n", encoding="utf-8")
    assert inference.load_github_map(path) == {"someone": "emp_unknown"}


@pytest.mark.parametrize("matcher", ["ref", "title"])
def test_matches_existing_card_by_ref_or_exactish_title(
    team_store: TeamStore,
    collab_store: CollabStore,
    employees: dict[str, str],
    matcher: str,
) -> None:
    existing = card(collab_store, ref="GH-9" if matcher == "ref" else "GH-1")
    activity = commit(
        title="Draft: Implement inference" if matcher == "title" else "refs GH-9 implement",
        ref=("b" if matcher == "ref" else "c") * 40,
    )

    summary = run(team_store, collab_store, activity)

    assert summary.created_cards == 0
    assert collab_store.get_board_task(existing.id)["status"] == BoardTaskStatus.IN_PROGRESS.value


def test_matches_branch_slug_conservatively() -> None:
    candidate = pull_request(branch="feat/bob-inference")
    task_id = inference._match_task(
        candidate,
        [
            {
                "id": "btk_branch",
                "title": "Different title",
                "ref": "GH-7",
                "status": "open",
                "owner_employee_id": "emp_bob",
                "org": {"branches": ["feat/bob-inference"]},
            }
        ],
    )
    assert task_id == "btk_branch"


def test_create_on_no_match_has_owner_source_ref_and_acceptance(
    team_store: TeamStore, collab_store: CollabStore, employees: dict[str, str]
) -> None:
    summary = run(team_store, collab_store, pull_request(title="New dashboard", number=12))

    created = collab_store.list_board_tasks()[0]
    assert summary.created_cards == 1
    assert created["owner_employee_id"] == "emp_bob"
    assert created["source"] == "inference-github"
    assert created["ref"] == "GH-1"
    assert created["size"] == "S"
    assert created["acceptance_criteria"] == "Ship the inference loop"
    assert created["status"] == BoardTaskStatus.AWAITING_APPROVAL.value
    evidence = team_store.list_evidence(created["id"])[0]
    assert evidence["kind"] == "pr"
    assert evidence["quality_gate"] == "rejected"


def test_ref_conflict_retries_next_gh_number(
    team_store: TeamStore,
    collab_store: CollabStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = collab_store.create_board_task
    attempts = {"count": 0}

    def collide_once(value: BoardTask, *, actor: str = "system") -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("ref_conflict")
        original(value, actor=actor)

    monkeypatch.setattr(collab_store, "create_board_task", collide_once)
    run(team_store, collab_store, pull_request(title="Conflict retry", number=14))

    created = collab_store.list_board_tasks()[0]
    assert attempts["count"] == 2
    assert created["ref"] == "GH-2"


def test_full_advancement_chain_to_done_and_mechanical_verify(
    team_store: TeamStore, collab_store: CollabStore, employees: dict[str, str]
) -> None:
    existing = card(collab_store, ref="GH-1")
    run(team_store, collab_store, commit(title="refs GH-1 activity"))
    assert collab_store.get_board_task(existing.id)["status"] == BoardTaskStatus.IN_PROGRESS.value

    run(team_store, collab_store, pull_request(activity="opened", branch="feat/GH-1-work"))
    assert (
        collab_store.get_board_task(existing.id)["status"]
        == BoardTaskStatus.AWAITING_APPROVAL.value
    )

    summary = run(
        team_store, collab_store, pull_request(activity="merged", branch="feat/GH-1-work")
    )
    done = collab_store.get_board_task(existing.id)
    assert summary.status_changes == 1
    assert summary.verified == 1
    assert done["status"] == BoardTaskStatus.DONE.value
    assert done["verified_by"] == "inference"


def test_never_moves_awaiting_approval_back_to_progress(
    team_store: TeamStore, collab_store: CollabStore, employees: dict[str, str]
) -> None:
    existing = card(collab_store, status=BoardTaskStatus.AWAITING_APPROVAL)
    summary = run(team_store, collab_store, commit(title="refs GH-1 more activity"))

    assert summary.status_changes == 0
    assert (
        collab_store.get_board_task(existing.id)["status"]
        == BoardTaskStatus.AWAITING_APPROVAL.value
    )


def test_second_run_is_idempotent(
    team_store: TeamStore, collab_store: CollabStore, employees: dict[str, str]
) -> None:
    activity = pull_request(title="Idempotent work", number=22)
    first = run(team_store, collab_store, activity)
    created = collab_store.list_board_tasks()[0]
    event_count = len(team_store.list_events(created["id"]))

    second = run(team_store, collab_store, activity)
    assert first.created_cards == 1
    assert second.created_cards == second.attached_evidence == second.status_changes == 0
    assert len(collab_store.list_board_tasks()) == 1
    assert len(team_store.list_events(created["id"])) == event_count


def test_manual_evidence_is_untouched_before_card_creation(
    team_store: TeamStore, collab_store: CollabStore, employees: dict[str, str]
) -> None:
    activity = pull_request(number=30)
    team_store.add_evidence(
        kind="pr",
        ref="owner/repo#30",
        repo="owner/repo",
        attribution="manual",
        actor="emp_bob",
    )

    summary = run(team_store, collab_store, activity)
    assert summary.manual_untouched == 1
    assert collab_store.list_board_tasks() == []
    stored = team_store.list_unattributed()[0]
    assert stored["attribution"] == "manual"


def test_collect_activity_injects_fake_collectors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(inference, "_github_slug", lambda path: "owner/repo")
    monkeypatch.setattr(
        inference.ingest,
        "iter_commits",
        lambda *args, **kwargs: iter([commit(ref="d" * 40)]),
    )

    def fake_prs(*args: Any, **kwargs: Any) -> Any:
        calls.append(("prs", kwargs))
        return iter([pull_request()])

    monkeypatch.setattr(inference.ingest, "iter_prs", fake_prs)
    activities = inference.collect_activity(
        repo_grok="/repo-a", repo_initech="/repo-b", since="2026-08-11T00:00:00Z"
    )
    assert len(activities) == 4
    assert all(
        kwargs["include_open"] is True and kwargs["preflight"] is False for _, kwargs in calls
    )
