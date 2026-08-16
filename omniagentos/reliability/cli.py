"""Command-line interface for the reliability worker tier (§11).

Every §11 command maps here: ``watch/audit/daily/weekly`` are the scheduled loops;
``recover`` runs one safe-recovery cycle; ``apply --improvement ID`` /
``rollback --improvement ID`` are the DETACHED worker entry points the W7 routes
spawn after a human decision (route does the CAS to ``approved`` + spawns this);
``scorecards`` / ``status`` are read-only introspection.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import TYPE_CHECKING

from omniagentos.contracts import default_db_path
from omniagentos.reliability.audit import daily_summary, twice_daily, watch, weekly_architecture
from omniagentos.reliability.store import SqliteReliabilityStore

if TYPE_CHECKING:
    from omniagentos.reliability.pipeline import ApplyResult, ImprovementPipeline


def _pipeline(store: SqliteReliabilityStore, repo_root: str) -> ImprovementPipeline:
    from omniagentos.reliability.pipeline import ImprovementPipeline

    return ImprovementPipeline(store, repo_root=repo_root)


# Which audit function each --kind selects (pinned RF2 contract).
_KIND_FNS = {
    "watch": watch,
    "twice_daily": twice_daily,
    "daily_summary": daily_summary,
    "weekly_architecture": weekly_architecture,
}


def _log_path_for(command: str, improvement_id: str, repo_root: str) -> str:
    return os.path.join(repo_root, "var", "log", f"reliability-{command}-{improvement_id}.log")


def main(argv: list[str] | None = None) -> int:
    import logging
    import warnings

    warnings.warn(
        "DEPRECATION WARNING: The reliability CLI is frozen and deprecated. It will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    logging.getLogger("omniagentos").warning(
        "DEPRECATION WARNING: The reliability CLI is frozen and deprecated. It will be removed in a future release."
    )
    parser = argparse.ArgumentParser(prog="python -m omniagentos.reliability")
    parser.add_argument(
        "command",
        choices=[
            "watch",
            "audit",
            "daily",
            "weekly",
            "recover",
            "apply",
            "rollback",
            "redrive",
            "scorecards",
            "status",
        ],
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--improvement", default=None, help="improvement id (apply/rollback)")
    parser.add_argument(
        "--kind",
        default=None,
        choices=sorted(_KIND_FNS),
        help="audit kind to run (for the 'audit' command); defaults to twice_daily",
    )
    parser.add_argument(
        "--audit-id",
        default=None,
        help="run a pre-created queued audit row instead of minting a new one",
    )
    parser.add_argument("--reason", default="manual", help="rollback reason")
    parser.add_argument("--repo-root", default=os.getcwd(), help="git repo root for apply/rollback")
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=None,
        help="redrive: only re-drive approved rows older than this many seconds",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help=(
            "redrive: escalate to failed after this many FAILED attempts "
            "(spawn/worker/apply failures). Soft deferrals — monitoring overlap, apply "
            "lease contention — are bounded by their own horizon and do not count here."
        ),
    )
    parser.add_argument(
        "--inline",
        action="store_true",
        help="redrive: run apply in-process instead of re-spawning a detached worker",
    )
    args = parser.parse_args(argv)

    store = SqliteReliabilityStore(args.db)
    audits = {
        "watch": watch,
        "audit": twice_daily,
        "daily": daily_summary,
        "weekly": weekly_architecture,
    }

    if args.command in audits:
        # 'audit --kind K' selects K; the bare command keeps its historical mapping.
        fn = (
            _KIND_FNS[args.kind]
            if (args.command == "audit" and args.kind)
            else audits[args.command]
        )
        # repo_root lets watch/twice_daily hydrate the real ImprovementPipeline
        # (sandbox/judges/monitoring); env OMNIAGENTOS_RELIABILITY_LLM='0' skips LLM stages.
        kwargs: dict[str, object] = {"vault_dir": args.vault_dir, "repo_root": args.repo_root}
        if fn is not watch:
            # Production wiring: department/CTO review stages engage only when a
            # company-style adapter is provided (tests inject their own or omit it).
            from omniagentos.orgdims.company_init import default_adapter_fn

            kwargs["company_adapter_fn"] = default_adapter_fn
        out = fn(store, once=args.once, audit_id=args.audit_id, **kwargs)
        print(json.dumps(out, sort_keys=True, default=str))
    elif args.command == "recover":
        from omniagentos.reliability.recovery import run_recovery_cycle

        print(json.dumps(run_recovery_cycle(store), sort_keys=True))
    elif args.command == "apply":
        if not args.improvement:
            raise SystemExit("apply requires --improvement ID")
        from omniagentos.reliability.worker_drive import (
            OUTCOME_APPLIED,
            OUTCOME_DEFERRED,
            OUTCOME_FAILED,
            record_worker_exit,
        )

        log_path = _log_path_for("apply", args.improvement, args.repo_root)
        try:
            result = _pipeline(store, args.repo_root).apply(args.improvement)
        except Exception as exc:  # noqa: BLE001 — durable exit evidence before dying
            record_worker_exit(
                store,
                args.improvement,
                exit_code=1,
                outcome=OUTCOME_FAILED,
                reason="apply_exception",
                log_path=log_path,
                error=str(exc),
            )
            raise
        if result.applied:
            record_worker_exit(
                store,
                args.improvement,
                exit_code=0,
                outcome=OUTCOME_APPLIED,
                reason=result.reason or "applied",
                applied_sha=result.applied_sha,
                log_path=log_path,
            )
            code = 0
        elif result.deferred:
            record_worker_exit(
                store,
                args.improvement,
                exit_code=0,
                outcome=OUTCOME_DEFERRED,
                reason=result.reason or "deferred",
                deferred=True,
                log_path=log_path,
            )
            code = 0
        else:
            record_worker_exit(
                store,
                args.improvement,
                exit_code=1,
                outcome=OUTCOME_FAILED,
                reason=result.reason or "apply_failed",
                log_path=log_path,
            )
            code = 1
        print(json.dumps(result.__dict__, sort_keys=True, default=str))
        return code
    elif args.command == "rollback":
        if not args.improvement:
            raise SystemExit("rollback requires --improvement ID")
        from omniagentos.reliability.worker_drive import (
            OUTCOME_FAILED,
            record_worker_exit,
        )

        log_path = _log_path_for("rollback", args.improvement, args.repo_root)
        try:
            rollback_result = _pipeline(store, args.repo_root).rollback(
                args.improvement, reason=args.reason, decided_by="cli"
            )
        except Exception as exc:  # noqa: BLE001
            record_worker_exit(
                store,
                args.improvement,
                exit_code=1,
                outcome=OUTCOME_FAILED,
                reason="rollback_exception",
                log_path=log_path,
                error=str(exc),
            )
            raise
        record_worker_exit(
            store,
            args.improvement,
            exit_code=0 if rollback_result.success else 1,
            outcome="rolled_back" if rollback_result.success else OUTCOME_FAILED,
            reason=rollback_result.reason
            or ("rolled_back" if rollback_result.success else "rollback_failed"),
            log_path=log_path,
        )
        print(json.dumps(rollback_result.__dict__, sort_keys=True, default=str))
        return 0 if rollback_result.success else 1
    elif args.command == "redrive":
        # H-07: CAS/lease-guarded re-driver for stranded approved rows.
        from omniagentos.reliability.worker_drive import (
            DEFAULT_GRACE_SECONDS,
            DEFAULT_MAX_ATTEMPTS,
            make_worker_spawn_fn,
            redrive_stranded_approvals,
        )

        grace = DEFAULT_GRACE_SECONDS if args.grace_seconds is None else args.grace_seconds
        max_attempts = DEFAULT_MAX_ATTEMPTS if args.max_attempts is None else args.max_attempts
        pipeline = _pipeline(store, args.repo_root) if args.inline else None

        def _inline_apply(imp_id: str) -> ApplyResult:
            assert pipeline is not None
            return pipeline.apply(imp_id)

        # Same spawn helper the scheduled re-drive uses, so manual and automatic
        # re-drives cannot diverge on interpreter/module/cwd/log semantics (H-06).
        out = redrive_stranded_approvals(
            store,
            apply_fn=_inline_apply if args.inline else None,
            spawn_fn=(
                None
                if args.inline
                else make_worker_spawn_fn(store, repo_root=args.repo_root, db_path=args.db)
            ),
            grace_seconds=grace,
            max_attempts=max_attempts,
            owner=f"cli-redrive:{os.getpid()}",
        )
        print(json.dumps(out, sort_keys=True, default=str))
    elif args.command == "scorecards":
        cards = store.list_scorecards(limit=200)
        print(json.dumps([c.__dict__ for c in cards], sort_keys=True, default=str))
    elif args.command == "status":
        print(
            json.dumps(
                {
                    "audits": [a.__dict__ for a in store.list_audits(limit=20)],
                    "events": len(store.list_events(limit=1000)),
                },
                default=str,
            )
        )
    return 0
