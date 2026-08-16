#!/usr/bin/env python3
"""The mechanical publisher of `state/queue.json` — its SOLE RUNTIME writer.

`bridge/integration.py:rebuild_queue()` already emits the right shape — `wip`,
`wip_cap`, `wip_definition`, replayed from `ledger.jsonl` — but nothing ran it
on its own cadence, so the coordinating LLM kept hand-authoring queue.json
between Integration ticks, and every hand-write dropped `wip`. A missing
`wip` key reads as headroom to both producer loops (favourable absence), so a
12-over-8 WIP breach went unseen for hours (2026-08-08, sha256:6c2519c3).

This file is deliberately NOT a new gate and NOT a second replay engine: it
imports `LedgerView`, `kinds_from_disk`, and `rebuild_queue` straight from
`bridge/integration.py` (the ONE emitter — see DESIGN.md's "true-pending
computation ... never a second independent replay") and does nothing else:

  * no admission decision
  * no gating
  * no subprocess
  * atomic tmp+fsync+rename write, same discipline as every other writer here
  * `write_atomic()` here is the ONLY runtime call site that ever touches
    `state/queue.json`. `bootstrap.sh` still seeds an initial placeholder
    file (create-if-empty, one-time, at repo setup) — that is a distinct,
    pre-runtime concern and is explicitly NOT superseded by this module; see
    `bootstrap.sh`'s own `queue.json` line if it needs to change too.

Run it on a 300s timer (`com.threeloops.publish-queue.plist`, StartInterval
300) instead of a human or an LLM loop hand-editing `state/queue.json`.

**As of this commit, no `PROMPT-*-loop.md` tells any loop to call this file.**
That wiring was drafted and then reverted from this branch — prompt TEXT is
authored by the operator only (CONTRACT.md §1), and an Implementer-authored
prompt edit is self-modification. The draft lives out-of-repo, routed to the
operator for signature. Until it lands, this module and `read_wip()` below
have a caller only inside `bridge/integration.py`'s own tick (see
`Integration.iterate()`) and in this file's own `--loops-root` CLI — no loop
prompt invokes either yet.

`read_wip()` below is the fail-closed reader half of the same fix: a producer
that indexes queue.json directly (`.get("wip", 0)`) reintroduces the exact
favourable-absence bug this file exists to close. Route the check through
`read_wip()` (or replicate its refusal shape) instead.

Usage:
    publish_queue.py --loops-root var/loopqueue --once   # one publish, exit
    publish_queue.py --loops-root var/loopqueue          # loop every 300s
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bridge import integration as I  # noqa: E402  — the ONE emitter, reused not re-derived

TICK_SECONDS = 300  # matches com.threeloops.publish-queue.plist StartInterval
PUBLISHER = "publish_queue"


def read_wip_cap(root: Path, default: int = 4) -> int:
    """`wip_cap` from `state/budget.json` if present and parseable, else `default`.

    This is deliberately NOT `integration.read_governor()` — that function's
    job is a fail-closed ADMISSION decision (stop the iteration on stale/absent
    budget); this publisher never admits or gates anything, it just needs a
    number to stamp into the published queue, and an unreadable budget should
    not stop the queue itself from staying live.

    A deliberate `wip_cap: 0` (halt every producer) is a real, distinct value
    from "the key is absent" — `budget.get("wip_cap") or default` used to
    collapse them, silently ignoring an operator's zero. Missing/null/absent
    falls back to `default`; a present-but-zero value is honoured as zero.
    """
    path = root / "state" / "budget.json"
    try:
        budget = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(budget, dict):
        return default
    raw = budget.get("wip_cap")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def write_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(path)


class WipKeyMissing(RuntimeError):
    """Raised by `read_wip()` when a queue.json omits the backpressure keys.

    A missing key must never be read as headroom. This is the mechanism half
    of the falsifier: "producers demonstrably refuse to file when the key is
    absent."
    """


def read_wip(path: Path) -> dict:
    """Load `queue.json`, refusing (raising `WipKeyMissing`) rather than
    silently treating an absence as headroom.

    Refuses when:
      * the file is absent or unreadable/not-JSON/not-an-object
      * any of `wip`, `wip_cap`, `wip_definition`, `wip_degraded` is missing;
        the expected degraded shape omits only `wip` and is refused below with
        its damage locator
      * `wip` or `wip_cap` is present but not an int (e.g. `"0"`, a string —
        presence alone is not enough; a producer that does `int(wip) > cap`
        without this check gets a `TypeError` at best and a silent wrong
        comparison at worst, both of which are the same favourable-absence
        shape one level down)
      * `rebuilt_at` is more than 2x the publish interval old (the publisher
        died; a stale count is exactly as dangerous as a missing one)
      * `wip_degraded` is true because ledger replay discarded a line or saw
        a torn tail; damaged provenance cannot support a trustworthy count
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WipKeyMissing(
            f"queue.json absent at {path} — STOP, do not treat as headroom"
        ) from None
    except OSError as exc:
        raise WipKeyMissing(
            f"queue.json unreadable at {path}: {type(exc).__name__}: {exc} — STOP"
        ) from exc
    try:
        q = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WipKeyMissing(f"queue.json at {path} is not valid JSON: {exc} — STOP") from exc
    if not isinstance(q, dict):
        raise WipKeyMissing(f"queue.json at {path} is not a JSON object — STOP")

    missing = [k for k in ("wip", "wip_cap", "wip_definition", "wip_degraded") if k not in q]
    expected_degraded_omission = missing == ["wip"] and q.get("wip_degraded") is True
    if missing and not expected_degraded_omission:
        raise WipKeyMissing(
            f"queue.json at {path} is missing {missing} — a missing wip key reads as "
            "headroom by accident; STOP and alert instead of proceeding")

    degraded = q["wip_degraded"]
    if not isinstance(degraded, bool):
        raise WipKeyMissing(
            f"queue.json at {path} has wip_degraded={degraded!r} "
            f"({type(degraded).__name__}, not bool) — ledger health is unknown; STOP and alert")
    if degraded:
        detail = q.get("wip_degraded_detail")
        locator = (detail if isinstance(detail, str) and detail
                   else (f"discarded={q.get('ledger_discarded')!r}, "
                         f"torn_tail={q.get('ledger_torn_tail')!r}"))
        raise WipKeyMissing(
            f"queue.json at {path} has degraded WIP provenance: {locator} — "
            f"discarded={q.get('ledger_discarded')!r}, "
            f"torn_tail={q.get('ledger_torn_tail')!r}; refuse the guessed count; "
            "STOP and repair the ledger")

    if "wip" not in q:
        raise WipKeyMissing(
            f"queue.json at {path} is missing ['wip'] — a missing wip key reads as "
            "headroom by accident; STOP and alert instead of proceeding")

    for key in ("wip", "wip_cap"):
        val = q[key]
        if isinstance(val, bool) or not isinstance(val, int):
            raise WipKeyMissing(
                f"queue.json at {path} has {key}={val!r} ({type(val).__name__}, not int) — "
                "an untyped value is not a trustworthy backpressure signal; STOP and alert")

    stamp = q.get("rebuilt_at")
    age = None
    if stamp:
        parsed = I._parse(stamp)
        if parsed is not None:
            age = (I._now() - parsed).total_seconds()
    if age is None:
        raise WipKeyMissing(
            f"queue.json at {path} has no parseable rebuilt_at ({stamp!r}) — cannot tell "
            "fresh from rotten; STOP and alert")
    if age > 2 * TICK_SECONDS:
        raise WipKeyMissing(
            f"queue.json at {path} is STALE by {int(age)}s (rebuilt_at={stamp}) — the "
            "publisher is not running; a stale wip count reads as headroom; STOP and alert")
    return q


def publish(root: Path) -> tuple[dict | None, str]:
    """Build a fresh ledger view and publish from it. Returns `(queue_or_None, message)`.

    This is what the 300s timer calls. `integration.py` calls `publish_from()`
    directly with its own (richer) in-memory ledger view instead — see that
    function's docstring — so that EVERY write to `state/queue.json`, from
    either caller, goes through the same `write_atomic()` call below. Two
    independent writers racing the same file is the bug this module exists to
    retire, not just the hand-authored one.
    """
    ledger = I.LedgerView.build(root)
    ledger.kinds.update(I.kinds_from_disk(root))
    return publish_from(root, ledger)


def publish_from(root: Path, ledger: I.LedgerView, wip_cap: int | None = None,
                 *, write: bool = True) -> tuple[dict | None, str]:
    """Rebuild `state/queue.json` from an already-built `LedgerView`, and
    atomically write it unless `write=False`. Returns `(queue_or_None, message)`.

    `write=False` is for a caller (namely `integration.py` in `--dry-run`,
    its default mode) that wants the computed queue for its own report
    without mutating the live file — the old code path this replaced went
    through `Integration._write_json`, which no-ops when `apply` is false,
    and that dry-run guarantee has to survive here too: the whole point of
    dry-run is "writes nothing", and a 300s timer publisher is already the
    steady-state writer, so a dry-run adapter tick racing it would be a new
    bug of exactly the shape this module exists to close.

    `wip_cap` defaults to `read_wip_cap(root)` (reads `state/budget.json`);
    pass it explicitly when the caller already has a governor reading for this
    tick (e.g. `integration.py`, which fail-closes on budget staleness before
    it ever gets here).

    Refuses (returns `None`) when the ledger's tail is torn AND replay
    produced zero events: a queue.json written from a failed read would
    publish `wip: 0`, the maximum-headroom lie, to every producer. This is
    the ONE place that guard is checked — `integration.py` used to carry its
    own copy of it.
    """
    if ledger.torn_tail and not ledger.events:
        return None, (
            "refusing to publish queue.json: the ledger is unreadable end to end "
            "(torn tail, zero events) — a queue.json written from a failed read "
            "would publish wip:0, maximum headroom, to every producer; NO file written")

    if wip_cap is None:
        wip_cap = read_wip_cap(root)
    q = I.rebuild_queue(root, ledger, wip_cap)
    q["rebuilt_by"] = PUBLISHER  # names the mechanical publisher, never a hand-edit
    if write:
        write_atomic(root / "state" / "queue.json", q)
        verb = "published"
    else:
        verb = "computed (dry-run: NOT written)"
    return q, (f"{verb} wip={q.get('wip')} wip_cap={q['wip_cap']} "
               f"items={len(q['items'])} torn_tail={ledger.torn_tail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--once", action="store_true", help="one publish, then exit (default: loop)")
    args = ap.parse_args(argv)

    root: Path = args.loops_root.resolve()
    if not root.is_dir():
        print(f"no such queue: {root}", file=sys.stderr)
        return 2

    while True:
        q, msg = publish(root)
        print(json.dumps({"ok": q is not None, "message": msg}))
        if args.once:
            return 0 if q is not None else 1
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
