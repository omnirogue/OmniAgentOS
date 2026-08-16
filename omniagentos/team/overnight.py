"""Overnight work: numbered 16:00 suggestions → one-reply reservations → 22:00 launches.

The operator's protocol, same as repairs: the 16:00 pulse numbers each
suggestion from the SHARED decision allocator (:mod:`decisions` — one number
space, so a reply can never decide the wrong kind), a `N yes` reply reserves
it, and the 22:00 runner launches one bounded Claude session per reservation.

Launch containment, in order of importance:

* **Confirm-first.** Nothing runs without a numbered yes from a roster human.
* **Branch isolation.** Each session works in its own git worktree on
  ``overnight/<ref>-<MMDD>`` off origin/main. The serving checkout and main
  are never touched; the session's brief forbids merging.
* **Bounded.** ``gtimeout`` caps each session (default 4h); sessions run
  detached with their own log under ``var/log/overnight/``.
* **Announced.** The runner posts one channel line per launch, so overnight
  compute is never invisible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from omniagentos.team import decisions
from omniagentos.team.notify import DEFAULT_CHANNEL, SlackNotifier, load_slack_env
from omniagentos.team.session_collector import _write_atomic
from omniagentos.team.session_tracker import _iso, _utcnow

RESERVATIONS_FILENAME = "team-overnight-queue.json"
CLAUDE_BIN = "/Users/youruser/.local/bin/claude"
GTIMEOUT_BIN = "/opt/homebrew/bin/gtimeout"
SESSION_CAP_SECONDS = 4 * 3600
WORKTREE_ROOT = Path("/Users/youruser/.overnight-wt")


def _queue_path(repo_root: Path) -> Path:
    from omniagentos.runtime_paths import resolve_var_root

    try:
        return Path(resolve_var_root()) / RESERVATIONS_FILENAME
    except Exception:  # noqa: BLE001
        return repo_root / "var" / RESERVATIONS_FILENAME


def load_reservations(repo_root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(_queue_path(repo_root).read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except (OSError, ValueError):
        return []


def save_reservations(repo_root: Path, reservations: list[dict[str, Any]]) -> None:
    _write_atomic(_queue_path(repo_root), reservations)


def register_suggestions(
    repo_root: Path, suggestions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Number each 16:00 suggestion from the shared allocator (deduped daily).

    ``suggestions`` items carry employee_id, card_id, ref, title. Returns the
    pending overnight decisions (new + still-open) for rendering.

    This is a REGISTRAR on the shared decision namespace, so the whole
    load->allocate->save cycle runs inside :func:`decisions._state_lock` — the
    contract that function's docstring states for every next_number allocation.
    Unlocked, this raced ``register_repair_proposals`` and ``process_replies``
    (both already locked) from a different launchd job: both sides read the
    same stale ``next_number``, issued the same number, and the later save
    dropped the earlier registration. That is not merely a lost row —
    ``render_numbered`` invites the operator to reply ``N yes`` against this
    number, so a collision points that reply at a decision they were never
    shown.
    """
    with decisions._state_lock(repo_root):
        state = decisions.load_state(repo_root)
        decisions._expire(state)
        today = _iso(_utcnow())[:10]
        for suggestion in suggestions:
            dedup_key = f"overnight:{suggestion.get('card_id')}:{today}"
            if decisions._recent_same_kind(state, dedup_key):
                continue
            number = int(state["next_number"])
            state["next_number"] = number + 1
            state["decisions"].append(
                {
                    "number": number,
                    "kind": "overnight",
                    "dedup_key": dedup_key,
                    "title": (
                        f"Overnight session for {suggestion.get('ref') or suggestion.get('card_id')}"
                        f" — {str(suggestion.get('title') or '')[:80]}"
                        f" ({suggestion.get('employee_id')})"
                    ),
                    "payload": dict(suggestion),
                    "status": "pending",
                    "proposed_at": _iso(_utcnow()),
                    "decided_by": None,
                    "decided_at": None,
                    "result": None,
                }
            )
        decisions.save_state(repo_root, state)
        return [
            d
            for d in state["decisions"]
            if d.get("status") == "pending" and d.get("kind") == "overnight"
        ]


def render_numbered(pending: list[dict[str, Any]]) -> list[str]:
    return [
        f"overnight {d['number']}. {d['title']} — reply `{d['number']} yes` to run tonight"
        for d in pending
    ]


def reserve_approved(repo_root: Path) -> int:
    """Move approved overnight decisions into the launch queue (idempotent).

    The approved->executed flip is a STATUS FLIP on the shared namespace, so it
    holds :func:`decisions._state_lock` across load->modify->save like every
    other mutator. Unlocked, a concurrent ``process_replies`` pass could write
    its own copy of the state after this one read it, resurrecting a decision
    already reserved here — and the ``launched: False`` reservation would then
    be created a second time on the next tick.

    Safe to acquire here: ``decisions.main()`` calls this only AFTER
    ``process_replies`` has returned and released, so the blocking ``LOCK_EX``
    is never taken re-entrantly (which, being per-file-description, would hang
    rather than fail).
    """
    with decisions._state_lock(repo_root):
        state = decisions.load_state(repo_root)
        reservations = load_reservations(repo_root)
        queued = {r.get("number") for r in reservations}
        added = 0
        for decision in state["decisions"]:
            if (
                decision.get("kind") == "overnight"
                and decision.get("status") == "approved"
                and decision.get("number") not in queued
            ):
                reservations.append(
                    {
                        "number": decision["number"],
                        "reserved_at": _iso(_utcnow()),
                        "launched": False,
                        **{
                            k: decision["payload"].get(k)
                            for k in ("employee_id", "card_id", "ref", "title")
                        },
                    }
                )
                decision["status"] = "executed"
                decision["result"] = "reserved for tonight's 22:00 run"
                added += 1
        if added:
            save_reservations(repo_root, reservations)
            decisions.save_state(repo_root, state)
        return added


def _session_brief(reservation: dict[str, Any], branch: str) -> str:
    ref = reservation.get("ref") or reservation.get("card_id")
    return (
        f"Overnight work session, operator-approved (decision {reservation['number']}). "
        f"Work ONLY the board card {ref}: {reservation.get('title')}. "
        f"You are in a dedicated git worktree on branch {branch}. Rules: "
        "commit your work to THIS branch with clear messages and push it with "
        "`git push -u origin HEAD`; NEVER merge, never touch main, never modify "
        "CI/gate/permission configs; run the tests that cover what you change; "
        "carry `refs " + str(ref) + "` in every commit message so the board "
        "attributes the work; if genuinely blocked, write BLOCKED.md explaining "
        "why and stop. End by writing OVERNIGHT-SUMMARY.md (what you did, test "
        "results, what remains)."
    )


def launch_reservations(repo_root: Path, *, notifier: Any, dry_run: bool = False) -> int:
    """22:00 entry: one detached, capped session per unlaunched reservation."""
    reservations = load_reservations(repo_root)
    launched = 0
    stamp = _iso(_utcnow())[:10].replace("-", "")[4:]
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    log_dir = repo_root / "var" / "log" / "overnight"
    log_dir.mkdir(parents=True, exist_ok=True)
    for reservation in reservations:
        if reservation.get("launched"):
            continue
        ref = str(reservation.get("ref") or reservation.get("card_id") or "card")
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", ref).lower()
        # The decision number makes same-day re-approvals of one ref collide-free.
        tag = f"{slug}-{stamp}-{reservation.get('number', 0)}"
        branch = f"overnight/{tag}"
        worktree = WORKTREE_ROOT / tag
        if dry_run:
            print(json.dumps({"would_launch": {"branch": branch, "ref": ref}}))
            continue
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    "origin/main",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            # Mechanical containment backstop, not just the brief's rules: the
            # session's own permission layer refuses main pushes, PR merges,
            # and gate/CI-config edits inside this worktree.
            settings_dir = worktree / ".claude"
            settings_dir.mkdir(parents=True, exist_ok=True)
            # Deny rules use the DOCUMENTED prefix grammar (Bash(cmd:*));
            # "push to main" is not expressible as a prefix, so a PreToolUse
            # hook blocks any Bash command that names main/master in a push.
            guard = (
                'python3 -c "import json,re,sys;'
                "d=json.load(sys.stdin);"
                "c=d.get('tool_input',{}).get('command','');"
                "sys.exit(2 if re.search(r'push[^|;&]*\\b(main|master)\\b', c) else 0)\""
            )
            _write_atomic(
                settings_dir / "settings.json",
                {
                    "permissions": {
                        "deny": [
                            "Bash(gh pr merge:*)",
                            "Bash(git merge:*)",
                            "Edit(.github/**)",
                            "Edit(configs/launchd/**)",
                            "Write(.github/**)",
                            "Write(configs/launchd/**)",
                        ]
                    },
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": guard}],
                            }
                        ]
                    },
                },
            )
            log_path = log_dir / f"{tag}.log"
            command = (
                f"cd {shlex.quote(str(worktree))} && "
                f"{shlex.quote(GTIMEOUT_BIN)} {SESSION_CAP_SECONDS} "
                f"{shlex.quote(CLAUDE_BIN)} -p {shlex.quote(_session_brief(reservation, branch))} "
                f"--permission-mode acceptEdits "
                f">> {shlex.quote(str(log_path))} 2>&1"
            )
            subprocess.Popen(
                ["/bin/bash", "-c", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            reservation["launched"] = True
            reservation["launched_at"] = _iso(_utcnow())
            reservation["branch"] = branch
            launched += 1
            notifier.post_channel(
                f"🌙 overnight {reservation['number']}. launched for {ref} on "
                f"`{branch}` (capped 4h; log var/log/overnight/{tag}.log)"
            )
        except (subprocess.SubprocessError, OSError) as exc:
            reservation["launched"] = True  # never retry-storm a broken launch
            reservation["error"] = str(exc)[:300]
            notifier.post_channel(
                f"🌙 overnight {reservation['number']}. LAUNCH FAILED for {ref}: {str(exc)[:140]}"
            )
    # Merge-on-save: a reservation approved WHILE this pass ran (the 300s
    # decisions processor can phase-align with 22:00) must survive — a blind
    # whole-file rewrite would silently erase an operator-confirmed action.
    latest = load_reservations(repo_root)
    ours = {r.get("number"): r for r in reservations}
    merged = [ours.get(r.get("number"), r) for r in latest]
    known = {r.get("number") for r in latest}
    merged.extend(r for n, r in ours.items() if n not in known)
    save_reservations(repo_root, merged)
    return launched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=str(decisions.REPO_ROOT))
    parser.add_argument(
        "--reserve",
        action="store_true",
        help="move approved overnight decisions into tonight's queue",
    )
    parser.add_argument(
        "--launch", action="store_true", help="launch tonight's reservations (22:00 job)"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    load_slack_env()
    channel = (
        os.environ.get("OMNI_TEAM_PULSE_CHANNEL")
        or os.environ.get("OMNI_TEAM_REPORT_CHANNEL")
        or DEFAULT_CHANNEL
    )
    notifier = SlackNotifier(os.environ.get("SLACK_BOT_TOKEN", ""), channel=channel)
    if args.reserve:
        print(json.dumps({"reserved": reserve_approved(repo_root)}))
        return 0
    if args.launch:
        count = launch_reservations(repo_root, notifier=notifier, dry_run=args.dry_run)
        print(json.dumps({"launched": count}))
        return 0
    parser.error("pass --reserve or --launch")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
