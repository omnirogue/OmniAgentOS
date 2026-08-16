"""Command-line entry point for ``python -m omniagentos.runner``."""

from __future__ import annotations

import argparse
import logging
import os
import socket

from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.runner import Runner
from omniagentos.runner.core import real_harness_enabled

# The operator's standing cap on concurrent real agent workers. It applies ONLY
# when real-harness execution is armed (OMNIAGENTOS_REAL_HARNESS): armed runs
# spend money and hold real workspaces, so a pool started with a larger
# --concurrency is clamped down to this rather than trusted. Disarmed, the
# requested concurrency is used unchanged, exactly as before.
REAL_HARNESS_MAX_CONCURRENCY_ENV = "OMNIAGENTOS_REAL_HARNESS_MAX_CONCURRENCY"
DEFAULT_REAL_HARNESS_MAX_CONCURRENCY = 3


def resolve_concurrency(requested: int) -> int:
    """Clamp ``requested`` to the real-harness worker cap when armed.

    Disarmed -> ``requested`` untouched. Armed -> at most the cap (never raised
    above what was asked for), and never below 1. A non-numeric cap override
    falls back to the default rather than disabling the clamp.
    """
    if not real_harness_enabled():
        return requested
    raw = os.environ.get(REAL_HARNESS_MAX_CONCURRENCY_ENV, "").strip()
    try:
        cap = int(raw) if raw else DEFAULT_REAL_HARNESS_MAX_CONCURRENCY
    except ValueError:
        cap = DEFAULT_REAL_HARNESS_MAX_CONCURRENCY
    if cap < 1:
        cap = DEFAULT_REAL_HARNESS_MAX_CONCURRENCY
    return max(1, min(requested, cap))


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute queued OmniAgentOS runs")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--poll-ms", type=int, default=500)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("OMNIAGENTOS_RUNNER_CONCURRENCY", "1")),
        help="Runs this worker executes at once (default 1; the pool sets it higher).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    concurrency = resolve_concurrency(args.concurrency)
    if concurrency != args.concurrency:
        logging.getLogger(__name__).warning(
            "real-harness execution is armed: concurrency clamped %d -> %d",
            args.concurrency,
            concurrency,
        )
    Runner(
        SqliteStore(default_db_path()),
        args.worker_id,
        concurrency=concurrency,
    ).run_forever(poll_ms=args.poll_ms, once=args.once)


if __name__ == "__main__":
    main()
