"""Mark board cards in-progress from LIVE session activity.

The operator's contract for the board: **sessions say what is in progress
right now; GitHub says what is completed.** The GitHub half is
:mod:`omniagentos.team.inference`; this module is the sessions half. It reads
the collector drop-files (``var/team-sessions/<employee>.json``), matches each
person's ACTIVE sessions against that person's OWN non-terminal cards, and
advances ``open`` → in-progress through the normal claim/update path.

Deliberately narrow, in the same spirit as the inference rulings:

* **Owner-scoped by construction.** A session report for ``emp_x`` is only
  ever matched against cards owned by ``emp_x`` (or claimed by them). No
  cross-owner move is representable, so no session can steal or advance
  someone else's card.
* **Conservative matching.** A session matches a card when the card's REF
  appears as a delimited token in the session description, or the card's
  normalized title is contained in the normalized description. No fuzzy
  scoring — an unmatched session is simply not board signal (the tracker
  still shows it).
* **Forward-only.** Only ``open`` and ``claimed`` cards advance (to claimed /
  in_progress). ``awaiting_approval``, ``blocked``, ``pending``, ``done`` are
  never touched — completing and un-blocking are other subsystems' jobs.
* **Evidence, not testimony.** Each advance attaches idempotent ``session``
  evidence keyed on the session id, so re-running a pass changes nothing.
* **Same-day only.** Stale drop-files (older than the tracker's freshness
  window) are ignored, consistent with the operator privacy policy: liveness
  reflects NOW, never history.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.inference import _title_key
from omniagentos.team.session_tracker import (
    DROP_FRESH_SECONDS,
    read_person_reports,
)
from omniagentos.team.store import TeamStore

ACTOR = "session-liveness"
_ADVANCEABLE = frozenset({BoardTaskStatus.OPEN.value, BoardTaskStatus.CLAIMED.value})


def _ref_tokens(text: str) -> set[str]:
    return {token.upper() for token in re.findall(r"\b[A-Za-z]+-?\d+\b", text)}


# A title must be substantial before containment counts as a match: a card
# titled "fix" or "api" would substring-match half of every description.
_MIN_TITLE_KEY = 12


def _match_card(session: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    description = str(session.get("description") or "")
    tokens = _ref_tokens(description)
    normalized = _title_key(description)
    for card in cards:
        ref = str(card.get("ref") or "").upper()
        if ref and ref in tokens:
            return card
        title_key = _title_key(card.get("title"))
        if len(title_key) >= _MIN_TITLE_KEY and " " in title_key and title_key in normalized:
            return card
    return None


def run_liveness(
    *,
    team: TeamStore,
    collab: CollabStore,
    reports: dict[str, dict[str, Any]],
    dry_run: bool = False,
) -> dict[str, int]:
    """One pass: advance owner-matched cards for every fresh report."""
    stats = {"matched": 0, "advanced": 0, "evidence": 0, "skipped_stale": 0}
    all_cards = collab.list_board_tasks(archived=0)
    for employee_id, report in reports.items():
        if report.get("_age_seconds", 0.0) > DROP_FRESH_SECONDS:
            stats["skipped_stale"] += 1
            continue
        own_cards = [
            card
            for card in all_cards
            if str(card.get("owner_employee_id") or "") == employee_id
            and str(card.get("status")) in _ADVANCEABLE
        ]
        if not own_cards:
            continue
        for session in report.get("sessions", []):
            if not isinstance(session, dict) or not session.get("active"):
                continue
            card = _match_card(session, own_cards)
            if card is None:
                continue
            stats["matched"] += 1
            card_id = str(card["id"])
            session_ref = f"session:{session.get('id') or 'unknown'}"
            if dry_run:
                print(
                    json.dumps(
                        {
                            "would_advance": {
                                "card": card_id,
                                "ref": card.get("ref"),
                                "owner": employee_id,
                                "from": card.get("status"),
                                "session": session_ref,
                            }
                        },
                        sort_keys=True,
                    )
                )
                continue
            status = str(card.get("status"))
            if status == BoardTaskStatus.OPEN.value:
                # CAS claim first. claim_task signals a lost race by returning
                # False (never by raising); on a loss we re-read the card and
                # only continue if the winner left it in an advanceable state.
                won = collab.claim_task(
                    card_id,
                    f"{ACTOR}:{employee_id}",
                    int(card.get("claim_version") or 0),
                    actor=ACTOR,
                    owner_employee_id=employee_id,
                )
                if won:
                    status = BoardTaskStatus.CLAIMED.value
                else:
                    refreshed = collab.get_board_task(card_id)
                    status = str(refreshed.get("status")) if refreshed else ""
                    if status not in _ADVANCEABLE:
                        print(
                            f"liveness: {card_id}: lost claim race, now {status!r}; skipping",
                            file=sys.stderr,
                        )
                        continue
            advanced = False
            if status == BoardTaskStatus.CLAIMED.value:
                try:
                    collab.update_board_task(
                        card_id,
                        {"status": BoardTaskStatus.IN_PROGRESS.value},
                        actor=ACTOR,
                    )
                    advanced = True
                except ValueError as exc:
                    print(f"liveness: {card_id}: {exc}", file=sys.stderr)
            if advanced:
                # Only after the DB write succeeded — stats and the local
                # snapshot must never claim a move the store refused.
                stats["advanced"] += 1
                card["status"] = BoardTaskStatus.IN_PROGRESS.value
            team.record_evidence(
                kind="session",
                ref=session_ref,
                task_id=card_id,
                repo="",
                actor="",
                title=str(session.get("description") or "")[:140],
                attribution="deterministic",
                quality_gate="pass",
                meta={"host": report.get("host", ""), "harness": session.get("harness", "")},
            )
            stats["evidence"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("OMNIAGENTOS_DB", ""))
    parser.add_argument("--repo-root", default="/Users/youruser/OmniAgentOS")
    parser.add_argument(
        "--collect-local",
        metavar="EMPLOYEE_ID",
        help="refresh this machine's own drop-file before the pass",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    db_path = args.db or str(repo_root / "var" / "runtime" / "state.sqlite3")
    if args.collect_local:
        from omniagentos.team.session_tracker import collect_local

        collect_local(repo_root, args.collect_local)
    collab = CollabStore(db_path)
    team = TeamStore(collab._store)
    stats = run_liveness(
        team=team,
        collab=collab,
        reports=read_person_reports(repo_root),
        dry_run=args.dry_run,
    )
    print(json.dumps({"liveness": stats}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
