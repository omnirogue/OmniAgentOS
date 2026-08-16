"""CLI entry point: ``python -m omniagentos.comms.poll --source <name> [--once]``.

Dispatches by source name: ``telegram`` and ``slack`` are fixed single-account
pollers; any other name is treated as a named IMAP mailbox. Missing credentials
never crash this process — the poller flips the source to ``pending_setup`` with
a ``last_error`` and this CLI still exits 0.

``--seed-cursor`` exists because ``upsert_comms_source`` REPLACES ``config``
wholesale (``config_json = excluded.config_json``). Seeding the rollout cursor
with an ad-hoc one-liner therefore deletes ``reconciled_total`` and the
per-channel cursors — the only durable record that the socket has ever missed
anything, and the windows the sweep still owes. This subcommand read-modify-writes
instead, and is testable.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from typing import Any

from omniagentos.comms.pollers import gmail as gmail_poller
from omniagentos.comms.pollers import imap as imap_poller
from omniagentos.comms.pollers import slack as slack_poller
from omniagentos.comms.pollers import telegram as telegram_poller
from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore

__all__ = ["main"]

#: Annotated because the two pollers no longer share an exact signature (the
#: slack sweep takes injectable clock/sleep seams for its rate-limit tests);
#: without it the inferred value type collapses to `object`.
_NAMED_POLLERS: dict[str, Callable[[StewardStore, str], dict[str, Any]]] = {
    telegram_poller.KIND: telegram_poller.poll_once,
    slack_poller.KIND: slack_poller.poll_once,
}


def _poll_once(steward: StewardStore, source: str) -> dict[str, Any]:
    handler = _NAMED_POLLERS.get(source)
    if handler is not None:
        return handler(steward, source)
    # Gmail accounts are brokered (OAuth), not IMAP: any `gmail`/`gmail_<acct>`
    # source routes to the gmail poller. imap/telegram/slack stay untouched.
    if source == gmail_poller.KIND or source.startswith("gmail_"):
        return gmail_poller.poll_once(steward, source)
    return imap_poller.poll_once(steward, source)


def seed_cursor(steward: StewardStore, source: str, *, at: float | None = None) -> dict[str, Any]:
    """Set ``config["last_poll_ts"]`` on *source* WITHOUT discarding the rest.

    A merge, never a replace: the reconciliation counters and every per-channel
    cursor are carried forward. Re-running this after the sweep has been live is
    then a safe re-baseline rather than the silent erasure of the evidence that
    the socket has been missing messages.
    """
    rows = {row["name"]: row for row in steward.list_comms_sources()}
    existing = rows.get(source) or {}
    config = dict(existing.get("config") or {})
    stamp = time.time() if at is None else float(at)
    config["last_poll_ts"] = f"{stamp:.6f}"
    kind = str(existing.get("kind") or source)
    steward.upsert_comms_source(source, kind, config=config)
    return {"source": source, "kind": kind, "seeded_last_poll_ts": config["last_poll_ts"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omniagentos.comms.poll")
    parser.add_argument(
        "--source",
        required=True,
        help="telegram, slack, a gmail_<acct> broker mailbox, or a named IMAP mailbox",
    )
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument(
        "--interval", type=float, default=30.0, help="seconds between polls when not --once"
    )
    parser.add_argument(
        "--seed-cursor",
        nargs="?",
        const="now",
        default=None,
        metavar="EPOCH",
        help=(
            "merge config['last_poll_ts'] (default: now) into --source and exit — "
            "never replaces the rest of config"
        ),
    )
    args = parser.parse_args(argv)

    steward = StewardStore(SqliteStore(default_db_path()))
    if args.seed_cursor is not None:
        at = None if args.seed_cursor == "now" else float(args.seed_cursor)
        print(json.dumps(seed_cursor(steward, args.source, at=at)))
        return 0
    result = _poll_once(steward, args.source)
    print(json.dumps(result))
    if args.once:
        return 0

    while True:
        time.sleep(args.interval)
        result = _poll_once(steward, args.source)
        print(json.dumps(result))


if __name__ == "__main__":
    raise SystemExit(main())
