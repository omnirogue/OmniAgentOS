#!/usr/bin/env python3
"""Collapse duplicate/stale notification noise left by the pre-fix re-notify loops.

FOUR loops were re-notifying the SAME entity indefinitely (all fixed in code;
this only cleans up what they already wrote):

  * ``audit._critical_alerts`` re-alerted every OPEN critical reliability event
    on every 600 s watch cycle (fixed by migration 053 + ``alerted_at``).
  * ``intake._renotify_orchestration_approvals`` passed ``dedupe=False`` and ran
    on every orchestration resume tick (fixed by deduping).
  * Notifications pointing at an approval the operator ALREADY resolved stayed
    unread forever, so the badge counted work that no longer exists.
  * ``ReflectionWatchdog.file_board_alert()`` called ``create_board_task()``
    unconditionally on every invocation for a PERSISTENT condition (one stuck
    reflection run) instead of an event -- 1,709 open, character-identical
    "reflection loop broken: ..." cards, measured live (fixed by deduping the
    writer on the full condition, scoped to discipline='reflection'; see
    ``omniagentos/reflection/watchdog.py:file_board_alert``).

Passes 1-2 keep the NEWEST notification per ``(ref_type, ref_id)`` unread and
mark older siblings read. Pass 3 marks read anything targeting a resolved
approval. Nothing is ever deleted -- the history stays inspectable (board
rows move open -> cancelled, a status already in normal use, never removed).

Dry-run by default. Pass --apply to write.

Pass 4 (GENERALIZED FINGERPRINT / QUARANTINE, task-queue detox) reports, and
optionally collapses, duplicate mass across ALL open ``board_tasks`` rows --
not just the reflection watchdog's own alert cards. It groups by each row's
own structured ``Condition: <hash>`` line when present (see
``_extract_condition_key`` in ``omniagentos/reflection/watchdog.py`` -- the
identity of a CONDITION, derived from the full alert reason, never a
possibly-truncated display title), and falls back to a fingerprint over the
normalized ``discipline + title`` pair (inside the reflection watchdog's own
alert scope -- the ORIGINAL, narrower pass 4's exact grouping key) or
``discipline + title + description`` (everywhere else) -- hashed with the
SAME ``_condition_key`` helper the watchdog uses for its own condition hash,
reused rather than cloned (this repo's dominant defect class is duplicated
logic drifting apart). Outside the reflection scope the description is
load-bearing: two cards from an unrelated writer can render an identical
TITLE for genuinely different work (e.g. two "Companion task for chat: ..."
cards for two different chats) while their descriptions differ, and a
title-only key there previously cancelled one of them as a false duplicate
-- exactly how the historical 1,709-row reflection backlog this pass exists
to clean up was minted in the
first place (character-identical title AND description on every duplicate).

Dry-run (the default, and the only thing this pass ever does without
``--apply``) prints the top-20 collision groups, worst-first, for eyeball
review. Nothing is written in this mode.

``--apply`` performs the QUARANTINE: for every group with more than one open
row, the newest row is kept open (with an "Occurrences / first seen / last
seen" rollup line stamped into its description, replacing any earlier rollup
from a prior run so re-applying stays idempotent) and every older sibling is
flipped ``open -> cancelled`` through ``CollabStore.update_board_task`` (a
status already in normal use, never a delete -- restoring is a 1:1
``cancelled -> open`` PATCH per the receipt below). A machine-readable
receipt mapping every collapsed row id to its survivor is written to
``--receipt-path`` (default: ``var/reliability/collapse-alert-backlog-receipt.json``)
so the apply is auditable and the exact set of moves can be undone.

``--apply`` also backfills ``blocked_reason`` on BLOCKED rows that carry no
lane, no owner and no ``blocked_reason`` of their own -- those rows are
otherwise unroutable (no queue a person or a lane sweep can find them
through). The backfilled reason is a fixed, greppable marker string,
``unrouted: no lane/owner (detox backfill)``, distinct from any reason a
human or a writer would compose, so a later audit can always tell a detox
backfill apart from a genuine block.

    python scripts/reliability/collapse-alert-backlog.py            # report
    python scripts/reliability/collapse-alert-backlog.py --apply    # collapse
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.reflection.watchdog import _condition_key, _extract_condition_key

# One row per open critical event per watch cycle.
_STORM_PREFIX = "Critical failure: "
# An approval in one of these states is decided; a notification for it is stale.
_RESOLVED_APPROVAL_STATES = ("approved", "rejected", "expired")

# The reflection watchdog's own alert scope (see ``_generalized_fingerprint``):
# the ORIGINAL, narrower pass 4 grouped rows in exactly this scope by TITLE
# ALONE (no description check) because the historical 1,709-row backlog it
# cleans up predates the writer's ``Condition:`` field and was minted with
# description drift the title-only key still had to catch. That behaviour is
# pinned by ``tests/reflection/test_watchdog.py`` and preserved here as a
# special case of the fallback fingerprint, not reproduced by a second code
# path.
_REFLECTION_ALERT_DISCIPLINE = "reflection"
_REFLECTION_ALERT_TITLE_PREFIX = "reflection loop broken:"

# Default location for the pass-4 apply receipt (see module docstring).
DEFAULT_RECEIPT_PATH = "var/reliability/collapse-alert-backlog-receipt.json"

# Fixed, greppable marker so a later audit can always tell a detox backfill
# apart from a genuine human/writer-authored blocked_reason.
BLOCKED_BACKFILL_REASON = "unrouted: no lane/owner (detox backfill)"

# Actor recorded on every board_tasks mutation this script makes, so the
# task_events audit trail names the tool, not "system".
_ACTOR = "collapse-alert-backlog"

# Matches a rollup line this script itself stamped on a survivor's
# description, so a re-``--apply`` replaces it in place instead of growing an
# unbounded tail of stale rollups.
_ROLLUP_RE = re.compile(
    r"\n*Collapsed occurrences: \d+ \(first: \S+, last: \S+\)\s*$"
)


def _superseded_ids(rows: list[sqlite3.Row]) -> list[str]:
    """Every row but the newest for each ``(ref_type, ref_id)``.

    *rows* must already be ordered newest-first within each ref.
    """
    superseded: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["ref_type"]), str(row["ref_id"]))
        if key in seen:
            superseded.append(str(row["id"]))
        else:
            seen.add(key)
    return superseded


def _duplicates(connection: sqlite3.Connection, where: str, params: tuple) -> list[str]:
    rows = connection.execute(
        "SELECT id, ref_type, ref_id FROM notifications "
        f"WHERE {where} AND ref_id IS NOT NULL AND read_at IS NULL "
        "ORDER BY ref_type, ref_id, created_at DESC, id DESC",
        params,
    ).fetchall()
    return _superseded_ids(rows)


def _normalize_title(title: str | None) -> str:
    """Case/whitespace-fold a title for the fallback fingerprint."""
    return " ".join(str(title or "").strip().lower().split())


def _generalized_fingerprint(
    title: str | None, discipline: str | None, description: str | None
) -> str:
    """Group id for ANY open ``board_tasks`` row, across every discipline.

    Prefers the row's OWN structured ``Condition: <hash>`` line (written by
    ``ReflectionWatchdog.file_board_alert``; see ``_extract_condition_key``)
    when present -- that is the card's real dedupe identity, independent of
    its display title.

    Rows written before that field existed, or by writers that never write
    it, fall back to a fingerprint hashed (with the SAME ``_condition_key``
    helper the watchdog uses for its own condition hash, reused not cloned)
    over one of two normalized keys:

    * **In the reflection watchdog's own scope** (``discipline = 'reflection'``
      AND the title carries the ``'reflection loop broken:'`` alert prefix)
      the key is ``discipline + title`` ALONE, exactly what the ORIGINAL,
      narrower pass 4 grouped on. That scope's historical 1,709-row backlog
      predates the ``Condition:`` field and was minted with description
      drift ("old"/"new" markers, differing timestamps in free text) the
      title-only key still has to catch -- this is a regression bar pinned
      by ``tests/reflection/test_watchdog.py``, preserved here as a special
      case of this one fingerprint function rather than a second code path.
    * **Everywhere else** the key ALSO includes the description:
      ``discipline + title + description``. Outside the reflection scope, a
      title match alone is not proof of duplication -- two cards from an
      unrelated writer can render an identical TITLE for genuinely different
      work (e.g. two "Companion task for chat: ..." cards for two different
      chats) while their descriptions differ, and title-only grouping there
      previously cancelled one of them as a false duplicate (the admitted
      proposal's cleanup-scope falsifier, pinned by
      ``tests/reflection/test_watchdog.py::test_collapse_alert_backlog_apply_cancels_only_the_enumerated_reflection_set``).
    """
    condition_key = _extract_condition_key(str(description or ""))
    if condition_key:
        return f"condition:{condition_key}"
    normalized_title = _normalize_title(title)
    if discipline == _REFLECTION_ALERT_DISCIPLINE and normalized_title.startswith(
        _REFLECTION_ALERT_TITLE_PREFIX
    ):
        normalized = f"{discipline}|{normalized_title}"
    else:
        normalized = f"{discipline or ''}|{normalized_title}|{str(description or '').strip()}"
    return f"title:{_condition_key(normalized)}"


def _open_board_task_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, title, description, discipline, created_at "
        "FROM board_tasks WHERE status = 'open' "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()


def _generalized_duplicate_groups(rows: list[sqlite3.Row]) -> list[dict]:
    """Collision groups (size > 1 only), worst-first, across ALL open rows.

    *rows* must already be ordered newest-first (see
    ``_open_board_task_rows``) -- within any fingerprint group the FIRST row
    encountered is therefore always its newest member (the survivor to keep
    open) and every later one is superseded (the ones to collapse).
    """
    order: list[str] = []
    members: dict[str, list[sqlite3.Row]] = {}
    sample_titles: dict[str, str] = {}
    sample_disciplines: dict[str, str | None] = {}
    for row in rows:
        fp = _generalized_fingerprint(row["title"], row["discipline"], row["description"])
        if fp not in members:
            members[fp] = []
            order.append(fp)
            sample_titles[fp] = str(row["title"])
            sample_disciplines[fp] = row["discipline"]
        members[fp].append(row)

    groups: list[dict] = []
    for fp in order:
        group_rows = members[fp]
        if len(group_rows) < 2:
            continue
        survivor = group_rows[0]
        collapsed = group_rows[1:]
        created_ats = sorted(
            str(row["created_at"]) for row in group_rows if row["created_at"]
        )
        groups.append(
            {
                "fingerprint": fp,
                "sample_title": sample_titles[fp],
                "discipline": sample_disciplines[fp],
                "survivor_id": str(survivor["id"]),
                "collapsed_ids": [str(row["id"]) for row in collapsed],
                "occurrences": len(group_rows),
                "first_seen": created_ats[0] if created_ats else None,
                "last_seen": created_ats[-1] if created_ats else None,
            }
        )
    groups.sort(key=lambda group: group["occurrences"], reverse=True)
    return groups


def _apply_rollup(
    description: str | None, occurrences: int, first_seen: str | None, last_seen: str | None
) -> str:
    """Idempotently stamp a survivor's description with the collapse rollup.

    Re-applying against an already-stamped description REPLACES the trailing
    rollup line rather than appending a second one, so re-running ``--apply``
    after fresh duplicates land never grows an unbounded tail of stale
    rollup lines.
    """
    base = _ROLLUP_RE.sub("", description or "").rstrip()
    line = f"Collapsed occurrences: {occurrences} (first: {first_seen}, last: {last_seen})"
    return f"{base}\n\n{line}" if base else line


def _blocked_backfill_candidates(connection: sqlite3.Connection) -> list[str]:
    """Ids of BLOCKED rows with no lane, no owner and no blocked_reason.

    Those rows are unroutable -- no lane sweep and no person's owned-work
    view can find them -- so ``--apply`` stamps a fixed, greppable reason on
    them (``BLOCKED_BACKFILL_REASON``). Scoped to lane-less AND owner-less
    (``owner_employee_id`` and ``claimed_by`` both NULL) rows only: a blocked
    row that already has a lane or an owner has somewhere a human/lane sweep
    can already find it, so it is left for its own writer to explain.
    """
    rows = connection.execute(
        "SELECT id FROM board_tasks WHERE status = 'blocked' "
        "AND (blocked_reason IS NULL OR blocked_reason = '') "
        "AND lane IS NULL AND owner_employee_id IS NULL AND claimed_by IS NULL"
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _stale_approval_notifications(connection: sqlite3.Connection) -> list[str]:
    placeholders = ",".join("?" for _ in _RESOLVED_APPROVAL_STATES)
    rows = connection.execute(
        "SELECT n.id FROM notifications n JOIN approvals a ON a.id = n.ref_id "
        f"WHERE n.ref_type = 'approval' AND n.read_at IS NULL AND a.state IN ({placeholders})",
        _RESOLVED_APPROVAL_STATES,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _write_receipt(
    path: str,
    *,
    db_path: str,
    applied_at: str,
    groups: list[dict],
    blocked_backfilled_ids: list[str],
) -> None:
    receipt = {
        "applied_at": applied_at,
        "db": db_path,
        "collapsed_groups": groups,
        "collapsed_row_ids": {
            collapsed_id: group["survivor_id"]
            for group in groups
            for collapsed_id in group["collapsed_ids"]
        },
        "blocked_backfilled_ids": blocked_backfilled_ids,
        "blocked_backfill_reason": BLOCKED_BACKFILL_REASON,
    }
    receipt_path = Path(path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collapse(db_path: str, apply: bool, receipt_path: str = DEFAULT_RECEIPT_PATH) -> int:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        passes: list[tuple[str, list[str]]] = [
            (
                "reliability critical re-alerts",
                _duplicates(connection, "kind = 'alert' AND title LIKE ? || '%'", (_STORM_PREFIX,)),
            ),
            (
                "approval re-notifications",
                _duplicates(connection, "kind = 'approval' AND ref_type = 'approval'", ()),
            ),
            ("notifications for resolved approvals", _stale_approval_notifications(connection)),
        ]

        unread_before = int(
            connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
            ).fetchone()[0]
        )
        print(f"db:             {db_path}")
        print(f"unread before:  {unread_before}")
        collapse_ids: set[str] = set()
        for label, ids in passes:
            print(f"  {label}: {len(ids)}")
            collapse_ids.update(ids)
        print(f"to mark read:   {len(collapse_ids)}")

        open_rows = _open_board_task_rows(connection)
        open_before = len(open_rows)
        total_board_tasks_before = int(
            connection.execute("SELECT COUNT(*) FROM board_tasks").fetchone()[0]
        )
        groups = _generalized_duplicate_groups(open_rows)
        board_task_ids = [
            collapsed_id for group in groups for collapsed_id in group["collapsed_ids"]
        ]
        print(f"\nopen board_tasks before: {open_before} (total rows: {total_board_tasks_before})")
        print(
            "  generalized fingerprint collision groups (all disciplines, shadow report): "
            f"{len(groups)}"
        )
        if groups:
            print("  top 20 collision groups (worst-first):")
            for group in groups[:20]:
                print(
                    f"    {group['occurrences']:>5}  [{group['discipline'] or '-'}] "
                    f"{group['sample_title']}"
                )
        print(f"to cancel:      {len(board_task_ids)}")

        blocked_backfill_ids = _blocked_backfill_candidates(connection)
        print(
            "\nblocked rows unroutable (no lane/owner, no blocked_reason): "
            f"{len(blocked_backfill_ids)}"
        )

        total_to_write = len(collapse_ids) + len(board_task_ids) + len(blocked_backfill_ids)
        if not apply or total_to_write == 0:
            if not apply:
                print("\ndry run — re-run with --apply to collapse and backfill")
            return total_to_write

        now = utc_now_iso()
        if collapse_ids:
            connection.executemany(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                [(now, nid) for nid in sorted(collapse_ids)],
            )
            connection.commit()

        store = CollabStore(db_path)
        applied_groups: list[dict] = []
        for group in groups:
            survivor = store.get_board_task(group["survivor_id"])
            if survivor is None:
                # Concurrent write raced this row out from under us; skip
                # rather than fabricate a rollup for a card we cannot read.
                continue
            new_description = _apply_rollup(
                str(survivor.get("description") or ""),
                group["occurrences"],
                group["first_seen"],
                group["last_seen"],
            )
            store.update_board_task(
                group["survivor_id"], {"description": new_description}, actor=_ACTOR
            )
            collapsed_now: list[str] = []
            for collapsed_id in group["collapsed_ids"]:
                if store.update_board_task(
                    collapsed_id, {"status": "cancelled"}, expect_status="open", actor=_ACTOR
                ):
                    collapsed_now.append(collapsed_id)
            applied_groups.append({**group, "collapsed_ids": collapsed_now})

        backfilled_now: list[str] = []
        for task_id in blocked_backfill_ids:
            if store.update_board_task(
                task_id,
                {"blocked_reason": BLOCKED_BACKFILL_REASON},
                expect_status="blocked",
                actor=_ACTOR,
            ):
                backfilled_now.append(task_id)

        if applied_groups or backfilled_now:
            _write_receipt(
                receipt_path,
                db_path=db_path,
                applied_at=now,
                groups=applied_groups,
                blocked_backfilled_ids=backfilled_now,
            )
            print(f"\nreceipt written: {receipt_path}")

        unread_after = int(
            connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
            ).fetchone()[0]
        )
        open_after = int(
            connection.execute("SELECT COUNT(*) FROM board_tasks WHERE status = 'open'").fetchone()[
                0
            ]
        )
        total_board_tasks_after = int(
            connection.execute("SELECT COUNT(*) FROM board_tasks").fetchone()[0]
        )
        print(
            f"\ncollapsed at {now}; unread now {unread_after}; "
            f"open board_tasks now {open_after} "
            f"(total board_tasks rows unchanged: {total_board_tasks_before} -> "
            f"{total_board_tasks_after}); "
            f"blocked rows backfilled: {len(backfilled_now)}"
        )
        return len(collapse_ids) + sum(len(g["collapsed_ids"]) for g in applied_groups) + len(
            backfilled_now
        )
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=default_db_path(), help="database path")
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument(
        "--receipt-path",
        default=DEFAULT_RECEIPT_PATH,
        help="where to write the apply receipt (default: %(default)s)",
    )
    args = parser.parse_args()
    collapse(args.db, args.apply, receipt_path=args.receipt_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
