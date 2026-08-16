#!/usr/bin/env python3
"""Import the 2026-08-10 production-reset queue into the Team Work OS (migration 123).

Reads ``omniagentos/team/data/production_reset_2026_08_10.yaml`` (the packaged data file —
override with ``--yaml`` for a different snapshot or a test fixture) and creates the goals and
board cards it describes via :class:`~omniagentos.company_goals.store.CompanyGoalsStore`,
:class:`~omniagentos.collab.store.CollabStore` and :class:`~omniagentos.team.store.TeamStore` —
the same three stores every other Team Work OS writer uses, composed over ONE
:class:`~omniagentos.db.store.SqliteStore` so a goal write and a board write can never interleave
into each other's transaction (the same discipline ``tests/team/conftest.py`` documents).

Idempotent by construction, never by convention: a goal is skipped on an existing
``(org_company_id, title)`` match, a task is skipped on an existing ``ref``. Existing baseline
cards are also reconciled if their authored evidence or verification stamp is missing. Re-running
against a healthy database creates and repairs nothing — see
``tests/team_import/test_import.py::test_reimport_creates_nothing``.

Two passes over ``tasks``: every task with no ``parent_task_ref`` first (this is every UP-*/SP-*/
OPSP-* parent AND every standalone card, e.g. OPS-3), then every task that names one — the
schema's hierarchy is exactly one level deep (``CollabStore._validate_team_rules`` refuses a
second), so two passes are sufficient and a parent's real board_tasks id always exists before its
children are created.

Exit codes: ``0`` clean (everything created, or every ref/goal already existed), ``1`` PARTIAL —
the run finished but something was refused (an unknown company slug, an unresolved parent), listed
in the report and the log, ``2`` could not run at all (bad YAML, cannot open the database).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# A worktree checkout can carry brand-new packages (this branch's ``omniagentos.team``) that do
# not exist yet on whatever checkout ``$PYTHONPATH`` happens to name (commonly the main serving
# checkout, set by the shell profile). Run as ``python scripts/import-reset-queue.py`` — as
# opposed to ``python -c`` or pytest, both of which insert the invoking cwd/rootdir ahead of
# ``$PYTHONPATH`` — sys.path[0] is this file's OWN directory (``scripts/``), so a plain `import
# omniagentos` falls through to ``$PYTHONPATH`` and finds a same-named but stale package that has
# no ``team`` submodule at all. This worktree's own editable install ALSO puts this same root on
# sys.path, but at the tail end (after ``$PYTHONPATH``) — a "skip if already present" guard would
# see it there and never fix the ORDER, which is the actual problem. Always insert at position 0;
# a duplicate entry later in sys.path is harmless.
sys.path.insert(0, str(REPO_ROOT))

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTask  # noqa: E402
from omniagentos.collab.store import CollabStore  # noqa: E402
from omniagentos.company_goals.store import CompanyGoalsStore  # noqa: E402
from omniagentos.contracts import default_db_path, utc_now_iso  # noqa: E402
from omniagentos.team.store import TeamStore  # noqa: E402

DEFAULT_LOG_PATH = REPO_ROOT / "devtasks" / "team-os-import-20260810" / "IMPORT-LOG.md"
_KNOWN_OWNERS = frozenset({"emp_owner", "emp_alice", "emp_bob"})
_REQUIRED_COMPANY_SLUGS = frozenset(
    {"initech", "globex", "acmeuni", "hooli", "omniagentos", "personal"}
)


class ImportPreflightError(RuntimeError):
    """The target database lacks prerequisites the importer is not allowed to invent."""


def _default_yaml_path() -> Path:
    """The packaged production-reset data file, resolved as package data.

    ``omniagentos/team/data/`` carries no ``__init__.py`` (it is data, not a subpackage) — that is
    fine for :func:`importlib.resources.files`, which only needs ``omniagentos.team`` itself to be
    importable and then walks into the plain directory beneath it.
    """
    return Path(str(files("omniagentos.team").joinpath("data", "production_reset_2026_08_10.yaml")))


@dataclass
class ImportResult:
    """What one run did. ``dry_run`` entries land in ``planned_*``, a real run's in ``created_*``."""

    dry_run: bool
    created_goals: list[str] = field(default_factory=list)
    skipped_goals: list[str] = field(default_factory=list)
    planned_goals: list[str] = field(default_factory=list)
    created_tasks: list[str] = field(default_factory=list)
    skipped_tasks: list[str] = field(default_factory=list)
    planned_tasks: list[str] = field(default_factory=list)
    repairs: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the document root, got {type(data)!r}")
    for key in ("companies", "goals", "tasks"):
        if key not in data or not isinstance(data[key], list):
            raise ValueError(f"{path}: missing or non-list top-level key {key!r}")
    return data


def _resolve_company_ids(connection: sqlite3.Connection, companies: list[dict[str, Any]]) -> dict[str, str]:
    """slug -> org_companies.id for every slug this file names, for the slugs that EXIST.

    Never creates a company (P7's brief: "do NOT create new companies") — a slug this file names
    but the live database does not have is a warning here, and every goal that needs it is refused
    downstream rather than silently guessing an id.
    """
    resolved: dict[str, str] = {}
    for company in companies:
        slug = str(company["slug"])
        row = connection.execute("SELECT id FROM org_companies WHERE slug = ?", (slug,)).fetchone()
        if row is not None:
            resolved[slug] = str(row["id"])
    return resolved


def _preflight(
    connection: sqlite3.Connection, goals_store: CompanyGoalsStore
) -> None:
    """Fail before item processing when the production roster/taxonomy is absent.

    The importer deliberately does not seed either table: doing that here would turn a queue
    import into an org mutation and conceal a broken startup sequence. The error names the real
    seed callables, so the operator has a deterministic remedy. This check runs under dry-run as
    well: a plan against a database that could not accept the real import is not a valid plan.
    """
    missing_employees = sorted(
        employee_id
        for employee_id in _KNOWN_OWNERS
        if goals_store.get_employee(employee_id) is None
    )
    placeholders = ", ".join("?" for _ in _REQUIRED_COMPANY_SLUGS)
    rows = connection.execute(
        f"SELECT slug FROM org_companies WHERE slug IN ({placeholders})",
        tuple(sorted(_REQUIRED_COMPANY_SLUGS)),
    ).fetchall()
    present_slugs = {str(row["slug"]) for row in rows}
    missing_slugs = sorted(_REQUIRED_COMPANY_SLUGS - present_slugs)

    lines: list[str] = []
    if missing_employees:
        lines.extend(
            (
                "import preflight failed: missing employee ids: "
                + ", ".join(missing_employees),
                "remedy: run "
                "omniagentos.company_goals.seed_employees.seed_employees"
                "(CompanyGoalsStore(store)) against this database",
            )
        )
    if missing_slugs:
        lines.extend(
            (
                "import preflight failed: missing org company slugs: " + ", ".join(missing_slugs),
                "remedy: run omniagentos.orgdims.service.OrgDimsService"
                "(db_path=...).ensure_seeded() against this database",
            )
        )
    if lines:
        raise ImportPreflightError("\n".join(lines))


def _find_existing_goal(
    goals_store: CompanyGoalsStore, org_company_id: str, title: str
) -> dict[str, Any] | None:
    for row in goals_store.list_goals(org_company_id=org_company_id):
        if str(row["title"]) == title:
            return row
    return None


def _find_existing_task_id(connection: sqlite3.Connection, ref: str) -> str | None:
    row = connection.execute("SELECT id FROM board_tasks WHERE ref = ?", (ref,)).fetchone()
    return None if row is None else str(row["id"])


def _owned_paths_line(owned_paths: list[str]) -> str:
    return "Owned paths: " + "; ".join(str(p) for p in owned_paths)


def _task_description(entry: dict[str, Any]) -> str:
    """The card's description: the authored text, plus an "Owned paths:" line.

    ``board_tasks`` has no dedicated owned-paths column. The machine-readable list is stored under
    ``org_json.owned_paths`` after creation; this description copy is the human-readable view.
    """
    description = str(entry.get("description") or "").strip()
    owned_paths = entry.get("owned_paths") or []
    if not owned_paths:
        return description
    line = _owned_paths_line(owned_paths)
    return f"{description}\n\n{line}" if description else line


def _import_goals(
    *,
    data: dict[str, Any],
    connection: sqlite3.Connection,
    goals_store: CompanyGoalsStore,
    store_write: Any,
    company_ids: dict[str, str],
    dry_run: bool,
    result: ImportResult,
) -> dict[str, str]:
    """Create/skip every goal, long_term before short_term. Returns ref -> goal id (or, under
    ``--dry-run``, ref -> a placeholder string) so task creation can resolve ``goal_ref``."""
    goal_id_map: dict[str, str] = {}
    ordered = sorted(data["goals"], key=lambda g: g.get("horizon") != "long_term")
    for entry in ordered:
        ref = str(entry["ref"])
        title = str(entry["title"])
        company_slug = str(entry["company"])
        company_id = company_ids.get(company_slug)
        if company_id is None:
            result.refused.append((f"goal:{ref}", f"unknown company slug {company_slug!r}"))
            continue

        existing = _find_existing_goal(goals_store, company_id, title)
        if existing is not None:
            goal_id_map[ref] = str(existing["id"])
            result.skipped_goals.append(ref)
            continue

        parent_id: str | None = None
        parent_ref = entry.get("parent")
        if parent_ref:
            parent_id = goal_id_map.get(str(parent_ref))
            if parent_id is None:
                result.refused.append((f"goal:{ref}", f"parent goal {parent_ref!r} was not created"))
                continue

        if dry_run:
            goal_id_map[ref] = f"(dry-run:{ref})"
            result.planned_goals.append(ref)
            continue

        try:
            created = goals_store.create_goal(
                org_company_id=company_id,
                title=title,
                horizon=str(entry["horizon"]),
                parent_goal_id=parent_id,
            )
            goal_id_map[ref] = str(created["id"])
            owner = entry.get("owner")
            if owner:
                # CompanyGoalsStore predates migration 123's owner_employee_id column and has no
                # setter for it (create_goal/update_goal's allowed-field sets both omit it) — a
                # direct, single-statement write on the composed store, the same idiom
                # CompanyGoalsStore/TeamStore themselves use internally (``self._store._write``).
                store_write(
                    "UPDATE company_goals SET owner_employee_id = ? WHERE id = ?",
                    (owner, created["id"]),
                )
        except sqlite3.IntegrityError as exc:
            result.refused.append((f"goal:{ref}", f"integrity error: {exc}"))
            continue
        result.created_goals.append(ref)
    return goal_id_map


def _reconcile_baseline(
    *,
    entry: dict[str, Any],
    task_id: str,
    connection: sqlite3.Connection,
    team_store: TeamStore,
    actor: str,
    result: ImportResult,
    record_repairs: bool,
) -> None:
    """Ensure the authored evidence and verification exist for one baseline card.

    The evidence uniqueness key is global. A row already claimed by another task (or manually
    parked without a task) is a conflict, not permission to move a human decision silently.
    """
    baseline_evidence = entry.get("baseline_evidence")
    if str(entry.get("source") or "") != BASELINE_SOURCE or not baseline_evidence:
        return

    evidence_ref = str(baseline_evidence["ref"])
    existing_evidence = connection.execute(
        "SELECT id, task_id, attribution FROM task_evidence "
        "WHERE kind = 'doc' AND repo = '' AND ref = ?",
        (evidence_ref,),
    ).fetchone()
    if existing_evidence is not None and existing_evidence["task_id"] != task_id:
        claimed_by = existing_evidence["task_id"] or "unattributed/manual"
        raise ValueError(
            f"baseline evidence {evidence_ref!r} already belongs to {claimed_by}"
        )

    if existing_evidence is None:
        row, outcome = team_store.record_evidence(
            kind="doc",
            ref=evidence_ref,
            task_id=task_id,
            title=str(baseline_evidence.get("title") or "Baseline evidence summary"),
            attribution="manual",
            actor=actor,
        )
        if outcome == "conflict" or row.get("task_id") != task_id:
            raise ValueError(f"baseline evidence {evidence_ref!r} could not attach to {task_id}")
        if record_repairs:
            result.repairs.append((str(entry["ref"]), "evidence"))

    card = connection.execute(
        "SELECT status, verified_at FROM board_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if card is None:
        raise ValueError(f"baseline task disappeared during reconciliation: {task_id}")
    if card["verified_at"] is None:
        team_store.verify_task(task_id, verifier="emp_owner")
        if record_repairs:
            result.repairs.append((str(entry["ref"]), "verification"))


def _import_tasks(
    *,
    data: dict[str, Any],
    connection: sqlite3.Connection,
    collab_store: CollabStore,
    team_store: TeamStore,
    store_write: Any,
    goal_id_map: dict[str, str],
    dry_run: bool,
    actor: str,
    result: ImportResult,
) -> None:
    tasks = data["tasks"]
    by_ref = {str(t["ref"]): t for t in tasks}
    # Pass 1: every task with no parent (every UP-*/SP-*/OPSP-* parent, plus standalones).
    # Pass 2: every task that names one of pass 1's refs as its parent_task_ref.
    ordered = sorted(tasks, key=lambda t: bool(t.get("parent_task_ref")))
    task_id_map: dict[str, str] = {}

    for entry in ordered:
        ref = str(entry["ref"])
        parent_ref = entry.get("parent_task_ref")

        existing_id = _find_existing_task_id(connection, ref)
        if existing_id is not None:
            task_id_map[ref] = existing_id
            result.skipped_tasks.append(ref)
            if not dry_run:
                try:
                    _reconcile_baseline(
                        entry=entry,
                        task_id=existing_id,
                        connection=connection,
                        team_store=team_store,
                        actor=actor,
                        result=result,
                        record_repairs=True,
                    )
                except sqlite3.IntegrityError as exc:
                    result.refused.append((ref, f"integrity error: {exc}"))
                except ValueError as exc:
                    result.refused.append((ref, str(exc)))
            continue

        owner = entry.get("owner")
        if owner is not None and owner not in _KNOWN_OWNERS:
            result.refused.append((ref, f"owner {owner!r} is not one of {sorted(_KNOWN_OWNERS)}"))
            continue

        goal_id: str | None = None
        goal_ref = entry.get("goal_ref")
        if goal_ref:
            goal_id = goal_id_map.get(str(goal_ref))
            if goal_id is None:
                result.refused.append((ref, f"goal {goal_ref!r} was not created"))
                continue

        parent_id: str | None = None
        if parent_ref:
            if str(parent_ref) not in by_ref:
                result.refused.append((ref, f"parent_task_ref {parent_ref!r} is not in this file"))
                continue
            parent_id = task_id_map.get(str(parent_ref))
            if parent_id is None:
                result.refused.append((ref, f"parent task {parent_ref!r} was not created"))
                continue

        if dry_run:
            task_id_map[ref] = f"(dry-run:{ref})"
            result.planned_tasks.append(ref)
            continue

        task = BoardTask(
            title=str(entry["title"]),
            description=_task_description(entry),
            status=str(entry.get("status", "open")),
            priority=str(entry.get("priority") or "normal"),
            parent_task_id=parent_id,
            goal_id=goal_id,
            owner_employee_id=owner,
            ref=ref,
            size=str(entry.get("size", "M")),
            acceptance_criteria=str(entry.get("acceptance_criteria") or ""),
            blocked_reason=str(entry.get("blocked_reason") or ""),
            due_date=entry.get("due_date"),
            source=str(entry.get("source") or "production-reset"),
        )
        try:
            collab_store.create_board_task(task, actor=actor)
        except ValueError as exc:
            if str(exc) == "ref_conflict":
                # Raced with another writer between the pre-check and this INSERT; adopt it.
                adopted = _find_existing_task_id(connection, ref)
                if adopted is not None:
                    task_id_map[ref] = adopted
                    result.skipped_tasks.append(ref)
                    continue
            result.refused.append((ref, str(exc)))
            continue
        except sqlite3.IntegrityError as exc:
            result.refused.append((ref, f"integrity error: {exc}"))
            continue

        task_id_map[ref] = task.id
        result.created_tasks.append(ref)

        try:
            owned_paths = entry.get("owned_paths") or []
            if owned_paths:
                # The attribution engine's owned-paths matcher reads org_json.owned_paths
                # (team/sweep.py); the "Owned paths:" description line is for humans.
                store_write(
                    "UPDATE board_tasks SET org_json = json_set(COALESCE(org_json, '{}'),"
                    " '$.owned_paths', json(?)) WHERE id = ?",
                    (json.dumps([str(p) for p in owned_paths]), task.id),
                )

            _reconcile_baseline(
                entry=entry,
                task_id=task.id,
                connection=connection,
                team_store=team_store,
                actor=actor,
                result=result,
                record_repairs=False,
            )
        except sqlite3.IntegrityError as exc:
            result.refused.append((ref, f"integrity error: {exc}"))
        except ValueError as exc:
            result.refused.append((ref, str(exc)))


def run_import(
    *,
    db_path: str,
    yaml_path: Path,
    dry_run: bool,
    actor: str = "system",
) -> ImportResult:
    """Run one import pass. Never writes when ``dry_run`` is True."""
    data = _load_yaml(yaml_path)
    result = ImportResult(dry_run=dry_run)

    collab_store = CollabStore(db_path)  # migrates the database (idempotent) like every other caller
    store = collab_store._store
    connection = store._connection
    goals_store = CompanyGoalsStore(store)
    team_store = TeamStore(store)

    _preflight(connection, goals_store)

    for employee_id in sorted(_KNOWN_OWNERS):
        employee = goals_store.get_employee(employee_id)
        if employee is not None and not employee.get("role"):
            result.warnings.append(f"employee {employee_id} exists but has no role set")

    company_ids = _resolve_company_ids(connection, data["companies"])
    for company in data["companies"]:
        slug = str(company["slug"])
        if slug not in company_ids:
            result.warnings.append(
                f"company slug {slug!r} not found in org_companies — every goal naming it is refused"
            )

    goal_id_map = _import_goals(
        data=data,
        connection=connection,
        goals_store=goals_store,
        store_write=store._write,
        company_ids=company_ids,
        dry_run=dry_run,
        result=result,
    )
    _import_tasks(
        data=data,
        connection=connection,
        collab_store=collab_store,
        team_store=team_store,
        store_write=store._write,
        goal_id_map=goal_id_map,
        dry_run=dry_run,
        actor=actor,
        result=result,
    )
    return result


def _print_report(result: ImportResult) -> None:
    verb = "would create" if result.dry_run else "created"
    print(f"goals {verb}: {len(result.planned_goals if result.dry_run else result.created_goals)}")
    for ref in (result.planned_goals if result.dry_run else result.created_goals):
        print(f"  [create] goal:{ref}")
    for ref in result.skipped_goals:
        print(f"  [skip]   goal:{ref} (already exists)")

    print(f"tasks {verb}: {len(result.planned_tasks if result.dry_run else result.created_tasks)}")
    for ref in (result.planned_tasks if result.dry_run else result.created_tasks):
        print(f"  [create] {ref}")
    for ref in result.skipped_tasks:
        print(f"  [skip]   {ref} (already exists)")
    for ref, action in result.repairs:
        print(f"  [repair] {ref}: {action}")

    for ref, reason in result.refused:
        print(f"  [refused] {ref}: {reason}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  [warn] {warning}", file=sys.stderr)


def _write_import_log(result: ImportResult, log_path: Path) -> None:
    """Only called on a real (non-dry-run) pass."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Team Work OS import log",
        "",
        f"Run at {utc_now_iso()} (UTC).",
        "",
        f"## Goals — created {len(result.created_goals)}, skipped {len(result.skipped_goals)}",
        "",
    ]
    lines += [f"- created: `{ref}`" for ref in result.created_goals]
    lines += [f"- skipped (already existed): `{ref}`" for ref in result.skipped_goals]
    lines += [
        "",
        f"## Tasks — created {len(result.created_tasks)}, skipped {len(result.skipped_tasks)}",
        "",
    ]
    lines += [f"- created: `{ref}`" for ref in result.created_tasks]
    lines += [f"- skipped (already existed): `{ref}`" for ref in result.skipped_tasks]
    if result.repairs:
        lines += ["", f"## Repairs ({len(result.repairs)})", ""]
        lines += [f"- `{ref}`: restored {action}" for ref, action in result.repairs]
    if result.refused:
        lines += ["", f"## Refused ({len(result.refused)})", ""]
        lines += [f"- `{ref}`: {reason}" for ref, reason in result.refused]
    if result.warnings:
        lines += ["", f"## Warnings ({len(result.warnings)})", ""]
        lines += [f"- {warning}" for warning in result.warnings]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="Database path (default: $OMNIAGENTOS_DB, else var/omniagentos.db — same "
        "resolution as omniagentos.contracts.default_db_path()).",
    )
    parser.add_argument(
        "--yaml",
        default=None,
        type=Path,
        help="Override the packaged production_reset_2026_08_10.yaml (e.g. a test fixture).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the creation plan; write nothing.",
    )
    parser.add_argument(
        "--log-path",
        default=None,
        type=Path,
        help="Override devtasks/team-os-import-20260810/IMPORT-LOG.md (tests inject a tmp path).",
    )
    parser.add_argument(
        "--actor",
        default="system",
        help="Actor recorded on every task_events row this run writes (default: system).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db or default_db_path()
    yaml_path = args.yaml or _default_yaml_path()

    print(f"import-reset-queue: db={db_path} yaml={yaml_path} dry_run={args.dry_run}")

    try:
        result = run_import(
            db_path=str(db_path), yaml_path=Path(yaml_path), dry_run=args.dry_run, actor=args.actor
        )
    except Exception as exc:  # noqa: BLE001 — this IS the top-level "could not run" boundary
        print(f"could not run: {exc}", file=sys.stderr)
        return 2

    _print_report(result)

    if not args.dry_run:
        _write_import_log(result, Path(args.log_path) if args.log_path else DEFAULT_LOG_PATH)

    return 1 if result.refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
