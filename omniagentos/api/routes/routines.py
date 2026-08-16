"""Routine builder HTTP surface: CRUD + enable/disable + run recording.

Every create/update goes through :func:`omniagentos.scheduler.routines
.validate_routine` inside :class:`omniagentos.scheduler.store.RoutinesStore`
— a routine missing its trigger, task template, objective gate, hard
stop-condition, or notification target is rejected with 400 before it ever
reaches SQLite (except ``status='disabled'`` drafts — LOOPS1-E2).

**D5 activation guard (LOOPS1-E6/E7):** every HTTP write that leaves a row
active runs proof-of-life: full as-active ``validate_routine`` + harness
resolution via ``resolve_adapter`` (refuses mock/unset) + real gate
execution via ``scheduler/gate_runner``. Fail closed; the DB row is
unchanged. ``set_status`` alone never activates — enable is the only
status-flip path and it is guarded here.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.adapters.registry import resolve_adapter
from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.contracts import _repo_root
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.gate_evidence import GateEvidenceError
from omniagentos.scheduler.gate_runner import (
    INTERPRETER_CLASS_LOOPS,
    LOOPS_SUBTREE,
    _run_process_group,
    _sanitized_env,
    default_loops_interpreter,
    interpreter_class_for_targets,
    parse_gate_command,
)
from omniagentos.scheduler.routines import (
    RoutineValidationError,
    _is_non_vacuous_gate_command,
    compute_next_run,
    validate_routine,
)
from omniagentos.scheduler.store import RoutinesStore

router = APIRouter(prefix="/api/routines", tags=["routines"])

# PATCH fields that re-trigger D5 when the row is (or stays) active.
_ENGINE_FIELDS = frozenset(
    {
        "task_template",
        "gate_type",
        "gate_config",
        "hard_cap_type",
        "hard_cap_value",
        "trigger_type",
        "trigger_config",
    }
)

_ACTIVATION_GATE_TIMEOUT_S = 120.0


class RoutineModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateRoutineRequest(RoutineModel):
    name: str
    description: str = ""
    trigger_type: str | None = None
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    task_template: dict[str, Any] = Field(default_factory=dict)
    gate_type: str | None = None
    gate_config: dict[str, Any] = Field(default_factory=dict)
    hard_cap_type: str | None = None
    hard_cap_value: float | None = None
    notification_target: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    scope: str | None = None
    purpose: str | None = None
    # M1 project axis (migration 116). Optional; omitted/blank creates an
    # UNSCOPED routine (NULL), which is this route's pre-M1 behaviour. A named
    # project must exist.
    project_id: str | None = None


class UpdateRoutineRequest(RoutineModel):
    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    trigger_config: dict[str, Any] | None = None
    task_template: dict[str, Any] | None = None
    gate_type: str | None = None
    gate_config: dict[str, Any] | None = None
    hard_cap_type: str | None = None
    hard_cap_value: float | None = None
    notification_target: dict[str, Any] | None = None
    status: str | None = None
    scope: str | None = None
    purpose: str | None = None


class RecordRunRequest(RoutineModel):
    run_id: str | None = None
    iteration: int = 1
    gate_passed: bool | None = None
    accepted: bool | None = None
    # ISSUE-8 (Sol review, seam 1): a `float = 0.0` default meant an omitted
    # cost_usd silently became a KNOWN, exact free run — the very "unknown
    # rendered as $0.00" defect this issue exists to remove, just at the
    # public write seam instead of the read path. `None` means genuinely
    # unknown; RoutinesStore.record_run (omniagentos/scheduler/store.py)
    # preserves it as such all the way to the rollup, exactly like the
    # dispatched-settlement path (routines_settle.py) already does.
    cost_usd: float | None = None
    stop_reason: str = ""
    notes: str = ""
    started_at: str | None = None
    finished_at: str | None = None


def _routines(store: StoreDep) -> RoutinesStore:
    return RoutinesStore(cast(SqliteStore, store))


def _get_or_404(routines: RoutinesStore, routine_id: str) -> dict[str, Any]:
    routine = routines.get_routine(routine_id)
    if routine is None:
        fail(404, "not_found", "routine not found", {"id": routine_id})
    return routine


def _validation_fail(exc: RoutineValidationError) -> NoReturn:
    fail(400, "validation", "routine is missing a required field", exc.errors)


def _resolve_project_id(store: StoreDep, project_id: str | None) -> str | None:
    """The project a new routine is scoped to, or ``None`` for unscoped (M1).

    NULL is the default and it is declared, not derived. A routine that names
    no project is UNSCOPED; it is not filed under a "default project", because
    the project axis feeds per-project spend and C11 grant binding, and a
    fabricated scope there is a fabricated authorization.

    A NAMED project must exist — migration 116's foreign key is live on this
    connection (``SqliteStore`` sets ``PRAGMA foreign_keys=ON``), so an unknown
    id would otherwise surface as a 500 instead of the 404 it is.
    """
    if project_id is None:
        return None
    normalized = project_id.strip()
    if not normalized:
        return None
    connection = cast(SqliteStore, store)._connection
    row = connection.execute("SELECT id FROM projects WHERE id = ?", (normalized,)).fetchone()
    if row is None:
        fail(404, "not_found", "unknown project", {"project_id": normalized})
    return normalized


def _serialize_routine(routine: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Attach computed ``next_run`` (and pass through scope/purpose)."""
    out = dict(routine)
    out["next_run"] = compute_next_run(routine, now=now)
    return out


def _is_pytest_argv(argv: list[str]) -> bool:
    """True when *argv* is a pytest invocation (bare or ``python -m pytest``)."""
    if not argv:
        return False
    if argv[0] == "pytest" or argv[0].endswith("/pytest"):
        return True
    # python -m pytest …
    for i, part in enumerate(argv):
        if part == "-m" and i + 1 < len(argv) and argv[i + 1] == "pytest":
            return True
    return False


def _junit_activation_counts(path: Path) -> tuple[int, int, int, int, int]:
    """Return ``(collected, passed, skipped, failed, errors)`` from JUnit XML."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return 0, 0, 0, 0, 0
    collected = passed = skipped = failed = errors = 0
    for testcase in root.iter("testcase"):
        collected += 1
        if testcase.find("skipped") is not None:
            skipped += 1
        elif testcase.find("error") is not None:
            errors += 1
        elif testcase.find("failure") is not None:
            failed += 1
        else:
            passed += 1
    return collected, passed, skipped, failed, errors


_PYTEST_SUMMARY_RE = re.compile(
    r"={3,}\s*(?P<body>.+?)\s+in\s+[0-9.]+\s*s",
    re.IGNORECASE,
)


def _parse_pytest_summary_counts(text: str) -> tuple[int, int, int, int, int] | None:
    """Best-effort parse of a pytest summary line into count tuple, or None."""
    match = _PYTEST_SUMMARY_RE.search(text or "")
    if match is None:
        # Quiet mode: "1 passed in 0.01s" / "2 skipped in 0.02s"
        quiet = re.search(
            r"(?P<body>\d+\s+\w+(?:,\s*\d+\s+\w+)*)\s+in\s+[0-9.]+\s*s",
            text or "",
            re.IGNORECASE,
        )
        if quiet is None:
            return None
        body = quiet.group("body")
    else:
        body = match.group("body")
    if re.search(r"no tests ran", body, re.IGNORECASE):
        return 0, 0, 0, 0, 0
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "errors": 0}
    for piece in body.split(","):
        piece = piece.strip()
        m = re.match(r"(\d+)\s+(\w+)", piece)
        if not m:
            continue
        n, kind = int(m.group(1)), m.group(2).lower()
        if kind in counts:
            counts[kind] = n
        elif kind.rstrip("s") + "s" in counts:
            counts[kind.rstrip("s") + "s"] = n
    errors = counts["error"] + counts["errors"]
    passed = counts["passed"]
    failed = counts["failed"]
    skipped = counts["skipped"]
    collected = passed + failed + skipped + errors
    return collected, passed, skipped, failed, errors


def _pytest_counts_prove_life(
    collected: int, passed: int, failed: int, errors: int, skipped: int
) -> tuple[bool, str]:
    """D5: exit 0 is not enough — require real collected+passed work."""
    if collected <= 0:
        return False, "gate refused: zero tests collected (no proof of life)"
    if passed <= 0 and skipped > 0 and failed == 0 and errors == 0:
        return (
            False,
            f"gate refused: all-skipped suite "
            f"(collected={collected}, passed={passed}, skipped={skipped})",
        )
    if passed <= 0:
        return (
            False,
            f"gate refused: passed={passed} (need passed > 0); "
            f"collected={collected}, failed={failed}, errors={errors}, skipped={skipped}",
        )
    if failed != 0:
        return (
            False,
            f"gate refused: failed={failed} (need failed == 0); "
            f"collected={collected}, passed={passed}",
        )
    if errors != 0:
        return (
            False,
            f"gate refused: errors={errors} (need errors == 0); "
            f"collected={collected}, passed={passed}",
        )
    return (
        True,
        f"gate passed with collected={collected}, passed={passed}, "
        f"failed={failed}, errors={errors}",
    )


def _run_activation_gate(routine: dict[str, Any]) -> tuple[bool, int | None, str]:
    """Execute the routine's gate command via gate_runner process containment.

    Returns ``(passed, exit_code, detail)``. Fail closed on anything other
    than a real exit code equal to the expected value *and*, for pytest
    gates, non-vacuous counts (collected > 0, passed > 0, failed == 0,
    errors == 0). An all-skipped or zero-collected suite is NOT proof of life
    even when pytest exits 0.
    """
    gate_type = routine.get("gate_type")
    gate_config = routine.get("gate_config") or {}
    if not isinstance(gate_config, dict):
        return False, None, "gate_config must be an object"
    if gate_type not in {"exit_code", "test_command", "merge_candidate"}:
        return (
            False,
            None,
            f"gate_type={gate_type!r} cannot be proof-executed at activation",
        )
    command = gate_config.get("command")
    if not isinstance(command, str) or not command.strip():
        return False, None, "gate_config.command is missing"
    if not _is_non_vacuous_gate_command(command):
        return False, None, "gate command is not a recognized objective verifier"

    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        return False, None, f"gate command could not be parsed: {exc}"
    if not parts:
        return False, None, "gate command is empty after parse"

    workspace = Path(_repo_root())

    # Prefer the running interpreter for `python -m …` / bare `pytest` so the
    # activation check does not depend on a system shim — EXCEPT for a pytest
    # gate whose targets live under `loops/`, which the production venv cannot
    # import (no LangGraph). The class is derived by the settlement path's own
    # function, deliberately: two rules would mean a gate that activates here and
    # then fails forever at settlement, or the reverse. Only `pytest` is
    # redirected — `ruff`/`mypy`/`pyright` are standalone tools that read
    # `loops/` fine from the production venv and are not installed in the loops
    # one. `_run_activation_gate` is fail-closed by contract, so an unresolvable
    # interpreter declines activation here rather than settling the NULL the
    # settlement path uses; declining to activate auto-pauses nothing.
    interpreter = sys.executable
    try:
        tool, gate_targets = parse_gate_command(command)
    except GateEvidenceError:
        tool, gate_targets = "", ()
    if tool == "pytest":
        try:
            interpreter_class = interpreter_class_for_targets(workspace, gate_targets)
        except GateEvidenceError as exc:
            return False, None, f"gate targets are not usable: {exc}"
        if interpreter_class == INTERPRETER_CLASS_LOOPS:
            loops_python = default_loops_interpreter()
            if not loops_python.is_file() or not os.access(loops_python, os.X_OK):
                return (
                    False,
                    None,
                    f"gate targets live under {LOOPS_SUBTREE}/ but the loops interpreter "
                    f"is missing or not executable: {loops_python}",
                )
            interpreter = str(loops_python)

    argv = list(parts)
    if argv[0] == "pytest":
        argv = [interpreter, "-m", "pytest", *argv[1:]]
    elif Path(argv[0]).name.startswith("python"):
        argv = [interpreter, *argv[1:]]

    try:
        env = _sanitized_env(workspace)
    except Exception as exc:  # pragma: no cover — workspace always exists in-repo
        return False, None, f"gate workspace unavailable: {exc}"
    # Keep pytest importable under the sanitized env.
    env["PATH"] = env.get("PATH") or ""
    env["PYTHONPATH"] = str(workspace)

    junit_path: Path | None = None
    is_pytest = _is_pytest_argv(argv)
    if is_pytest:
        # Counted proof: junit is authoritative for collected/passed/skipped.
        tmp = tempfile.NamedTemporaryFile(
            prefix="activation-gate-",
            suffix=".xml",
            delete=False,
        )
        junit_path = Path(tmp.name)
        tmp.close()
        argv = [*argv, f"--junitxml={junit_path}"]

    try:
        try:
            exit_code, stdout, stderr = _run_process_group(
                argv,
                cwd=str(workspace),
                env=env,
                timeout_seconds=_ACTIVATION_GATE_TIMEOUT_S,
            )
        except Exception as exc:
            return False, None, f"gate execution failed: {type(exc).__name__}: {exc}"

        expected = gate_config.get("expected_exit_code", 0)
        if not isinstance(expected, int) or isinstance(expected, bool):
            expected = 0
        if exit_code != expected:
            detail = f"gate failed: exit_code={exit_code} (expected {expected})" + (
                f"; stderr={stderr.strip()[:200]}" if stderr and stderr.strip() else ""
            )
            return False, exit_code, detail

        if is_pytest:
            collected = passed = skipped = failed = errors = 0
            if junit_path is not None and junit_path.is_file():
                collected, passed, skipped, failed, errors = _junit_activation_counts(junit_path)
            if collected == 0 and passed == 0:
                # Fall back to summary text when junit is empty/missing.
                parsed = _parse_pytest_summary_counts(stdout + "\n" + stderr)
                if parsed is not None:
                    collected, passed, skipped, failed, errors = parsed
            ok, detail = _pytest_counts_prove_life(collected, passed, failed, errors, skipped)
            if not ok:
                return False, exit_code, detail
            return True, exit_code, detail

        return True, exit_code, f"gate passed with exit_code={exit_code}"
    finally:
        if junit_path is not None:
            try:
                junit_path.unlink(missing_ok=True)
            except OSError:
                pass


def _assert_activation_harness(routine: dict[str, Any]) -> None:
    """Harness leg only — used on re-read before the atomic status flip."""
    template = routine.get("task_template") or {}
    harness = template.get("harness") if isinstance(template, dict) else None
    if not harness:
        fail(
            400,
            "activation_harness",
            "harness is unset — a real resolvable harness is required to activate",
            {"leg": "harness"},
        )
    if harness == "mock":
        fail(
            400,
            "activation_harness",
            "harness is mock — mock is dry-run only and cannot activate",
            {"leg": "harness", "harness": "mock"},
        )
    try:
        resolve_adapter(harness)
    except KeyError:
        fail(
            400,
            "activation_harness",
            f"harness is not resolvable: {harness!r}",
            {"leg": "harness", "harness": harness},
        )


def _assert_activation_ready(routine: dict[str, Any]) -> None:
    """D5 proof-of-life. Raises via :func:`fail` (4xx) on any leg failure.

    Legs, in order:
    1. full as-active ``validate_routine`` (draft fields can never activate)
    2. harness set, not ``mock``, resolvable via ``resolve_adapter``
    3. real gate command execution via gate_runner; pytest counts must prove life
    """
    as_active = {**routine, "status": "active"}
    try:
        validate_routine(as_active)
    except RoutineValidationError as exc:
        fail(
            400,
            "activation_validation",
            "routine cannot activate: missing required engine fields",
            {"leg": "validation", "errors": exc.errors},
        )

    _assert_activation_harness(routine)

    passed, exit_code, detail = _run_activation_gate(routine)
    if not passed:
        fail(
            400,
            "activation_gate",
            detail,
            {"leg": "gate", "exit_code": exit_code},
        )


@router.get("")
def list_routines(
    store: StoreDep, status: str | None = Query(default=None)
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [_serialize_routine(r, now=now) for r in _routines(store).list_routines(status=status)]


@router.post("", status_code=201)
def create_routine(body: CreateRoutineRequest, store: StoreDep) -> dict[str, Any]:
    """Create a routine.

    ``project_id`` (M1, optional) scopes it. Omitted means UNSCOPED (NULL) —
    this route's pre-M1 behaviour, and the only truthful value when the caller
    did not say which project the routine's work belongs to.
    """
    routines = _routines(store)
    payload = body.model_dump()
    payload["project_id"] = _resolve_project_id(store, payload.get("project_id"))
    # D5: create-with-active (explicit or the :41 default) must proof-of-life.
    if payload.get("status", "active") == "active":
        _assert_activation_ready(payload)
    try:
        created = routines.create_routine(payload)
    except RoutineValidationError as exc:
        _validation_fail(exc)
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return _serialize_routine(created, now=datetime.now(UTC))


class RecentRunItem(RoutineModel):
    routine_id: str
    routine_name: str
    run_id: str | None = None
    gate_passed: bool | None = None
    accepted: bool | None = None
    cost_usd: float | None = None
    finished_at: str | None = None


class RecentRunsResponse(RoutineModel):
    runs: list[RecentRunItem]


@router.get("/runs")
def list_recent_runs(
    store: StoreDep,
    limit: int = Query(default=50, ge=1, le=500),
) -> RecentRunsResponse:
    """Cross-routine recent runs aggregate. Registered BEFORE /{routine_id}
    so FastAPI does not capture ``runs`` as a routine id."""
    runs = _routines(store).list_recent_runs(limit=limit)
    return RecentRunsResponse(runs=[RecentRunItem(**r) for r in runs])


@router.get("/engine")
def _get_engine_heartbeat(store: StoreDep) -> dict[str, Any]:
    """Engine liveness heartbeat (LOOPS1-E3 / F3).

    Returns the latest ``routines.tick`` event timestamp from the API's own
    DB, or ``null`` when no tick has been observed. Registered BEFORE
    ``/{routine_id}`` so FastAPI does not capture ``engine`` as an id.
    Privatized (leading ``_``) so the reachability gate treats the FastAPI
    decorator registration as the sole surface — no unwired public symbol.
    """
    db = cast(SqliteStore, store)
    row = db._connection.execute(
        "SELECT MAX(ts) AS ts FROM events WHERE type = ?",
        ("routines.tick",),
    ).fetchone()
    last_tick_at = None if row is None else row["ts"]
    return {"last_tick_at": last_tick_at}


@router.get("/{routine_id}")
def get_routine(routine_id: str, store: StoreDep) -> dict[str, Any]:
    return _serialize_routine(_get_or_404(_routines(store), routine_id), now=datetime.now(UTC))


@router.patch("/{routine_id}")
def update_routine(routine_id: str, body: UpdateRoutineRequest, store: StoreDep) -> dict[str, Any]:
    """Partial update with D5 proof on every write that leaves the row active.

    Becoming-active and active-engine edits snapshot the integer ``revision``,
    run proof outside the DB write lock, then atomically update only if that
    revision is unchanged.
    """
    routines = _routines(store)
    current = _get_or_404(routines, routine_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = {**current, **fields}
    resulting_status = merged.get("status", current.get("status"))
    touches_engine = bool(_ENGINE_FIELDS.intersection(fields))
    becoming_active = resulting_status == "active" and current.get("status") != "active"
    editing_active_engine = (
        resulting_status == "active" and current.get("status") == "active" and touches_engine
    )
    needs_d5 = becoming_active or editing_active_engine

    if not needs_d5:
        try:
            updated = routines.update_routine(routine_id, fields)
        except RoutineValidationError as exc:
            _validation_fail(exc)
        if updated is None:
            fail(404, "not_found", "routine not found", {"id": routine_id})
        return _serialize_routine(updated, now=datetime.now(UTC))

    token = current.get("revision")
    if not isinstance(token, int) or isinstance(token, bool) or token < 0:
        fail(
            400,
            "activation_race",
            "routine has no integer revision concurrency token",
            {"leg": "concurrency"},
        )
    # Proof against the candidate merged row (includes requested field changes).
    _assert_activation_ready(merged)
    try:
        updated = routines.update_routine_cas(routine_id, fields, expected_revision=token)
    except RoutineValidationError as exc:
        _validation_fail(exc)
    if updated is None:
        fail(
            409,
            "activation_race",
            "routine was modified during activation proof — refuse update",
            {"leg": "concurrency", "expected_revision": token},
        )
    return _serialize_routine(updated, now=datetime.now(UTC))


@router.delete("/{routine_id}", status_code=204)
def delete_routine(routine_id: str, store: StoreDep) -> None:
    routines = _routines(store)
    if not routines.delete_routine(routine_id):
        fail(404, "not_found", "routine not found", {"id": routine_id})


@router.post("/{routine_id}/enable")
def enable_routine(routine_id: str, store: StoreDep) -> dict[str, Any]:
    """Flip a routine to active after D5 proof-of-life.

    Proof runs against the integer ``revision``. The status flip
    is a compare-and-swap on that token so a concurrent harness→mock mutation
    between proof and commit cannot leave ``status=active, harness=mock``.
    On any D5 failure or race the DB row stays disabled.
    """
    routines = _routines(store)
    current = _get_or_404(routines, routine_id)
    token = current.get("revision")
    if not isinstance(token, int) or isinstance(token, bool) or token < 0:
        fail(
            400,
            "activation_race",
            "routine has no integer revision concurrency token",
            {"leg": "concurrency"},
        )
    # D5 against the snapshotted row. set_status alone never activates safely.
    _assert_activation_ready(current)
    updated = routines.set_status_cas(routine_id, "active", expected_revision=token, reason="")
    if updated is None:
        fail(
            409,
            "activation_race",
            "routine was modified during activation proof — refuse flip",
            {"leg": "concurrency", "expected_revision": token},
        )
    return _serialize_routine(updated, now=datetime.now(UTC))


@router.post("/{routine_id}/disable")
def disable_routine(routine_id: str, store: StoreDep) -> dict[str, Any]:
    routines = _routines(store)
    _get_or_404(routines, routine_id)
    updated = routines.set_status(routine_id, "disabled", reason="")
    assert updated is not None
    return _serialize_routine(updated)


@router.get("/{routine_id}/runs")
def list_routine_runs(
    routine_id: str,
    store: StoreDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    routines = _routines(store)
    _get_or_404(routines, routine_id)
    return routines.list_runs(routine_id, limit=limit)


@router.post("/{routine_id}/runs", status_code=201)
def record_routine_run(routine_id: str, body: RecordRunRequest, store: StoreDep) -> dict[str, Any]:
    routines = _routines(store)
    _get_or_404(routines, routine_id)
    try:
        return routines.record_run(routine_id, body.model_dump())
    except ValueError as exc:
        fail(400, "validation", str(exc))
