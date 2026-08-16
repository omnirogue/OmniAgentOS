"""Kill -9 drill child process (P1 harness pattern).

Runs ONE tick of one template in a fresh interpreter, optionally dying with
``os._exit(9)`` — no Python cleanup, no atexit, no checkpoint commit; the
power-loss equivalent — at a chosen instant:

``--crash-at effect``
    inside the tool, AFTER the irreversible external write but BEFORE the
    receipt is completed. Resume must find a claimed-but-uncompleted receipt and
    fail closed (state unknown), never re-run the effect.

``--crash-at post-receipt``
    after the receipt is completed but before the superstep commits — the exact
    window P7 measured as a DOUBLE side effect in the naive variant. Resume must
    replay the receipt and finish with the effect having happened once.

The external ledger file is the ground truth: one line per real effect. It lives
outside SQLite and outside the checkpoint on purpose, exactly like the git
merges / payments / outbound emails this guard exists to protect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omniagentos_loops import bootstrap_repo_path  # noqa: E402

bootstrap_repo_path()

from omniagentos_loops.runtime import LoopContext, run_once  # noqa: E402
from omniagentos_loops.templates import get_template  # noqa: E402
from template_kits import KITS  # noqa: E402

from omniagentos.db.store import SqliteStore  # noqa: E402
from omniagentos.policy import load_policy  # noqa: E402


class CrashingStore:
    """SqliteStore proxy that dies at a named observability instant."""

    def __init__(self, inner: Any, crash_on_action: str | None) -> None:
        self._inner = inner
        self._crash_on_action = crash_on_action

    def insert_event(self, *args: Any, **kwargs: Any) -> int:
        action = kwargs.get("action") or (args[2] if len(args) > 2 else "")
        if self._crash_on_action and action == self._crash_on_action:
            os._exit(9)
        return int(self._inner.insert_event(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def build_ctx(args: argparse.Namespace) -> LoopContext:
    kit = KITS[args.template]
    ledger = Path(args.ledger)

    def effect(**kwargs: Any) -> dict[str, Any]:
        # Optional hold, used by the concurrency test to keep this worker inside
        # its effect while a second worker starts and meets the lease.
        delay = float(os.environ.get("KILL_DRILL_EFFECT_DELAY_S") or 0)
        if delay:
            time.sleep(delay)
        # flush + fsync BEFORE the kill point. The ledger stands in for a real
        # irreversible effect (a sent email, a merged branch, a moved payment),
        # and a real one is durable the instant it happens. Relying on
        # CPython's close-on-exit flush would make the drill's evidence depend
        # on interpreter behaviour rather than on the guard under test, so the
        # bytes are forced to disk explicitly.
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(f"{kit.effect_tool}:{kit.effect_key(kwargs)}\n")  # IRREVERSIBLE
            handle.flush()
            os.fsync(handle.fileno())
        if args.crash_at == "effect":
            os._exit(9)
        return {"ok": True}

    inner = SqliteStore(args.db)
    store: Any = inner
    if args.crash_at == "post-receipt":
        store = CrashingStore(inner, "loop.effect.executed")

    return LoopContext(
        instance_id=args.instance,
        template=args.template,
        params=dict(kit.params),
        store=store,
        policy_cfg=load_policy(),
        tools=kit.registry(effect),
        notifier=lambda text: type("R", (), {"ok": True, "detail": "drill"})(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--instance", default="drill_loop")
    parser.add_argument("--template", default="draft_approve_send")
    parser.add_argument("--crash-at", default="", choices=["", "effect", "post-receipt"])
    args = parser.parse_args(argv)

    report = run_once(build_ctx(args), get_template(args.template))
    print(json.dumps(report.as_dict(), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
