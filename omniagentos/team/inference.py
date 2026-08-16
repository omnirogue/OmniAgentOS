"""Conservative GitHub-activity inference for the Team Work OS.

The sweep invokes this pass only when ``OMNIAGENTOS_TEAM_INFERENCE`` is truthy.
Start new deployments with a dry-run soak: set that flag and invoke the sweep
with ``--dry-run`` for at least one normal collection window, inspect its sorted
JSON output and alerts, then enable writes.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.team import attribution, ingest
from omniagentos.team.store import TeamStore

_DEFAULT_GITHUB_MAP_PATH = (
    Path(__file__).resolve().parent.parent.parent / "configs/team_github_map.yaml"
)
_TERMINAL_STATUSES = frozenset({BoardTaskStatus.DONE.value, BoardTaskStatus.CANCELLED.value})
_ADVANCEABLE = frozenset(
    {BoardTaskStatus.OPEN.value, BoardTaskStatus.CLAIMED.value, BoardTaskStatus.IN_PROGRESS.value}
)
_GH_REF_RE = re.compile(r"^GH-(\d+)$", re.IGNORECASE)
_TITLE_WORD_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_CREATION_CAP = 10


@dataclass(frozen=True)
class InferenceSummary:
    seen: int = 0
    ignored_unmapped: int = 0
    created_cards: int = 0
    attached_evidence: int = 0
    status_changes: int = 0
    verified: int = 0
    manual_untouched: int = 0
    map_loaded: bool = True
    failed_candidates: int = 0
    lost_cas: int = 0
    creation_cap_hit: int = 0

    def plus(self, **changes: Any) -> InferenceSummary:
        return InferenceSummary(**(self.__dict__ | changes))


def load_github_map(path: Path | None = None) -> ingest.ActorEmployeeMap:
    """Load the single identity config without turning a bad map into a crash."""

    mapping = ingest.load_actor_employee_map(path or _DEFAULT_GITHUB_MAP_PATH)
    if not mapping.loaded:
        print(f"inference: {mapping.error}", file=sys.stderr)
    return mapping


def _roster_ids(collab: CollabStore) -> set[str]:
    return {str(row["id"]) for row in CompanyGoalsStore(collab._store).list_employees()}


def _validate_mapping(mapping: dict[str, str], collab: CollabStore) -> tuple[bool, str]:
    roster = _roster_ids(collab)
    unknown = sorted({employee for employee in mapping.values() if employee not in roster})
    return (not unknown, "" if not unknown else f"unknown employee ids: {', '.join(unknown)}")


def _github_slug(repo_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ingest.IngestUnavailable(f"git remote probe failed for {repo_path}: {exc}") from exc
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", result.stdout.strip())
    return match.group(1) if result.returncode == 0 and match else None


def collect_activity(
    *, repo_grok: str, repo_initech: str, since: str, until: str | None = None
) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    for repo_path in (repo_grok, repo_initech):
        activities.extend(ingest.iter_commits(repo_path, since, until, repo_label=repo_path))
        if (slug := _github_slug(repo_path)) is not None:
            activities.extend(ingest.iter_prs(slug, since, preflight=False, include_open=True))
    return activities


def _candidate_login(candidate: dict[str, Any], mapping: dict[str, str]) -> str | None:
    meta: dict[str, Any] = candidate["meta"] if isinstance(candidate.get("meta"), dict) else {}
    for value in (candidate.get("actor"), meta.get("github_login"), meta.get("author_name")):
        if (text := str(value or "")) in mapping:
            return text
        if text in ingest.ACTOR_EMPLOYEE_MAP:
            return text
    return None


def _title_key(value: object) -> str:
    return _TITLE_WORD_RE.sub(
        " ", re.sub(r"^(?:draft|wip)\s*[:\-]\s*", "", str(value or "").lower().strip())
    ).strip()


def _activity_branch(candidate: dict[str, Any]) -> str:
    meta: dict[str, Any] = candidate["meta"] if isinstance(candidate.get("meta"), dict) else {}
    return str(meta.get("head_branch") or "")


def _acceptance_criteria(candidate: dict[str, Any]) -> str:
    body = (
        str((candidate.get("meta") or {}).get("body") or "")
        if isinstance(candidate.get("meta"), dict)
        else ""
    )
    for line in body.splitlines():
        if clean := line.strip().lstrip("#*- ").strip():
            return clean[:500]
    return (
        f"Land {_activity_branch(candidate)}"
        if _activity_branch(candidate)
        else "Land inferred GitHub activity"
    )


def _default_goal_id(collab: CollabStore, explicit_goal_id: str | None) -> str | None:
    designated = explicit_goal_id or os.getenv("OMNIAGENTOS_TEAM_DEFAULT_GOAL_ID")
    goal = CompanyGoalsStore(collab._store).get_goal(designated) if designated else None
    return str(goal["id"]) if goal is not None and goal.get("status") == "active" else None


def _next_ref(tasks: Iterable[dict[str, Any]]) -> int:
    return (
        max(
            (
                int(m.group(1))
                for task in tasks
                if (m := _GH_REF_RE.match(str(task.get("ref") or ""))) is not None
            ),
            default=0,
        )
        + 1
    )


def _evidence_fields(candidate: dict[str, Any]) -> dict[str, str]:
    meta: dict[str, Any] = candidate["meta"] if isinstance(candidate.get("meta"), dict) else {}
    # Defend the writer too: callers outside ingest cannot make an open PR pass-gated.
    gate = (
        "rejected"
        if candidate.get("kind") == "pr" and meta.get("activity") == "opened"
        else str(candidate.get("quality_gate") or "pass")
    )
    return {
        "kind": str(candidate.get("kind") or "note"),
        "ref": str(candidate.get("ref") or ""),
        "quality_gate": gate,
    }


def _record_evidence(
    team: TeamStore, candidate: dict[str, Any], task_id: str | None, *, dry_run: bool
) -> tuple[dict[str, Any] | None, str]:
    fields = _evidence_fields(candidate)
    if not fields["ref"]:
        return None, "exists"
    payload: dict[str, Any] = {
        **fields,
        "task_id": task_id,
        "repo": str(candidate.get("repo") or ""),
        "actor": "",
        "title": str(candidate.get("title") or ""),
        "attribution": "deterministic",
        "meta": dict(candidate.get("meta") or {}),
    }
    if dry_run:
        print(json.dumps({"would_record_evidence": payload}, sort_keys=True))
        return None, "created"
    return team.record_evidence(**payload)


def _task_branches(team: TeamStore) -> dict[str, set[str]]:
    """Scan evidence once per pass; inference-created cards persist branch linkage here."""
    rows = team._connection.execute(
        "SELECT task_id, meta_json FROM task_evidence WHERE task_id IS NOT NULL"
    ).fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        try:
            meta = json.loads(str(row["meta_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        branch = meta.get("head_branch") if isinstance(meta, dict) else None
        if branch:
            found.setdefault(str(row["task_id"]), set()).add(str(branch))
    return found


def _match_task(
    candidate: dict[str, Any],
    tasks: list[dict[str, Any]],
    branches: dict[str, set[str]] | None = None,
) -> str | None:
    branches = branches or {}
    prepared = []
    for task in tasks:
        copied = dict(task)
        org = dict(copied.get("org") or {})
        org["branches"] = sorted(
            set(org.get("branches") or []) | branches.get(str(task["id"]), set())
        )
        copied["org"] = org
        copied["branches"] = org["branches"]
        prepared.append(copied)
    task_id, _ = attribution.match_task_detailed(candidate, prepared)
    if task_id is not None:
        return task_id
    key = _title_key(candidate.get("title"))
    matches = [str(task["id"]) for task in prepared if key and _title_key(task.get("title")) == key]
    return matches[0] if len(matches) == 1 else None


def _claim_then_progress(
    collab: CollabStore, task: dict[str, Any], owner: str, *, created: bool
) -> tuple[bool, bool]:
    if task.get("status") == BoardTaskStatus.OPEN.value:
        kwargs: dict[str, Any] = {"actor": "inference"}
        if created and "owner_employee_id" in inspect.signature(collab.claim_task).parameters:
            kwargs["owner_employee_id"] = owner
        if not collab.claim_task(
            str(task["id"]), f"inference:{owner}", int(task.get("claim_version") or 0), **kwargs
        ):
            return False, True
        task = collab.get_board_task(str(task["id"])) or task
    if task.get("status") == BoardTaskStatus.CLAIMED.value:
        return collab.update_board_task(
            str(task["id"]),
            {"status": BoardTaskStatus.IN_PROGRESS.value},
            expect_status=BoardTaskStatus.CLAIMED.value,
            actor="inference",
        ), False
    return False, False


def _advance(
    collab: CollabStore,
    team: TeamStore,
    task: dict[str, Any],
    owner: str,
    candidate: dict[str, Any],
    *,
    created: bool,
    branches: dict[str, set[str]],
    dry_run: bool,
) -> tuple[int, int, int]:
    if (
        task.get("owner_employee_id") != owner
        or str(task.get("status")) in _TERMINAL_STATUSES
        or dry_run
    ):
        return 0, 0, 0
    status, kind = str(task.get("status")), str(candidate.get("kind") or "")
    activity = (
        str((candidate.get("meta") or {}).get("activity") or "")
        if isinstance(candidate.get("meta"), dict)
        else ""
    )
    if status not in _ADVANCEABLE and not (
        status == BoardTaskStatus.AWAITING_APPROVAL.value and kind == "pr" and activity == "merged"
    ):
        return 0, 0, 0
    if kind == "pr" and activity == "opened" and status in _ADVANCEABLE:
        return (
            int(
                collab.update_board_task(
                    str(task["id"]),
                    {"status": BoardTaskStatus.AWAITING_APPROVAL.value},
                    expect_status=status,
                    actor="inference",
                )
            ),
            0,
            0,
        )
    if kind == "pr" and activity == "merged":
        branch = _activity_branch(candidate)
        # Awaiting approval is human-gated except for the exact PR/branch already linked to it.
        if status == BoardTaskStatus.AWAITING_APPROVAL.value and (
            not branch or branch not in branches.get(str(task["id"]), set())
        ):
            return 0, 0, 0
        changed = collab.update_board_task(
            str(task["id"]),
            {"status": BoardTaskStatus.DONE.value},
            expect_status=status,
            actor="inference",
        )
        if not changed:
            return 0, 0, 1
        if created or task.get("source") == "inference-github":
            return 1, 0, 0
        try:
            verified = team.verify_task(str(task["id"]), "inference")
        except ValueError:
            verified = None
        return 1, int(verified is not None), 0
    changed, lost = _claim_then_progress(collab, task, owner, created=created)
    return int(changed), 0, int(lost)


def run_inference(
    *,
    team: TeamStore,
    collab: CollabStore,
    activities: Iterable[dict[str, Any]],
    github_map: dict[str, str] | None = None,
    default_goal_id: str | None = None,
    dry_run: bool = False,
    creation_cap: int = _DEFAULT_CREATION_CAP,
) -> InferenceSummary:
    # Explicit maps are useful to callers/tests, but aliases (especially git
    # author emails) still come from the one shared config source.
    mapping = github_map if github_map is not None else load_github_map()
    map_loaded = bool(getattr(mapping, "loaded", True))
    valid, error = (
        _validate_mapping(mapping, collab)
        if map_loaded
        else (False, getattr(mapping, "error", "map unavailable"))
    )
    if not valid:
        print(f"inference: map disabled: {error}", file=sys.stderr)
        return InferenceSummary(map_loaded=False)
    summary, tasks, branches = (
        InferenceSummary(map_loaded=True),
        collab.list_board_tasks(archived=None),
        _task_branches(team),
    )
    by_id = {str(task["id"]): task for task in tasks}
    for candidate in activities:
        summary = summary.plus(seen=summary.seen + 1)
        try:
            login = _candidate_login(candidate, mapping)
            if login is None:
                summary = summary.plus(ignored_unmapped=summary.ignored_unmapped + 1)
                continue
            owner = mapping.get(login) or ingest.ACTOR_EMPLOYEE_MAP[login]
            evidence, _ = _record_evidence(team, candidate, None, dry_run=dry_run)
            if evidence is not None and team.is_manual(str(evidence["id"])):
                summary = summary.plus(manual_untouched=summary.manual_untouched + 1)
                continue
            task_id = (
                str(evidence["task_id"])
                if evidence is not None and evidence.get("task_id")
                else _match_task(candidate, tasks, branches)
            )
            created = False
            if task_id is None:
                # Commits have no trustworthy per-commit branch identity. Do
                # not mint speculative cards from subjects; the PR head branch
                # is the durable one-card-per-branch creation key.
                if candidate.get("kind") != "pr" or not _activity_branch(candidate):
                    continue
                if summary.created_cards >= creation_cap:
                    summary = summary.plus(creation_cap_hit=summary.creation_cap_hit + 1)
                    continue
                if dry_run:
                    print(
                        json.dumps(
                            {
                                "would_create_card": {
                                    "owner_employee_id": owner,
                                    "title": str(
                                        candidate.get("title") or _activity_branch(candidate)
                                    ),
                                }
                            },
                            sort_keys=True,
                        )
                    )
                    continue
                ref_number = _next_ref(tasks)
                while True:
                    card = BoardTask(
                        title=str(
                            candidate.get("title")
                            or _activity_branch(candidate)
                            or "GitHub activity"
                        ),
                        owner_employee_id=owner,
                        goal_id=_default_goal_id(collab, default_goal_id),
                        ref=f"GH-{ref_number}",
                        size="S",
                        acceptance_criteria=_acceptance_criteria(candidate),
                        source="inference-github",
                    )
                    try:
                        collab.create_board_task(card, actor="inference")
                        task_id, created = card.id, True
                        task = collab.get_board_task(card.id)
                        assert task is not None
                        tasks.append(task)
                        by_id[card.id] = task
                        break
                    except ValueError as exc:
                        if str(exc) != "ref_conflict":
                            raise
                        ref_number += 1
            task = by_id.get(str(task_id)) or collab.get_board_task(str(task_id))
            if task is None:
                continue
            evidence, outcome = _record_evidence(team, candidate, str(task_id), dry_run=dry_run)
            if created:
                summary = summary.plus(created_cards=summary.created_cards + 1)
            if outcome in {"created", "attached", "regraded"}:
                summary = summary.plus(attached_evidence=summary.attached_evidence + 1)
            # A foreign card may receive deterministic evidence, but is otherwise read-only.
            changed, verified, lost = _advance(
                collab,
                team,
                task,
                owner,
                candidate,
                created=created,
                branches=branches,
                dry_run=dry_run,
            )
            summary = summary.plus(
                status_changes=summary.status_changes + changed,
                verified=summary.verified + verified,
                lost_cas=summary.lost_cas + lost,
            )
            if _activity_branch(candidate):
                branches.setdefault(str(task_id), set()).add(_activity_branch(candidate))
        except Exception as exc:
            print(
                f"team-os sweep: source inference candidate {candidate.get('kind', '?')}:{candidate.get('ref', '?')} failed: {exc}",
                file=sys.stderr,
            )
            summary = summary.plus(failed_candidates=summary.failed_candidates + 1)
    if summary.creation_cap_hit:
        print(
            f"inference: creation cap {creation_cap} reached; skipped {summary.creation_cap_hit} candidates",
            file=sys.stderr,
        )
    return summary


def _default_since() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", default=_default_since())
    parser.add_argument("--until")
    parser.add_argument("--repo-grok", default="/Users/youruser/OmniAgentOS")
    parser.add_argument("--repo-initech", default="/Users/youruser/Repos/initech-product")
    parser.add_argument("--default-goal-id")
    args = parser.parse_args(argv)
    collab = CollabStore(args.db)
    print(
        run_inference(
            team=TeamStore(collab._store),
            collab=collab,
            activities=collect_activity(
                repo_grok=args.repo_grok,
                repo_initech=args.repo_initech,
                since=args.since,
                until=args.until,
            ),
            default_goal_id=args.default_goal_id,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
