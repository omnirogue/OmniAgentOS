"""CLI for Continuous Self-Improvement.

python -m omniagentos.csi run -r design
python -m omniagentos.csi run-all
python -m omniagentos.csi approve --run-id ID --db PATH
python -m omniagentos.csi implement --run-id ID --db PATH
python -m omniagentos.csi recover --run-id ID --db PATH
python -m omniagentos.csi cleanup --run-id ID --db PATH
"""

from __future__ import annotations

import argparse
import json
import sys

from omniagentos.csi.config import clear_csi_config_cache, load_csi_config
from omniagentos.csi.engine import CsiEngine
from omniagentos.csi.evidence import IMPLEMENTED_ROUTINES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniagentos.csi")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="run one CSI routine")
    p_run.add_argument(
        "--routine",
        "-r",
        required=True,
        help="design|self_learning|routing|skills|tool_calls|speed|quality|tech_debt",
    )
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--db", default=None)
    p_run.add_argument("--product-db", default=None)

    p_all = sub.add_parser("run-all", help="run all enabled implemented routines")
    p_all.add_argument("--force", action="store_true")
    p_all.add_argument("--db", default=None)
    p_all.add_argument("--product-db", default=None)

    p_sl = sub.add_parser("self-learning", help="alias for run -r self_learning")
    p_sl.add_argument("--force", action="store_true")
    p_sl.add_argument("--db", default=None)
    p_sl.add_argument("--product-db", default=None)

    p_appr = sub.add_parser("approve", help="approve a CSI run (native)")
    p_appr.add_argument("--run-id", required=True)
    p_appr.add_argument("--db", required=True)
    p_appr.add_argument("--by", default="operator")

    p_impl = sub.add_parser("implement", help="worktree implement (never merges)")
    p_impl.add_argument("--run-id", required=True)
    p_impl.add_argument("--db", required=True)
    p_impl.add_argument("--improvement-id", default=None)
    p_impl.add_argument("--repo-root", default=None)

    p_recover = sub.add_parser("recover", help="restore a retained CSI worktree")
    p_recover.add_argument("--run-id", required=True)
    p_recover.add_argument("--db", required=True)
    p_recover.add_argument("--repo-root", default=None)

    p_cleanup = sub.add_parser(
        "cleanup",
        help="remove only clean terminal worktrees and merged branches",
    )
    p_cleanup.add_argument("--run-id", required=True)
    p_cleanup.add_argument("--db", required=True)
    p_cleanup.add_argument("--repo-root", default=None)

    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] not in {
        "run",
        "run-all",
        "self-learning",
        "approve",
        "implement",
        "recover",
        "cleanup",
        "-h",
        "--help",
    }:
        tok = args_list[0].replace("-", "_")
        if tok in IMPLEMENTED_ROUTINES:
            args_list = ["run", "--routine", tok, *args_list[1:]]
        elif args_list[0].startswith("-"):
            args_list = ["run", "--routine", "self_learning", *args_list]

    args = parser.parse_args(args_list)
    if not args.cmd:
        parser.print_help()
        return 2

    clear_csi_config_cache()
    cfg = load_csi_config()

    def _engine() -> CsiEngine:
        return CsiEngine(
            config=cfg,
            db_path=getattr(args, "db", None),
            product_db_path=getattr(args, "product_db", None) or getattr(args, "db", None),
        )

    if args.cmd in {"run", "self-learning"}:
        rid = (
            "self_learning" if args.cmd == "self-learning" else str(args.routine).replace("-", "_")
        )
        result = _engine().run_routine(rid, force=bool(args.force))
        print(result.model_dump_json(indent=2))
        if result.verdict == "halted" and not args.force:
            return 3
        return 0

    if args.cmd == "run-all":
        engine = _engine()
        summary = []
        for rid, spec in sorted(cfg.routines.items()):
            if rid not in IMPLEMENTED_ROUTINES:
                print(f"=== {rid}: skipped (not implemented) ===", flush=True)
                continue
            if not spec.enabled and not args.force:
                print(f"=== {rid}: skipped (disabled) ===", flush=True)
                continue
            # Global enabled: no force. Global disabled: need --force.
            force = bool(args.force) if (not cfg.enabled or not spec.enabled) else False
            if not cfg.enabled and not args.force:
                print("=== halted: self_improvement.enabled is false (use --force) ===")
                return 3
            if not spec.enabled and args.force:
                force = True
            r = engine.run_routine(rid, force=force)
            summary.append(
                {
                    "routine": rid,
                    "run_id": r.run_id,
                    "status": r.status,
                    "verdict": r.verdict,
                    "no_change_reason": r.no_change_reason[:120] if r.no_change_reason else "",
                }
            )
            print(
                f"=== {rid}: {r.status} / {r.verdict} id={r.run_id} ===",
                flush=True,
            )
        print(json.dumps({"runs": summary}, indent=2))
        return 0

    if args.cmd == "approve":
        from omniagentos.csi.implement import approve_run

        ok = approve_run(run_id=args.run_id, db_path=args.db, approved_by=args.by)
        print(
            json.dumps(
                {
                    "ok": ok,
                    "run_id": args.run_id,
                    "approval": "approved" if ok else "failed",
                }
            )
        )
        return 0 if ok else 1

    if args.cmd == "implement":
        from omniagentos.csi.implement import implement_approved

        implement_out = implement_approved(
            run_id=args.run_id,
            improvement_id=args.improvement_id,
            db_path=args.db,
            repo_root=args.repo_root,
        )
        print(
            json.dumps(
                {
                    "ok": implement_out.ok,
                    "status": implement_out.status,
                    "branch": implement_out.branch,
                    "worktree": implement_out.worktree,
                    "written_paths": implement_out.written_paths,
                    "error": implement_out.error,
                },
                indent=2,
            )
        )
        return 0 if implement_out.ok else 1

    if args.cmd == "recover":
        from omniagentos.csi.implement import recover_implementation

        recover_out = recover_implementation(
            run_id=args.run_id,
            db_path=args.db,
            repo_root=args.repo_root,
        )
        print(
            json.dumps(
                {
                    "ok": recover_out.ok,
                    "status": recover_out.status,
                    "branch": recover_out.branch,
                    "worktree": recover_out.worktree,
                    "error": recover_out.error,
                },
                indent=2,
            )
        )
        return 0 if recover_out.ok else 1

    if args.cmd == "cleanup":
        from omniagentos.csi.implement import cleanup_implementation

        cleanup_out = cleanup_implementation(
            run_id=args.run_id,
            db_path=args.db,
            repo_root=args.repo_root,
        )
        print(
            json.dumps(
                {
                    "ok": cleanup_out.ok,
                    "action": cleanup_out.action,
                    "branch": cleanup_out.branch,
                    "worktree": cleanup_out.worktree,
                    "retained_reason": cleanup_out.retained_reason,
                    "error": cleanup_out.error,
                },
                indent=2,
            )
        )
        return 0 if cleanup_out.ok else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
