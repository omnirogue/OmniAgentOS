"""Idempotent seed of the general-engineering goal ladder for three companies.

The dev-queue import (``omniagentos/team/data/dev_queue_2026_08_13.yaml``)
mints the ``omniagentos`` and ``initech`` ladders; this seeder covers the
other three — **Globex, AcmeUni, Hooli** — with the SAME structure the
YAML uses: one ``long_term`` parent per company, one ``short_term`` child
titled EXACTLY ``General engineering — <Display Name>``. That title prefix is
a hard contract: the Slack ``#company`` flag and the board's company-goal
lookup resolve on ``title LIKE 'General engineering%'``, so a reworded title
is a goal those paths cannot find.

Idempotency is keyed on the company: any company that ALREADY has a
``short_term`` goal titled ``General engineering — …`` is skipped whole — the
seeder never creates a second ladder, and re-running it is always safe.
Goals are owned by ``emp_owner`` (the operator prices and owns the catch-all
ladder until a company grows a dedicated owner).

A ladder with no rungs is inert: the company-agnostic pool selector
(``TeamStore.pool_cards``, ``omniagentos/team/store.py``) has nothing to show
until a ``board_tasks`` row exists, joined to the goal, that clears its
predicate (open, unowned, unarchived, no parent, non-baseline source,
non-empty acceptance criteria). So every ``short_term`` goal this module is
responsible for — freshly minted OR already existing — also gets exactly one
such card, via :class:`~omniagentos.collab.store.CollabStore`'s canonical
``create_board_task`` (never a raw INSERT).

Idempotency under CONCURRENCY, not just re-run, is a real requirement: two
seeders racing must not both observe "no card yet" and both insert. There is
no DB-level uniqueness on ``(goal_id, source)`` and adding one needs a
migration, so idempotency is carried instead on ``board_tasks.ref``, which
DOES carry a real partial ``UNIQUE INDEX`` (``idx_board_tasks_ref``, migration
123, ``WHERE ref IS NOT NULL``). Each card's ``ref`` is the deterministic
:func:`_pool_card_ref` of its goal id, and the INSERT happens inside
``create_board_task``'s own ``BEGIN IMMEDIATE`` transaction — SQLite
serializes writers there, so a second, racing insert of the SAME ref always
sees the first one's committed row and is refused with
``ValueError('ref_conflict')`` rather than writing a duplicate. See
:func:`_ensure_pool_card`.

CLI::

    OMNIAGENTOS_DB=/tmp/scratch.db python -m omniagentos.company_goals.seed_company_goals
    python -m omniagentos.company_goals.seed_company_goals --db /tmp/scratch.db [--dry-run]

Never point this at the live database from a feature lane — seed through the
serving checkout's own runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.models import LONG_TERM, SHORT_TERM
from omniagentos.company_goals.seed_employees import seed_employees
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore

__all__ = [
    "GOAL_OWNER",
    "POOL_CARD_SOURCE",
    "SEED_COMPANIES",
    "seed_company_goals",
    "main",
]

#: (slug, display name) — display name is what the short_term title embeds.
SEED_COMPANIES: tuple[tuple[str, str], ...] = (
    ("globex", "Globex"),
    ("acmeuni", "AcmeUni"),
    ("hooli", "Hooli"),
)

GOAL_OWNER = "emp_owner"

#: The hard title contract (see module docstring). ``<Display Name>`` follows.
SHORT_TERM_TITLE_PREFIX = "General engineering — "

#: ``board_tasks.source`` for cards this module mints. Deliberately NOT
#: ``BASELINE_SOURCE`` ("baseline-2026-08-03") — the pool selector excludes
#: that source on purpose, and a card seeded here must clear the pool, not be
#: filtered out of it.
POOL_CARD_SOURCE = "company-goal-seed-2026-08"

#: Prefix for the deterministic, per-goal ``board_tasks.ref``. See the module
#: docstring: this is the natural key concurrency-safe idempotency rides on,
#: not ``(goal_id, source)`` alone (which no index enforces atomically).
_POOL_CARD_REF_PREFIX = f"{POOL_CARD_SOURCE}:"


def _pool_card_ref(goal_id: str) -> str:
    """Deterministic ``board_tasks.ref`` for ``goal_id``'s pool card.

    Globally unique (one goal never has two of these cards) and stable across
    runs, so it is both the idempotency key and, via ``idx_board_tasks_ref``,
    an actual DB-level uniqueness guarantee — see :func:`_ensure_pool_card`.

    Pre-uppercased to match ``CollabStore._normalized_ref``'s storage form
    (every ``ref`` is folded to uppercase before it is written). Computing it
    here rather than relying on the write path's normalization means the read
    side (:func:`_existing_pool_card_id`, and the ``ref_conflict`` recovery in
    :func:`_ensure_pool_card`) queries the SAME string that is actually on
    disk, instead of a lowercase id fragment that would never match.
    """
    return f"{_POOL_CARD_REF_PREFIX}{goal_id}".upper()


def _company_id(store: CompanyGoalsStore, slug: str, name: str) -> str:
    """The org_companies id for ``slug``, creating the row when absent.

    ``org_companies`` belongs to orgdims; the insert mirrors its shape (the
    same direct idiom the orgdims seeds use) and is guarded on the slug so a
    concurrently-created row is adopted, never duplicated.
    """
    row = store._connection.execute(
        "SELECT id FROM org_companies WHERE slug = ?", (slug,)
    ).fetchone()
    if row is not None:
        return str(row["id"])
    company_id = f"co_{slug}"
    store._store._write(
        "INSERT OR IGNORE INTO org_companies (id, slug, name, status, created_at) "
        "VALUES (?, ?, ?, 'active', ?)",
        (company_id, slug, name, utc_now_iso()),
    )
    row = store._connection.execute(
        "SELECT id FROM org_companies WHERE slug = ?", (slug,)
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _existing_short_term_goal_id(store: CompanyGoalsStore, org_company_id: str) -> str | None:
    row = store._connection.execute(
        "SELECT id FROM company_goals WHERE org_company_id = ? AND horizon = ? "
        "AND title LIKE ? LIMIT 1",
        (org_company_id, SHORT_TERM, f"{SHORT_TERM_TITLE_PREFIX}%"),
    ).fetchone()
    return None if row is None else str(row["id"])


def _has_general_engineering_goal(store: CompanyGoalsStore, org_company_id: str) -> bool:
    return _existing_short_term_goal_id(store, org_company_id) is not None


def _legacy_pool_card_id(store: CompanyGoalsStore, goal_id: str) -> str | None:
    """A pre-ref-era card for ``(goal_id, POOL_CARD_SOURCE)`` with ``ref IS NULL``.

    The round-1 head (``090d8645``) persisted cards exactly this way — no
    ``ref`` at all. A database it already seeded is a real upgrade target,
    not a hypothetical: this lookup is what lets :func:`_ensure_pool_card`
    recognise and adopt that row instead of minting a second one next to it.
    """
    row = store._connection.execute(
        "SELECT id FROM board_tasks WHERE goal_id = ? AND source = ? AND ref IS NULL LIMIT 1",
        (goal_id, POOL_CARD_SOURCE),
    ).fetchone()
    return None if row is None else str(row["id"])


def _existing_pool_card_id(store: CompanyGoalsStore, goal_id: str) -> str | None:
    """Read-only lookup for reporting: the ref-bearing card, or a legacy one.

    Safe to call outside a write transaction (dry-run reporting uses this):
    it never itself decides whether to write, so a stale read here can only
    make a dry-run report slightly conservative, never create a duplicate.
    """
    row = store._connection.execute(
        "SELECT id FROM board_tasks WHERE ref = ? LIMIT 1",
        (_pool_card_ref(goal_id),),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    return _legacy_pool_card_id(store, goal_id)


def _ensure_pool_card(
    store: CompanyGoalsStore, collab_store: CollabStore, *, goal_id: str, name: str
) -> tuple[str, bool]:
    """Return ``(card_id, created)`` for the goal's pool card, minting it once —
    including under concurrency (see the module docstring) and across the
    upgrade from a database the round-1, ref-less head already seeded.

    Three cases, checked in order:

    1. **Legacy adoption.** A card already exists for this exact
       ``(goal_id, POOL_CARD_SOURCE)`` with ``ref IS NULL`` — the round-1
       shape. Adopted (and its ``ref`` back-filled) rather than left to
       collide silently with a second, ref-bearing insert below: that insert
       would carry a DIFFERENT primary key, so ``ref IS NULL`` never conflicts
       with it and a naive re-run would otherwise mint a duplicate.
    2. **Concurrent-safe create.** No legacy row: attempt the canonical
       insert. A losing racer is told so by ``ValueError('ref_conflict')`` —
       raised from INSIDE the same ``BEGIN IMMEDIATE`` transaction as the
       insert it lost to — rather than by a check this function ran itself,
       earlier, unprotected.
    3. **Conflict resolution is OWNERSHIP-CHECKED.** ``board_tasks.ref`` is a
       general-purpose column other writers use too. A ref clash is resolved
       by adopting the conflicting row ONLY when its ``goal_id`` and
       ``source`` actually match this call — never by identity of ``ref``
       alone. A clash with an unrelated row (this goal's deterministic ref
       coinciding with something else's manually-set ref) is a loud error
       naming the foreign row, not a silent adoption or a silent duplicate.
    """
    legacy_id = _legacy_pool_card_id(store, goal_id)
    if legacy_id is not None:
        ref = _pool_card_ref(goal_id)
        # Idempotent: a second concurrent backfill of the SAME value on the
        # SAME already-unique row is a no-op, not a race.
        store._store._write(
            "UPDATE board_tasks SET ref = ? WHERE id = ? AND ref IS NULL",
            (ref, legacy_id),
        )
        return legacy_id, False

    ref = _pool_card_ref(goal_id)
    task = BoardTask(
        title=f"{name} engineering: triage and file the next concrete task",
        goal_id=goal_id,
        acceptance_criteria=(
            f"Review {name}'s open engineering surface and either close something "
            "small end-to-end or file a scoped follow-up card with acceptance criteria."
        ),
        source=POOL_CARD_SOURCE,
        size="S",
        ref=ref,
    )
    try:
        collab_store.create_board_task(task, actor="seed_company_goals")
    except ValueError as exc:
        if str(exc) != "ref_conflict":
            raise
        existing = store._connection.execute(
            "SELECT id, goal_id, source FROM board_tasks WHERE ref = ? LIMIT 1", (ref,)
        ).fetchone()
        if existing is None:  # pragma: no cover - defensive; see module docstring
            raise RuntimeError(
                f"ref_conflict for {ref!r} but no row exists — a card was deleted "
                "mid-race, which this module cannot recover from safely"
            ) from exc
        if str(existing["goal_id"]) != goal_id or str(existing["source"]) != POOL_CARD_SOURCE:
            raise RuntimeError(
                f"ref_conflict for {ref!r} resolves to a FOREIGN card "
                f"{existing['id']!r} (goal_id={existing['goal_id']!r}, "
                f"source={existing['source']!r}) that does not belong to goal "
                f"{goal_id!r}/source {POOL_CARD_SOURCE!r} — refusing to silently "
                "adopt an unrelated row (and refusing to mint a second card, "
                "which would need a different ref and defeat the whole point)"
            ) from exc
        return str(existing["id"]), False
    return task.id, True


def seed_company_goals(store: CompanyGoalsStore, *, dry_run: bool = False) -> dict[str, Any]:
    """Mint the missing ladders. Returns per-company outcomes.

    ``created`` names both goal ids; ``existing`` means the company already
    carries a general-engineering short_term goal and was left untouched.
    """
    if not dry_run:
        seed_employees(store)  # emp_owner must exist before it can own a goal
    collab_store: CollabStore | None = None
    if not dry_run:
        # Bound to the SAME SqliteStore instance ``store`` already holds —
        # NOT ``CollabStore(store._store._db_path)``. For a file-backed DB the
        # path form happens to reach the same file, but for ``:memory:`` (a
        # supported CompanyGoalsStore input) a path-based re-open is a
        # DIFFERENT, empty in-memory database: the goal ladder would commit to
        # one DB and the card insert would then fail its ``goal_id`` foreign
        # key against the other. Constructing via ``__new__`` and binding
        # ``_store`` directly (the same idiom ``omniagentos/edc/actions.py``
        # and ``omniagentos/edc/sweep.py`` already use for exactly this
        # reason) guarantees one store, one connection registry, one writer
        # lock, regardless of backend.
        collab_store = CollabStore.__new__(CollabStore)
        collab_store._store = store._store
    outcomes: dict[str, Any] = {}
    for slug, name in SEED_COMPANIES:
        existing_company = store._connection.execute(
            "SELECT id FROM org_companies WHERE slug = ?", (slug,)
        ).fetchone()
        if dry_run:
            if existing_company is not None and _has_general_engineering_goal(
                store, str(existing_company["id"])
            ):
                goal_id = _existing_short_term_goal_id(store, str(existing_company["id"]))
                assert goal_id is not None
                if _existing_pool_card_id(store, goal_id) is not None:
                    outcomes[slug] = {"outcome": "existing"}
                else:
                    # The ladder is healthy but its pool card is missing — a
                    # real run WOULD write here. Reporting bare "existing"
                    # would be indistinguishable from the true no-op case.
                    outcomes[slug] = {"outcome": "existing", "card": "would-create"}
            else:
                outcomes[slug] = {"outcome": "would-create"}
            continue
        assert collab_store is not None
        company_id = _company_id(store, slug, name)
        existing_short_term = _existing_short_term_goal_id(store, company_id)
        if existing_short_term is not None:
            card_id, card_created = _ensure_pool_card(
                store, collab_store, goal_id=existing_short_term, name=name
            )
            outcomes[slug] = {
                "outcome": "existing",
                "short_term": existing_short_term,
                "card": card_id,
                "card_created": card_created,
            }
            continue
        parent = store.create_goal(
            org_company_id=company_id,
            title=f"{name} product & engineering health",
            horizon=LONG_TERM,
            owner_employee_id=GOAL_OWNER,
        )
        child = store.create_goal(
            org_company_id=company_id,
            title=f"{SHORT_TERM_TITLE_PREFIX}{name}",
            horizon=SHORT_TERM,
            parent_goal_id=str(parent["id"]),
            owner_employee_id=GOAL_OWNER,
        )
        card_id, card_created = _ensure_pool_card(
            store, collab_store, goal_id=str(child["id"]), name=name
        )
        outcomes[slug] = {
            "outcome": "created",
            "long_term": str(parent["id"]),
            "short_term": str(child["id"]),
            "card": card_id,
            "card_created": card_created,
        }
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed Globex/AcmeUni/Hooli general-engineering goal ladders."
    )
    parser.add_argument(
        "--db",
        default=None,
        help="database path (default: $OMNIAGENTOS_DB, then the configured default)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be created; write nothing"
    )
    args = parser.parse_args(argv)
    db_path = args.db or os.environ.get("OMNIAGENTOS_DB") or default_db_path()
    store = CompanyGoalsStore(SqliteStore(str(db_path)))
    outcomes = seed_company_goals(store, dry_run=args.dry_run)
    print(json.dumps({"db": str(db_path), "companies": outcomes}, indent=2, sort_keys=True))
    created = sum(1 for value in outcomes.values() if value["outcome"] == "created")
    print(f"seed_company_goals: {created} created", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
