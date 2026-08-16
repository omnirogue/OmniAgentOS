"""Auto-dispatch: the machine/compute-pool bridge. MACHINE WORK ONLY.

Every 300 seconds (launchd template:
``configs/launchd/com.omniagentos.team-dispatch.plist``) one bounded pass
walks the pool in the store's own priority order and ENQUEUES up to
``OMNIAGENTOS_TEAM_AUTODISPATCH_CAP`` (default 3) compute-pool cards to the
local wq-server. OFF BY DEFAULT: without ``OMNIAGENTOS_TEAM_AUTODISPATCH=1``
the run is a clean no-op, exit 0.

**Nothing here assigns humans — v3 (the operator's ruling, 2026-08-13).** The shared
queue is drained by people, through Slack: ``/task claim <REF>`` (self-service)
or ``/task assign @name <REF>`` (the operator/Alice delegation). A pool card without a
compute-pool envelope is simply left where it is — no action, no event, no DM.
Points pace still renders in the hourly pulse (:mod:`omniagentos.team.points`
via :mod:`omniagentos.team.notify`); it influences no assignment, because
nothing assigns.

**Compute-pool cards route to machines.** A card whose
``org_json.dispatch.target == 'compute-pool'`` is enqueued: the unit spec
carries the company slug and the board task id in its labels/brief,
``idempotency_key`` is derived from the task id so a 300s cycle can never
enqueue the same card twice, and an unreachable server is LOG-AND-SKIP — the
next pass simply tries again. A dedupe hit (or a card whose enqueue event
already exists) never counts toward the cap, so already-enqueued cards at the
front of the FIFO pool cannot starve fresh ones.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.collab.store import CollabStore, append_task_event
from omniagentos.contracts import default_db_path
from omniagentos.team.store import TeamStore

__all__ = [
    "DEFAULT_CAP",
    "ENV_CAP",
    "ENV_GATE",
    "DispatchAction",
    "dispatch_once",
    "main",
]

ENV_GATE = "OMNIAGENTOS_TEAM_AUTODISPATCH"
ENV_CAP = "OMNIAGENTOS_TEAM_AUTODISPATCH_CAP"
DEFAULT_CAP = 3

# Compute-pool routing (org_json.dispatch.target).
COMPUTE_POOL_TARGET = "compute-pool"
WQ_SERVER_ENV = "WQ_SERVER"
DEFAULT_WQ_SERVER = "http://127.0.0.1:8487"
#: Defaults for a unit spec whose card's ``org_json.dispatch`` does not
#: override them. The repo defaults point at the serving checkout's project.
DEFAULT_REPO_URL = "https://github.com/Globex/OmniAgentOS.git"
DEFAULT_REPO_SLUG = "Globex/OmniAgentOS"
REPO_ROOT = Path("/Users/youruser/OmniAgentOS")

_ACTOR = "team-dispatch"


@dataclass
class DispatchAction:
    """One thing a pass did (or, dry-run, would do).

    ``employee_id`` is retained for log-shape compatibility with the pre-v3
    human-assignment dispatcher; it is always ``None`` now — nothing assigns
    humans.
    """

    task_id: str
    kind: str  # 'enqueue' | 'skip'
    employee_id: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "employee_id": self.employee_id,
            "detail": self.detail,
        }


def _dispatch_envelope(task: Mapping[str, Any]) -> dict[str, Any]:
    org = task.get("org")
    if not isinstance(org, dict):
        return {}
    envelope = org.get("dispatch")
    return envelope if isinstance(envelope, dict) else {}


def _resolve_base_sha(envelope: Mapping[str, Any]) -> str | None:
    """The unit's 40-hex base: the envelope's pin, else the serving repo's HEAD."""
    pinned = str(envelope.get("base_sha") or "")
    if pinned:
        return pinned
    try:
        out = subprocess.run(  # noqa: S603 -- fixed argv, read-only git
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"team-dispatch: could not resolve base_sha: {exc}", file=sys.stderr)
        return None
    sha = out.stdout.strip()
    return sha or None


def _unit_submit(
    task: Mapping[str, Any], envelope: Mapping[str, Any], base_sha: str
) -> dict[str, Any]:
    """The wq-server submit for one compute-pool card.

    ``idempotency_key`` is the board task id, so the 300s cycle re-submitting
    an already-enqueued card is the server's documented dedupe no-op. The
    company slug and the board task id ride the labels AND the brief — the
    labels for routing/reporting, the brief for the worker actually doing it.
    """
    task_id = str(task["id"])
    slug = task.get("company_slug")
    company = str(slug) if slug else "none"
    title = str(task.get("title") or "")
    acceptance = str(task.get("acceptance_criteria") or "")
    return {
        "idempotency_key": f"team-dispatch:{task_id}",
        "repo_url": str(envelope.get("repo_url") or DEFAULT_REPO_URL),
        "repo_slug": str(envelope.get("repo_slug") or DEFAULT_REPO_SLUG),
        "base_sha": base_sha,
        "base_ref": str(envelope.get("base_ref") or "main"),
        "branch": str(envelope.get("branch") or f"wq/team-dispatch/{task_id}"),
        # acceptance_cmd and owned_paths have NO fallback: a unit that
        # self-passes acceptance or owns '**' is fail-open, so the envelope
        # must name both or _enqueue_compute_pool refuses the card.
        "owned_paths": list(envelope["owned_paths"]),
        "agent_profile": str(envelope.get("agent_profile") or "default"),
        "acceptance_cmd": str(envelope["acceptance_cmd"]),
        "risk_class": str(envelope.get("risk_class") or "standard"),
        "submitted_by": str(envelope.get("submitted_by") or _ACTOR),
        "labels": [f"company:{company}", f"board_task:{task_id}"],
        "brief_inline": (
            f"[team-dispatch] {title}\n"
            f"board_task: {task_id}\n"
            f"company: {company}\n"
            f"acceptance_criteria: {acceptance}"
        ),
    }


def _enqueue_compute_pool(
    collab: CollabStore,
    task: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    wq_client: Any,
) -> DispatchAction:
    """Enqueue one compute-pool card. Unreachable server = log-and-skip.

    A dedupe hit (or a card whose enqueue event already exists) returns
    ``kind="skip"`` — only a FRESH enqueue may count toward the dispatch cap,
    otherwise already-enqueued cards sitting at the front of the FIFO pool
    would spend the whole cap every cycle and starve fresh machine work.
    """
    task_id = str(task["id"])
    refusal = _compute_pool_refusal(collab, task_id, envelope)
    if refusal is not None:
        return refusal
    base_sha = _resolve_base_sha(envelope)
    if base_sha is None:
        return DispatchAction(task_id=task_id, kind="skip", detail="no base_sha")
    submit = _unit_submit(task, envelope, base_sha)
    try:
        unit_id, deduped = wq_client.enqueue(submit)
    except Exception as exc:  # noqa: BLE001 -- machine pool down must not sink the pass
        print(f"team-dispatch: wq enqueue failed for {task_id}: {exc}", file=sys.stderr)
        return DispatchAction(task_id=task_id, kind="skip", detail=f"wq unreachable: {exc}")
    if deduped:
        return DispatchAction(task_id=task_id, kind="skip", detail=f"wq:{unit_id} (deduped)")

    def body(connection: Any) -> None:
        append_task_event(
            connection,
            task_id=task_id,
            actor=_ACTOR,
            event="comment",
            note=f"auto_dispatch to compute-pool wq:{unit_id}",
        )

    collab._store._execute_write_txn(body, op="team.auto_dispatch_enqueue")
    return DispatchAction(task_id=task_id, kind="enqueue", detail=f"wq:{unit_id}")


def _compute_pool_refusal(
    collab: CollabStore, task_id: str, envelope: Mapping[str, Any]
) -> DispatchAction | None:
    """The named refusal for an unenqueueable card, or None when it may go.

    Shared by the real path and dry-run so a preview never predicts an enqueue
    the real pass would refuse.
    """
    if _already_enqueued(collab, task_id):
        return DispatchAction(task_id=task_id, kind="skip", detail="already enqueued")
    if envelope.get("ready") is False:
        # An approved ``/task propose ... for ai`` card: routed to the pool, but
        # its executable spec is still missing. Named FIRST and named PLAINLY
        # because the two refusals below would report the symptom (no
        # acceptance_cmd) rather than the cause (nobody has written the spec
        # yet), and this class of card is expected to sit here until a
        # coordinator completes it — a skip somebody can read, not a mystery.
        return DispatchAction(
            task_id=task_id,
            kind="skip",
            detail="dispatch.ready=false — awaiting an executable spec "
            "(acceptance_cmd + owned_paths)",
        )
    if not str(envelope.get("acceptance_cmd") or "").strip():
        return DispatchAction(task_id=task_id, kind="skip", detail="no acceptance_cmd in envelope")
    owned = envelope.get("owned_paths")
    if not isinstance(owned, (list, tuple)) or not owned:
        # A bare string would iterate into single-character globs downstream.
        return DispatchAction(task_id=task_id, kind="skip", detail="no owned_paths in envelope")
    return None


def _already_enqueued(collab: CollabStore, task_id: str) -> bool:
    """True when a prior cycle already sent this card to the compute pool."""
    row = collab._connection.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND note LIKE 'auto_dispatch to compute-pool wq:%' LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def dispatch_once(
    collab: CollabStore,
    team: TeamStore,
    *,
    cap: int = DEFAULT_CAP,
    wq_client: Any = None,
    dry_run: bool = False,
) -> list[DispatchAction]:
    """One bounded MACHINE-ONLY dispatch pass. Returns what it did, in order.

    Idempotent per cycle by construction: the wq idempotency key makes a
    repeated enqueue a dedupe, the enqueue-event pre-check skips a wq call
    entirely, and ``cap`` bounds fresh enqueues either way.

    Human pool cards (no ``dispatch.target == 'compute-pool'`` envelope) are
    passed over silently — no action, no event. They belong to people, who
    claim or delegate them through the Slack ``/task`` commands.
    """
    actions: list[DispatchAction] = []
    dispatched = 0
    for card in team.pool_cards():
        if dispatched >= cap:
            break
        task = collab.get_board_task(card.id)
        if task is None:  # pragma: no cover -- pool read raced an archive
            continue
        task = dict(task)
        task["company_slug"] = card.company_slug

        envelope = _dispatch_envelope(task)
        if str(envelope.get("target") or "") != COMPUTE_POOL_TARGET:
            continue  # human work: never assigned by a daemon (v3)
        if dry_run:
            refusal = _compute_pool_refusal(collab, card.id, envelope)
            if refusal is not None:
                actions.append(refusal)
                continue
            actions.append(DispatchAction(task_id=card.id, kind="enqueue", detail="dry-run"))
            dispatched += 1
            continue
        client = wq_client if wq_client is not None else _default_wq_client()
        action = _enqueue_compute_pool(collab, task, envelope, wq_client=client)
        actions.append(action)
        if action.kind == "enqueue":
            dispatched += 1
    return actions


def _default_wq_client() -> Any:
    from omniagentos.workqueue.client import HttpQueueClient

    return HttpQueueClient(os.environ.get(WQ_SERVER_ENV) or DEFAULT_WQ_SERVER, timeout_s=5.0)


def _gate_on() -> bool:
    return str(os.environ.get(ENV_GATE) or "").strip().lower() in {"1", "true", "yes", "on"}


def _cap_from_env() -> int:
    raw = os.environ.get(ENV_CAP)
    try:
        cap = int(raw) if raw is not None else DEFAULT_CAP
    except ValueError:
        return DEFAULT_CAP
    return cap if cap > 0 else DEFAULT_CAP


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enqueue compute-pool board cards to the wq-server (machine work only)."
    )
    parser.add_argument("--once", action="store_true", help="run exactly one dispatch pass")
    parser.add_argument(
        "--dry-run", action="store_true", help="print would-be actions; write nothing"
    )
    parser.add_argument("--db", default=None, help="control-plane database path")
    parser.add_argument("--cap", type=int, default=None, help=f"override {ENV_CAP}")
    args = parser.parse_args(argv)

    if not args.once:
        parser.error("--once is required (launchd owns the cadence; there is no loop mode)")
    if not args.dry_run and not _gate_on():
        print(f"team-dispatch: {ENV_GATE} is off — no-op")
        return 0

    collab = CollabStore(args.db or os.environ.get("OMNIAGENTOS_DB") or default_db_path())
    team = TeamStore(collab._store)
    cap = args.cap if args.cap is not None and args.cap > 0 else _cap_from_env()

    actions = dispatch_once(collab, team, cap=cap, dry_run=args.dry_run)
    for action in actions:
        print(json.dumps(action.as_dict(), sort_keys=True))
    print(f"team-dispatch: {sum(1 for a in actions if a.kind != 'skip')} dispatched")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
