"""One-shot evidence attribution sweep for the Team Work OS."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.team import attribution, inference, ingest
from omniagentos.team.store import TeamStore

try:
    from pipeline.bridge.notify import push_alert as _push_alert
except ImportError:  # pragma: no cover - optional estate integration
    push_alert = None
else:
    # Team OS has a one-message alert contract. The optional estate bridge has
    # varied across deployments, so keep its callable shape outside this module.
    push_alert = cast(Callable[[str], object], _push_alert)

_EPOCH = "1970-01-01T00:00:00Z"
_OPEN_STATUSES = ("open", "in_progress", "claimed", "blocked", "awaiting_approval")
_INFERENCE_CURSOR_SUFFIX = ".inference"
# OPERATOR PRIVACY POLICY (the operator, 2026-08-11): inference NEVER reads back into
# past days. A fresh install looks back ONE HOUR; a stale cursor is clamped to
# the start of the current UTC day at the earliest. History is not backfilled —
# cards are minted only for work happening now, not for what happened before.
_INFERENCE_FRESH_AGE = timedelta(hours=1)

_actor_map_alert_emitted = False


def _alert_actor_map_unavailable_once() -> None:
    """A missing/unreadable identity map silently yields {} for the ordinary
    sweep's actor->employee attribution; make that loud exactly once per
    process instead of letting every candidate go unattributed silently."""
    global _actor_map_alert_emitted
    if _actor_map_alert_emitted or ingest.ACTOR_EMPLOYEE_MAP.loaded:
        return
    _actor_map_alert_emitted = True
    message = (
        "team-os sweep: actor/employee identity map unavailable "
        f"({ingest.ACTOR_EMPLOYEE_MAP.error}); ordinary sweep attribution is disabled"
    )
    if push_alert is not None:
        try:
            push_alert(message)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _list_open_tasks(store: TeamStore, since_iso: str, until_iso: str) -> list[dict]:
    """Read actionable cards plus cards completed inside this sweep window.

    A commit or PR can be collected immediately after its card moved to done.
    The status-change event is the durable completion time; using it keeps an
    unrelated later edit from making an old done card attribution-eligible.
    """

    placeholders = ", ".join("?" for _ in _OPEN_STATUSES)
    with store._store._lock:
        rows = store._connection.execute(
            "SELECT id, title, ref, status, owner_employee_id, result_ref, run_id, "
            "created_at, updated_at, swarm_json, org_json FROM board_tasks "
            f"WHERE status IN ({placeholders}) OR (status = 'done' AND EXISTS ("
            "SELECT 1 FROM task_events e WHERE e.task_id = board_tasks.id "
            "AND e.event IN ('status_change', 'create') AND e.to_status = 'done' "
            "AND e.created_at >= ? AND e.created_at <= ?)) "
            "ORDER BY created_at ASC, id ASC",
            (*_OPEN_STATUSES, since_iso, until_iso),
        ).fetchall()
    tasks: list[dict] = []
    for row in rows:
        swarm = _json_object(row["swarm_json"])
        org = _json_object(row["org_json"])
        owned_paths = list(
            dict.fromkeys(
                _string_list(swarm.get("owned_paths")) + _string_list(org.get("owned_paths"))
            )
        )
        branches = list(
            dict.fromkeys(_string_list(swarm.get("branches")) + _string_list(org.get("branches")))
        )
        tasks.append(
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "ref": None if row["ref"] is None else str(row["ref"]),
                "status": str(row["status"]),
                "owner_employee_id": (
                    None if row["owner_employee_id"] is None else str(row["owner_employee_id"])
                ),
                "result_ref": None if row["result_ref"] is None else str(row["result_ref"]),
                "run_id": str(row["run_id"]) if row["run_id"] is not None else org.get("run_id"),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "owned_paths": owned_paths,
                "branches": branches,
            }
        )
    return tasks


def _github_slug(repo_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ingest.IngestUnavailable(
            f"git remote probe failed for configured repo {repo_path}: {exc}"
        ) from exc
    if result.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", result.stdout.strip())
    return match.group(1) if match else None


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _alert_unavailable(source_name: str, exc: ingest.IngestUnavailable) -> None:
    message = f"team-os sweep: source {source_name} unavailable: {exc}"
    if push_alert is not None:
        try:
            push_alert(message)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _alert_candidate_failed(source_name: str, candidate: object, exc: Exception) -> None:
    if isinstance(candidate, dict):
        identity = f"{candidate.get('kind', '?')}:{candidate.get('ref', '?')}"
    else:
        identity = str(candidate)
    message = f"team-os sweep: source {source_name} candidate {identity} failed: {exc}"
    if push_alert is not None:
        try:
            push_alert(message)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _unavailable_source(message: str) -> Callable[[str], Iterator[dict]]:
    def factory(_source_since: str) -> Iterator[dict]:
        raise ingest.IngestUnavailable(message)

    return factory


def _write_candidate(
    store: TeamStore,
    candidate: dict,
    open_tasks: list[dict],
    *,
    dry_run: bool,
) -> None:
    task_id = candidate.get("task_id")
    matcher: str | None = None
    if task_id is None:
        task_id, matcher = attribution.match_task_detailed(
            candidate,
            open_tasks,
            actor_employee_map=ingest.ACTOR_EMPLOYEE_MAP,
        )
    meta = dict(candidate.get("meta") or {})
    if matcher is not None:
        meta["matcher"] = matcher
    # Keep an open PR's stable identity for a later merge regrade, but make its
    # gate non-mechanical even if a caller bypassed ingest.iter_prs.
    open_pr = candidate.get("kind") == "pr" and meta.get("activity") == "opened"
    fields = {
        "kind": candidate["kind"],
        "ref": candidate["ref"],
        "task_id": task_id,
        "repo": candidate.get("repo", ""),
        "actor": candidate.get("actor", ""),
        "title": candidate.get("title", ""),
        "attribution": "deterministic",
        "quality_gate": "rejected" if open_pr else candidate.get("quality_gate", "pass"),
        "meta": meta,
    }
    if dry_run:
        print(json.dumps({"would_add_evidence": fields}, sort_keys=True))
    else:
        store.add_evidence(**fields)


def run_sweep(
    *,
    store: TeamStore,
    repo_grok: str,
    repo_initech: str,
    since: str | None,
    until: str | None,
    cursor_path: str,
    dry_run: bool = False,
    skip_prs: bool = False,
    now: str | None = None,
    collab_store: CollabStore | None = None,
) -> int:
    """Collect, attribute, and persist one bounded pass over all sources."""

    _alert_actor_map_unavailable_once()

    # An explicit upper bound is a historical replay, not a continuation of
    # the daemon's live stream. It neither consumes nor mutates live cursors.
    cursorless = until is not None
    loaded_cursors = {} if cursorless else ingest.load_cursors(cursor_path)
    cursors = dict(loaded_cursors)
    explicit_window = since is not None or until is not None
    advance_to = until or now or utc_now_iso()
    task_window_since = (
        since or _EPOCH
        if explicit_window
        else min(cursors.values(), key=_parse_iso, default=_EPOCH)
    )
    open_tasks = _list_open_tasks(store, task_window_since, advance_to)

    sources: list[tuple[str, Callable[[str], Iterator[dict]]]] = [
        (
            "commits:grok",
            lambda source_since: ingest.iter_commits(
                repo_grok, source_since, until, repo_label=repo_grok
            ),
        ),
        (
            "commits:initech",
            lambda source_since: ingest.iter_commits(
                repo_initech, source_since, until, repo_label=repo_initech
            ),
        ),
        ("sessions", lambda source_since: ingest.iter_sessions(store, source_since)),
    ]
    if not skip_prs:
        pr_specs: list[tuple[str, str | None, str | None]] = []
        for label, repo_path in (("grok", repo_grok), ("initech", repo_initech)):
            try:
                slug = _github_slug(repo_path)
            except ingest.IngestUnavailable as exc:
                pr_specs.append((label, None, str(exc)))
            else:
                error = (
                    None
                    if slug is not None
                    else f"could not resolve GitHub slug for configured repo {repo_path}"
                )
                pr_specs.append((label, slug, error))

        preflight_error: str | None = None
        if any(slug is not None for _label, slug, _error in pr_specs):
            try:
                ingest.preflight_github()
            except ingest.IngestUnavailable as exc:
                preflight_error = str(exc)

        for label, slug, slug_error in pr_specs:
            source_name = f"prs:{label}"
            error = slug_error or preflight_error
            if error is not None:
                sources.append((source_name, _unavailable_source(error)))
                continue
            assert slug is not None

            def pr_source(source_since: str, *, repo_slug: str = slug) -> Iterator[dict]:
                return ingest.iter_prs(repo_slug, source_since, preflight=False)

            sources.append((source_name, pr_source))

    sources_attempted = 0
    sources_ok = 0
    until_time = _parse_iso(until) if until is not None else None
    for source_name, factory in sources:
        sources_attempted += 1
        source_since = (since or _EPOCH) if explicit_window else cursors.get(source_name, _EPOCH)
        candidate_failed = False
        failed_floor = None
        try:
            for candidate in factory(source_since):
                try:
                    if until_time is not None:
                        occurred_at = candidate.get("occurred_at")
                        if occurred_at and _parse_iso(str(occurred_at)) > until_time:
                            continue
                    _write_candidate(store, candidate, open_tasks, dry_run=dry_run)
                except Exception as exc:
                    candidate_failed = True
                    occurred = None
                    if isinstance(candidate, dict):
                        try:
                            occurred = _parse_iso(str(candidate.get("occurred_at") or ""))
                        except ValueError:
                            occurred = None
                    if occurred is not None and (failed_floor is None or occurred < failed_floor):
                        failed_floor = occurred
                    _alert_candidate_failed(source_name, candidate, exc)
        except ingest.IngestUnavailable as exc:
            _alert_unavailable(source_name, exc)
            continue
        if not candidate_failed:
            sources_ok += 1
        if not cursorless:
            advance_parsed = _parse_iso(advance_to)
            if (
                failed_floor is not None
                and advance_parsed is not None
                and failed_floor < advance_parsed
            ):
                # Rewind to the oldest failed candidate so the next sweep re-reads
                # it (writes are idempotent, so re-covering the window is free) —
                # advancing past it would freeze the artifact as never-collected.
                # A failed candidate with no parseable timestamp cannot be re-found;
                # the alert above is its only receipt.
                cursors[source_name] = failed_floor.isoformat()
            else:
                cursors[source_name] = advance_to

    if not dry_run and not cursorless:
        ingest.save_cursors(cursor_path, cursors)
    # Keep inference in this process/cadence rather than scheduling a second
    # collector.  It deliberately reuses the public ingest readers: evidence
    # keys make the re-sighting idempotent, and open PRs become non-mechanical
    # notes until their merged PR evidence arrives.
    inference_enabled = os.getenv("OMNIAGENTOS_TEAM_INFERENCE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if collab_store is not None and not skip_prs and inference_enabled:
        try:
            inference_cursor_path = f"{cursor_path}{_INFERENCE_CURSOR_SUFFIX}"
            inference_cursors = {} if cursorless else ingest.load_cursors(inference_cursor_path)
            inferred_until = advance_to
            inferred_now = _parse_iso(inferred_until)
            inferred_since = since or inference_cursors.get("inference", "")
            day_start = inferred_now.replace(hour=0, minute=0, second=0, microsecond=0)
            if not inferred_since:
                floor = max(inferred_now - _INFERENCE_FRESH_AGE, day_start)
            else:
                # A cursor may lag (sweep outage) but never crosses midnight
                # backwards: same-day at most, per the operator privacy policy.
                floor = max(_parse_iso(inferred_since), day_start)
            inferred_since = floor.isoformat().replace("+00:00", "Z")
            inferred = inference.run_inference(
                team=store,
                collab=collab_store,
                activities=inference.collect_activity(
                    repo_grok=repo_grok,
                    repo_initech=repo_initech,
                    since=inferred_since,
                    until=until,
                ),
                dry_run=dry_run,
            )
            if not inferred.map_loaded:
                _alert_candidate_failed(
                    "inference", "github-map", RuntimeError("identity map unavailable")
                )
            # A pass that never loaded the identity map, or that failed one or
            # more candidates, did not actually cover [inferred_since,
            # inferred_until) -- advancing the cursor anyway would freeze that
            # window as permanently un-inferred. Only a fully-processed pass
            # may move the high-water mark; an unprocessed pass writes back its
            # own starting point so the next pass re-offers the same window.
            processed_window = inferred.map_loaded and inferred.failed_candidates == 0
            if not dry_run and not cursorless:
                ingest.save_cursors(
                    inference_cursor_path,
                    {"inference": inferred_until if processed_window else inferred_since},
                )
            if dry_run:
                print(json.dumps({"would_infer": inferred.__dict__}, sort_keys=True))
        except ingest.IngestUnavailable as exc:
            _alert_unavailable("inference", exc)
        except Exception as exc:
            # Inference is auxiliary. Its failures must never take down the
            # ordinary evidence collector or its cursor advancement.
            _alert_candidate_failed("inference", "pass", exc)
    if sources_ok == 0 and sources_attempted > 0:
        return 2
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run once (the default behavior).")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--repo-grok", default="/Users/youruser/OmniAgentOS")
    parser.add_argument("--repo-initech", default="/Users/youruser/Repos/initech-product")
    parser.add_argument("--db", required=True)
    parser.add_argument("--cursor-path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-prs", action="store_true")
    args = parser.parse_args(argv)
    cursor_path = args.cursor_path or f"{args.db}.team-sweep-cursors.json"
    collab_store = CollabStore(args.db)
    code = run_sweep(
        store=TeamStore(collab_store._store),
        repo_grok=args.repo_grok,
        repo_initech=args.repo_initech,
        since=args.since,
        until=args.until,
        cursor_path=cursor_path,
        dry_run=args.dry_run,
        skip_prs=args.skip_prs,
        collab_store=collab_store,
    )
    raise SystemExit(code)


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m``
    main()
