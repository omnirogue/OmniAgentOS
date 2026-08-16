"""The ONE production seam for the LangGraph loop runtime.

A loop is registered as an ordinary routine row whose ``task_template.input``
names this module (``module = "omniagentos.loops"``). On a due tick,
:func:`run_loop_job` launches ``loops/bin/loop-worker`` — a short-lived child in
the loops venv — and turns its JSON report into a ``BuiltinResult``.

Why a subprocess and not an import: the loop runtime needs LangGraph, and
``pyproject.toml`` deliberately does not (Option G, MIGRATION_ARCHITECTURE.md).
Keeping the boundary at ``subprocess`` is what lets the production dependency
set stay untouched and makes rollback ``rm -rf var/loops``.

Everything this module accepts from the database is validated against an
anchored identifier pattern before it becomes argv: a routine row is data, and
an unconstrained name in a scheduler-launched command is arbitrary execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from omniagentos.contracts import DASHBOARD_ORIGIN
from omniagentos.scheduler.builtin_jobs import BuiltinResult
from omniagentos.scheduler.loop_budget import LoopBudgetLedger
from omniagentos.scheduler.loop_effects import EffectServer
from omniagentos.scheduler.routines import (
    OUTCOME_ADVERSE,
    OUTCOME_FAVOURABLE,
    OUTCOME_NEUTRAL,
)

logger = logging.getLogger(__name__)

#: ``task_template.input.module`` that routes a routine to the loop runtime.
LOOP_MODULE = "omniagentos.loops"

#: Mirrors ``omniagentos_loops.paths.SAFE_NAME_RE``. Duplicated on purpose: this
#: module runs in the production venv and must never import the loops package.
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Mirrors ``omniagentos_loops.worker.INSTANCE_MODULE_PREFIX`` (and the same
#: constant in ``omniagentos_loops.registry``). A loop instance registers its
#: tools from this module, and the row's value becomes an ``importlib`` argument
#: inside the worker — so the prefix is a security boundary, and the anchored
#: pattern below is what keeps a database row from naming an arbitrary import.
INSTANCE_MODULE_PREFIX = "omniagentos_loops.instances."
INSTANCE_MODULE_RE = re.compile(
    r"^omniagentos_loops\.instances\.[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$"
)

DEFAULT_TIMEOUT_S = 600
MIN_TIMEOUT_S = 30
MAX_TIMEOUT_S = 3600

#: Loop statuses that produced the result the loop exists to produce. This set
#: is the acceptance NUMERATOR and nothing else belongs in it.
#:
#: ``parked`` and ``idle`` were once here. That was the defect: a loop parking
#: the same approval every tick, healing nothing, reported acceptance 1.0 and
#: could therefore never trip the auto-pause floor — a dead loop and a working
#: loop were indistinguishable (live proof: rtn_1e5567b9f3314a2c9d76,
#: total_runs=2 accepted_runs=2 acceptance_rate=1.0, two parks, zero heals).
FAVOURABLE_STATUSES = frozenset({"completed"})

#: Loop statuses that mean the tick BEHAVED but produced no judgeable result.
#: Excluded from the acceptance denominator ENTIRELY — neither success nor
#: failure. Scoring these unfavourable is the opposite defect, and it auto-paused
#: this repo's routines four times on 2026-07-31.
#:
#: Parking for a human is the system working, so it must never be punished; but
#: it must also never be rewarded, because "parked forever" and "parked once,
#: correctly" have to be distinguishable. Neutrality removes the false 100% and
#: leaves the visibility question to a reader that can count consecutive
#: non-results (each one is durably recorded with its own stop_reason).
NEUTRAL_STATUSES = frozenset({"parked", "idle"})

#: Loop statuses that count AGAINST the floor. ``aborted`` (policy denial,
#: rejected/expired approval) and ``failed`` (execution error) were always here.
#:
#: ``blocked`` is the addition the taxonomy exists for: a non-result the SYSTEM
#: caused and can act on — a dead credential, a revoked grant, a persistent
#: authorization failure. It looks like ``idle`` from the outside (no work got
#: done) and used to be reported as one, which is precisely how a loop with dead
#: credentials could idle green forever. It is adverse because, unlike a human's
#: pending decision, there is something the system can do about it, and the
#: auto-pause floor is how an operator gets told.
ADVERSE_STATUSES = frozenset({"aborted", "blocked", "failed"})

#: Machine-readable ``routine_runs.stop_reason`` per loop status. The class
#: alone would not tell an operator whether a routine is waiting on THEM or
#: stuck on a dead credential; these codes are what make that queryable.
STATUS_STOP_REASONS: dict[str, str] = {
    "completed": "",
    "parked": "loop_parked_awaiting_human",
    "idle": "loop_idle_no_work",
    "blocked": "loop_blocked",
    "aborted": "loop_aborted",
    "failed": "loop_failed",
}

#: Retained so a reader (and the counterfeit corpus) can still ask "which loop
#: statuses count as acceptance". It is an alias, not a second authority.
ACCEPTED_STATUSES = FAVOURABLE_STATUSES


def classify_loop_status(status: str) -> tuple[str, str]:
    """Map one loop status to ``(outcome_class, stop_reason)``.

    An unrecognised status is ADVERSE, deliberately. The alternative — treating
    an unknown status as neutral or favourable — means a worker that starts
    reporting a status this scheduler has never heard of silently stops being
    judged, which is the same invisibility the taxonomy is closing.
    """
    if status in FAVOURABLE_STATUSES:
        return OUTCOME_FAVOURABLE, STATUS_STOP_REASONS.get(status, "")
    if status in NEUTRAL_STATUSES:
        return OUTCOME_NEUTRAL, STATUS_STOP_REASONS[status]
    if status in ADVERSE_STATUSES:
        return OUTCOME_ADVERSE, STATUS_STOP_REASONS[status]
    return OUTCOME_ADVERSE, "loop_status_unrecognized"

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Non-secret variables the worker genuinely needs on top of the house hygiene
#: allowlist. Enumerated in code, not config: this list is the loop runtime's
#: entire window onto the parent environment, and it must be reviewable in one
#: glance. Nothing here is, or may become, a credential — and
#: :func:`_worker_env` proves that mechanically by running this list through the
#: same credential-shape filter it applies to everything else.
_WORKER_ENV_PASSTHROUGH = (
    "OMNIAGENTOS_LOOPS_ROOT",
    "OMNIAGENTOS_LOOPS_VENV",
    "OMNIAGENTOS_VAR_DIR",
    "OMNIAGENTOS_VAR",
    "OMNIAGENTOS_DASHBOARD_ORIGIN",
)

def _is_credential_shaped(name: str) -> bool:
    """Use the shared separator- and case-insensitive credential-shape check."""
    # Keep this import local: scheduler module import must not load the adapter
    # stack unless the loop-worker environment is actually being built.
    from omniagentos.adapters.common import _is_credential_shaped_env_name

    return _is_credential_shaped_env_name(name)


def _loop_worker_path() -> Path:
    return _REPO_ROOT / "loops" / "bin" / "loop-worker"


def _worker_env() -> dict[str, str]:
    """The environment the loop worker runs with — scrubbed, not inherited.

    The scheduler process is launched by ``scripts/launch-env.sh`` and therefore
    holds every value in ``connections.env``: payment rails, bank tokens,
    model-provider keys, the operator token. Handing that wholesale to a
    subprocess would make the loops venv a security boundary in name only, so
    the worker gets the SAME allowlist every other spawn path in this repo uses
    (``adapters.common._scrubbed_env``: credential-shaped names force-denied,
    then allowlisted with a post-prefix credential-shape closure), plus the
    non-secret pointers above, and THEN a loops-only post-filter that denies
    every remaining credential-shaped name. The final sweep is deliberately
    retained as defense in depth against any future source of worker env values.

    The enumerated pointers are filtered too. That is what keeps the exemption
    honest: a future edit cannot smuggle a credential into the worker by adding
    it to :data:`_WORKER_ENV_PASSTHROUGH`.

    Consequences, both deliberate: a loop tool cannot resolve a connector
    credential from the environment, and the worker cannot post to Slack — which
    is why approval paging happens in this process (see :func:`_deliver_page`).
    A loop that genuinely needs a connector must reach it through a control-plane
    seam, not through an inherited variable.

    That seam now exists for the general case
    (:mod:`omniagentos.scheduler.loop_effects`), and it deliberately does NOT
    appear here: its per-tick socket path is minted by this process and handed
    to the worker in ARGV, so :data:`_WORKER_ENV_PASSTHROUGH` stays exactly the
    five non-credential pointers it has always been and the filter below is
    never asked to make an exception.
    """
    from omniagentos.adapters.common import _scrubbed_env

    env = _scrubbed_env()
    for name in _WORKER_ENV_PASSTHROUGH:
        if _is_credential_shaped(name):
            continue
        value = os.environ.get(name)
        if value:
            env[name] = value
    for name in [key for key in env if _is_credential_shaped(key)]:
        del env[name]
    env.setdefault("PATH", os.defpath)
    return env


def _db_path_from_store(store: Any) -> str:
    """The DB path the tick is already bound to.

    Exact idiom from ``builtin_jobs._db_path_from_store``: without it the worker
    falls back to the ambient ``default_db_path()`` and a programmatic tick
    writes its approvals and receipts into a DIFFERENT database than the store
    it was handed — which is precisely the split the environment scrub exposed.
    """
    connection = getattr(store, "_connection", None)
    if connection is None:
        return ""
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except Exception:  # noqa: BLE001
        return ""
    if row is None:
        return ""
    return str(row[2])


def _spec(task_template: dict[str, Any]) -> dict[str, Any] | None:
    """Validated (template, instance_id, params, timeout_s), or None."""
    payload = (task_template or {}).get("input") or {}
    if payload.get("module") != LOOP_MODULE:
        return None
    template = str(payload.get("template") or "")
    instance_id = str(payload.get("instance_id") or "")
    if not SAFE_NAME_RE.match(template) or not SAFE_NAME_RE.match(instance_id):
        raise ValueError(f"invalid loop template/instance: {template!r}/{instance_id!r}")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("loop params must be an object")
    module = str(payload.get("instance_module") or "").strip()
    if not module:
        # A loop instance's tools come ONLY from its instance module; this
        # runtime ships none. A row that names no module therefore cannot pass
        # any template's required_tools check, and every tick reports
        # "instance is missing required tools: [...]" — which is precisely what
        # rtn_1e5567b9f3314a2c9d76 did every ten minutes on 2026-08-01, because
        # the public seeding helper could not express the field. Refuse the row
        # by name instead of spawning a worker that is certain to fail.
        raise ValueError(
            "loop routine names no instance_module "
            f"(expected a module under {INSTANCE_MODULE_PREFIX})"
        )
    if not INSTANCE_MODULE_RE.match(module):
        raise ValueError(f"invalid loop instance module: {module!r}")
    try:
        timeout_s = int(payload.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        raise ValueError("loop timeout_s must be an integer") from None
    return {
        "template": template,
        "instance_id": instance_id,
        "params": params,
        "instance_module": module,
        "timeout_s": max(MIN_TIMEOUT_S, min(timeout_s, MAX_TIMEOUT_S)),
        "project_id": str((task_template or {}).get("project_id") or ""),
    }


def _report(stdout: str) -> dict[str, Any]:
    """Parse the worker's JSON report (its LAST stdout line)."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
    raise ValueError("loop worker produced no JSON report")


def run_loop_job(store: Any, *, task_template: dict[str, Any] | None = None) -> BuiltinResult:
    """Run one tick of the loop instance named by *task_template*.

    Never raises: a routine tick must survive a broken loop the same way it
    survives a broken board sweep.
    """
    try:
        spec = _spec(task_template or {})
    except ValueError as exc:
        return BuiltinResult(accepted=False, notes=f"invalid loop routine: {exc}")
    if spec is None:
        return BuiltinResult(accepted=False, notes="routine is not a loop row")

    worker = _loop_worker_path()
    if not worker.is_file():
        return BuiltinResult(
            accepted=False,
            notes=(f"loop runtime is not installed ({worker} missing); see loops/README.md"),
        )

    db_path = _db_path_from_store(store)
    argv = [
        str(worker),
        "--template",
        spec["template"],
        "--instance",
        spec["instance_id"],
        "--params",
        json.dumps(spec["params"], sort_keys=True),
    ]
    if db_path:
        argv += ["--db", db_path]
    # Unconditional: _spec() has already refused an empty module, so an omitted
    # flag here could only mean the guard above was removed.
    argv += ["--instance-module", spec["instance_module"]]
    if spec["project_id"]:
        argv += ["--project-id", spec["project_id"]]

    # The credential seam for THIS tick: a private AF_UNIX socket served by this
    # process, which holds the credentials legitimately. The worker declares a
    # typed capability across it; the parent authorizes, executes and audits.
    # Same argument as _deliver_page, generalised — the secret never crosses the
    # process boundary. A seam that fails to start yields an empty path, the
    # worker is simply told there is none, and every credentialed capability
    # answers "unavailable" (absence, not failure).
    # Create budget ledger for this tick
    budget_ledger = None
    if db_path:
        try:
            budget_ledger = LoopBudgetLedger(db_path)
        except Exception:  # noqa: BLE001 - ledger creation failure should not block tick
            logger.exception("could not create budget ledger; spend caps will be unavailable")
    with EffectServer(db_path=db_path, budget_ledger=budget_ledger) as seam:
        socket_path = seam.path
        if socket_path:
            argv += ["--effect-socket", socket_path]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=spec["timeout_s"],
                cwd=str(_REPO_ROOT),
                env=_worker_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return BuiltinResult(
                accepted=False,
                notes=f"loop {spec['instance_id']} exceeded {spec['timeout_s']}s; killed",
            )
        except OSError as exc:
            return BuiltinResult(accepted=False, notes=f"loop worker could not start: {exc}")

    try:
        report = _report(completed.stdout)
    except ValueError:
        tail = (completed.stderr or "").strip()[-400:]
        return BuiltinResult(
            accepted=False,
            notes=f"loop {spec['instance_id']} rc={completed.returncode}: {tail or 'no output'}",
        )

    status = str(report.get("status") or "")
    detail = str(report.get("detail") or "")
    # The node the tick reached. Prose, but load-bearing prose: "parked" alone
    # does not tell an operator WHERE, and the stage is what turns a routine_runs
    # row into something a terminal can render.
    stage = str(report.get("stage") or "")
    notes = (
        f"loop {spec['instance_id']} ({spec['template']}) {status}"
        f"{' @' + stage if stage else ''}{': ' + detail if detail else ''}"
    )
    if not status:
        return BuiltinResult(accepted=False, notes=f"{notes} — worker reported no status")

    if status == "parked":
        _deliver_page(store, str(report.get("approval_id") or ""))

    outcome, stop_reason = classify_loop_status(status)
    return BuiltinResult(
        accepted=outcome == OUTCOME_FAVOURABLE,
        notes=notes[:1000],
        outcome=outcome,
        reason=stop_reason,
        # The worker's word, kept verbatim and kept SEPARATE from the verdict.
        # `accepted` above is no longer what reaches `routine_runs.gate_passed`
        # — settlement executes the routine's declared gate and writes that —
        # so this is the column an operator (or a counterfeit) reads to ask
        # whether a loop's claim and its gate ever disagreed.
        self_report=status,
    )


def _deliver_page(store: Any, approval_row_id: str) -> bool:
    """Page a human about a parked loop approval, from THIS process.

    The worker cannot do it: its environment is scrubbed, and the Slack webhook
    URL is itself the credential. The scheduler already holds that secret
    legitimately, so delivery belongs here — the secret never crosses the
    process boundary, and the audit's hard lesson (15/16 approvals expiring
    undecided, unseen) is still answered.

    Idempotent: the worker writes ``loop.approval.paged`` when it records the
    request, and this writes ``loop.approval.delivered`` once — and ONLY on a
    delivery that actually succeeded. That asymmetry is the whole point. The
    already-delivered probe below is what stops a parked loop paging every tick,
    so recording a ``delivered=False`` event on a transient Slack failure made
    that one failure permanent: every later tick found the event, believed a
    human had been paged, and returned early. The request then expired unseen —
    the exact audit failure (15/16 approvals expiring undecided) that paging
    exists to prevent. A failure now records nothing, so the next tick retries.
    """
    if not approval_row_id or store is None:
        return False
    try:
        connection = store._connection
        already = connection.execute(
            "SELECT 1 FROM events WHERE type = 'loop.approval' "
            "AND action = 'loop.approval.delivered' AND target_id = ? LIMIT 1",
            (approval_row_id,),
        ).fetchone()
        if already is not None:
            return False
        row = connection.execute(
            "SELECT * FROM approvals WHERE id = ? AND state = 'pending'",
            (approval_row_id,),
        ).fetchone()
        if row is None:
            return False
        text = _page_text(dict(row), approval_row_id)
    except Exception:  # noqa: BLE001 — paging must never fail a tick
        logger.exception("could not build the loop approval page")
        return False

    try:
        from omniagentos.steward.notify import send_slack

        result = send_slack(text)
        delivered = bool(getattr(result, "ok", False))
        detail = str(getattr(result, "detail", ""))
    except Exception as exc:  # noqa: BLE001
        delivered, detail = False, f"{type(exc).__name__}"

    if not delivered:
        # Record NOTHING: an undelivered page is not a page. The absence of the
        # event is what lets the next tick try again.
        logger.warning(
            "loop approval %s was not paged (retrying next tick): %s", approval_row_id, detail
        )
        return False

    try:
        store.insert_event(
            type="loop.approval",
            actor="scheduler",
            action="loop.approval.delivered",
            target_type="approval",
            target_id=approval_row_id,
            payload={"delivered": True, "detail": detail},
        )
    except Exception:  # noqa: BLE001
        # The page WAS delivered; only the record failed. Erring towards a
        # duplicate page on the next tick beats erring towards silence.
        logger.exception("could not record the loop approval page")
    return True


def _page_text(row: dict[str, Any], approval_row_id: str) -> str:
    try:
        params = json.loads(str(row.get("params_json") or "{}"))
    except ValueError:
        params = {}
    origin = os.environ.get("OMNIAGENTOS_DASHBOARD_ORIGIN") or DASHBOARD_ORIGIN
    return (
        f":lock: *Loop approval needed* — `{params.get('loop_instance', '?')}` / "
        f"`{params.get('node', '?')}`\n"
        f"tool `{params.get('tool', '?')}` (tier {params.get('tier', '?')}, "
        f"{row.get('action_class', '?')})\n"
        f"{row.get('evidence', '')}\n"
        f"expires {row.get('expires_at', '?')}\n"
        f"{origin.rstrip('/')}/approvals?approval={approval_row_id}"
    )


__all__ = [
    "ACCEPTED_STATUSES",
    "ADVERSE_STATUSES",
    "DEFAULT_TIMEOUT_S",
    "FAVOURABLE_STATUSES",
    "INSTANCE_MODULE_PREFIX",
    "INSTANCE_MODULE_RE",
    "LOOP_MODULE",
    "NEUTRAL_STATUSES",
    "SAFE_NAME_RE",
    "STATUS_STOP_REASONS",
    "classify_loop_status",
    "run_loop_job",
]
