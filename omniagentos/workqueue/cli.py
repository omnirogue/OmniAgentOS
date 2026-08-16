"""``wq`` — the operator surface for the shared work queue (SPEC §6).

    python -m omniagentos.workqueue.cli <command> [--db PATH | --server URL]

Plain argparse and plain ``print`` on purpose: this runs over ssh on a Vultr box
and inside a launchd log as often as it runs in a terminal, so it takes no
rendering dependency (no rich, no typer) and every line is greppable.

``--json`` prints EXACTLY the ``GET /v1/status`` payload — the text renderer and
the JSON are one payload with two renderers, which is why the static HTML page
was cut from this week's scope (§6): a third renderer of the same thing.

Rendering is deliberately tolerant of missing keys. The status payload's
per-machine rows are the queue core's shape (Lane A); a KeyError here would turn
"one telemetry column is not populated yet" into "the operator cannot see the
pool at all", which is the worse failure by a distance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from omniagentos.workqueue.worker import open_queue

WATCH_INTERVAL_S = 5

#: Reported for a unit nobody attributed. Matches ``store.UNATTRIBUTED`` — a
#: name rather than a blank, so unattributed work is visible instead of missing.
UNATTRIBUTED = "(unattributed)"


def default_submitter() -> str:
    """Who is offloading: ``--by``, else env ``WQ_USER``, else '(unattributed)'.

    Read at PARSE time so ``wq enqueue --help`` shows the value that will be
    used, and never guessed from the OS login: a shared ``omniworker`` account
    would attribute everyone's work to the machine.
    """
    return os.environ.get("WQ_USER", "").strip() or UNATTRIBUTED


# --------------------------------------------------------------- formatting --
def _pick(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return default


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _num(value: Any, fmt: str = "{}") -> str:
    return "—" if value is None else fmt.format(value)


def render_status(payload: dict[str, Any]) -> str:
    machines = payload.get("machines") or []
    depth = payload.get("depth") or {}
    capacity = payload.get("capacity") or {}
    lines: list[str] = []

    workers = sum(int(_pick(m, "workers", "worker_count", default=0) or 0) for m in machines)
    in_flight = capacity.get("in_flight")
    if in_flight is None:
        in_flight = sum(int(_pick(m, "in_flight", "inflight", default=0) or 0) for m in machines)
    lines.append(
        f"POOL  {len(machines)} machines · {workers} workers · {in_flight} in flight "
        f"· capacity {_num(capacity.get('total_slots'))}"
    )
    # the operator 2026-08-11: every box's cores and load are visible pool-wide.
    loads = [_pick(m, "last_load1", "load1") for m in machines]
    load_sum = sum(float(x) for x in loads if x is not None) if loads else 0.0
    lines.append(
        f"CAPACITY  {_num(capacity.get('total_cores'))} cores "
        f"({_num(capacity.get('total_perf_cores'))} perf) · "
        f"slots {_num(capacity.get('total_slots'))} "
        f"({_num(capacity.get('free_slots'))} free) · pool load1 {load_sum:.2f}"
    )
    lines.append("")
    lines.append(
        "DEPTH        "
        + "   ".join(
            f"{state} {int(depth.get(state, 0) or 0)}"
            for state in ("queued", "claimed", "running", "review", "done", "parked")
        )
    )
    lines.append(
        f"OLDEST UNCLAIMED   {_dur(payload.get('oldest_unclaimed_s'))}"
        "        (alert threshold: 15m with idle capacity)"
    )
    unclaimable = payload.get("unclaimable") or []
    detail = ""
    if unclaimable:
        # A queue that looks idle because nothing can run must never read as healthy.
        labels = sorted({str(lbl) for row in unclaimable for lbl in (row.get("labels") or [])})
        detail = f"  [labels: {', '.join(labels)} — no enrolled machine declares them]"
    lines.append(f"UNCLAIMABLE        {len(unclaimable)}{detail}")
    lines.append("")

    header = (
        f"{'MACHINE':<22}{'IN FLIGHT':>10}{'CAP':>5}{'CORES':>8}{'LOAD':>7}"
        f"{'DONE/1h':>9}{'DONE/6h':>9}{'ATT/COMPLETE':>14}{'LAST SEEN':>11}"
    )
    lines.append(header)
    for m in machines:
        cores = _pick(m, "ncpu", "cores")
        perf = _pick(m, "perf_cores")
        cores_text = f"{cores}({perf}P)" if cores is not None and perf is not None else _num(cores)
        name = str(_pick(m, "machine_id", "id", default="?"))
        if _pick(m, "drain", default=0):
            name += "*"  # * = draining
        lines.append(
            f"{name:<22}"
            f"{str(_pick(m, 'in_flight', 'inflight', default=0)) + '/' + str(_pick(m, 'max_concurrent', 'cap', default=0)):>10}"
            f"{_num(_pick(m, 'max_concurrent', 'cap')):>5}"
            f"{cores_text:>8}"
            f"{_num(_pick(m, 'last_load1', 'load1')):>7}"
            f"{_num(_pick(m, 'done_1h')):>9}"
            f"{_num(_pick(m, 'done_6h')):>9}"
            f"{_num(_pick(m, 'attempts_per_completion', 'att_per_complete'), '{:.1f}'):>14}"
            f"{_dur(_pick(m, 'last_seen_s')):>11}"
        )
    if any(_pick(m, "drain", default=0) for m in machines):
        lines.append("  * draining — finishes in-flight work, claims nothing new")
    lines.append("")

    # the operator 2026-08-11: "we should be aware when one of us is offloading or has a
    # pending job." One line per person, machines named — the answer to "is my
    # thing running, and where" without opening the JSON.
    offloads = payload.get("offloads") or []
    if offloads:
        lines.append("OFFLOADS")
        for row in offloads:
            segments: list[str] = []
            running = int(row.get("running") or 0)
            if running:
                boxes = [str(m) for m in (row.get("machines") or [])]
                where = f" ({', '.join(boxes)})" if boxes else ""
                segments.append(f"{running} running{where}")
            for key, label in (("queued", "queued"), ("in_review", "in review")):
                count = int(row.get(key) or 0)
                if count:
                    segments.append(f"{count} {label}")
            person = str(_pick(row, "person", "submitted_by", default=UNATTRIBUTED))
            lines.append(f"  {person}: {' · '.join(segments) or 'nothing in flight'}")
        lines.append("")

    refusals = payload.get("refusals_24h") or {}
    if refusals:
        ordered = [
            (k, refusals[k])
            for k in (
                "candidate-defect",
                "instrument-error",
                "environment",
                "contention-flake",
                "unchanged-retry",
                "storm-parked",
                "timeout",
            )
            if k in refusals
        ]
        lines.append("REFUSALS 24h     " + " · ".join(f"{name} {count}" for name, count in ordered))
        share = refusals.get("instrument_share")
        if share is not None:
            # the operator's own baseline is 64/90 = 71% mechanics. Watching this number
            # fall is how §4 proves itself; this week it only has to be MEASURED.
            lines.append(
                f"                 INSTRUMENT SHARE {float(share) * 100:.0f}%"
                "   (baseline 71% — 64 of 90 · target <20%)"
            )
    lines.append(
        f"LEASE RECLAIMS 1h   {_num(payload.get('lease_reclaims_1h'))}"
        f"        DOUBLE EXECUTIONS  {_num(payload.get('double_executions'))}"
        f"        ALERTS SENT  {_num(payload.get('alerts_sent'))} "
        f"({_num(payload.get('parks'))} parks)"
    )
    parked = payload.get("parked") or []
    if parked:
        lines.append("")
        lines.append("PARKED")
        for row in parked:
            lines.append(
                f"  {_pick(row, 'id', 'unit_id', default='?')} "
                f"{_pick(row, 'terminal_reason', default='parked')}  "
                f"{_pick(row, 'gate', default='')}"
            )
            remedy = _pick(row, "park_remedy", "remedy")
            if remedy:
                lines.append(f"           remedy: {remedy}")
    return "\n".join(lines)


def render_machines(rows: list[dict[str, Any]]) -> str:
    out = [
        f"{'MACHINE':<22}{'OS':<8}{'CORES':>8}{'MEM':>7}{'CAP':>5}{'CEIL':>6}"
        f"{'LOAD1':>7}{'DRAIN':>7}  LABELS"
    ]
    for m in rows:
        cores = _pick(m, "ncpu", "cores")
        perf = _pick(m, "perf_cores")
        labels = _pick(m, "labels", default=[])
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except json.JSONDecodeError:
                labels = [labels]
        out.append(
            f"{str(_pick(m, 'machine_id', default='?')):<22}"
            f"{str(_pick(m, 'os', default='?')):<8}"
            f"{(f'{cores}({perf}P)' if cores and perf else _num(cores)):>8}"
            f"{_num(_pick(m, 'mem_gb'), '{:.0f}G'):>7}"
            f"{_num(_pick(m, 'max_concurrent')):>5}"
            f"{_num(_pick(m, 'ceiling_fraction'), '{:.2f}'):>6}"
            f"{_num(_pick(m, 'last_load1')):>7}"
            f"{_num(_pick(m, 'drain', default=0)):>7}"
            f"  {','.join(str(x) for x in labels)}"
        )
    return "\n".join(out)


# ----------------------------------------------------------------- commands --
def cmd_init(q: Any, args: argparse.Namespace) -> int:
    # Opening the store runs its own migrator (and ONLY its own — the queue DB must
    # contain nothing but wq_* and schema_migrations; SPEC §1.2).
    print(f"initialised {args.db or args.server}")
    return 0


def _iter_units(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.json:
        return [json.loads(args.json)]
    units: list[dict[str, Any]] = []
    with open(args.file, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                units.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{args.file}:{lineno}: {exc}") from exc
    return units


def cmd_enqueue(q: Any, args: argparse.Namespace) -> int:
    if not (args.file or args.json):
        raise SystemExit("enqueue: one of --file or --json is required")
    new = deduped = 0
    # '(unattributed)' is a RENDERING of an empty submitted_by, not a person id:
    # storing the literal would leave the column with two spellings of "nobody".
    attributed = "" if args.by == UNATTRIBUTED else args.by
    for submit in _iter_units(args):
        # A submitted_by already in the payload WINS over --by: a JSONL file
        # written by an orchestrator is attributing on behalf of someone else,
        # and the flag's job is only to fill the gap.
        submit.setdefault("submitted_by", attributed)
        unit_id, was_dedupe = q.enqueue(submit)
        deduped += 1 if was_dedupe else 0
        new += 0 if was_dedupe else 1
        print(
            f"{'dedup' if was_dedupe else 'queued'} {unit_id} {submit.get('idempotency_key', '')}"
        )
    print(f"-- {new} queued, {deduped} deduplicated")
    return 0


def cmd_submit(q: Any, args: argparse.Namespace) -> int:
    """Re-queue a unit. The ledger KEEPS ITS MEMORY across a resubmit.

    A soft park (``unchanged-retry``: nothing ran) re-queues freely through
    ``requeue`` — which touches neither ``wq_refusals`` nor the attempt budget, so
    each resubmit of an unchanged unit adds one more refusal to the same row and
    the storm cap of 5 is actually reachable. Submitting through ``unpark``
    instead deletes that row, which makes the sixth submit look exactly like the
    first: the count oscillates, nothing ever storm-parks, and nobody is alerted.

    A terminal park (``storm-parked``, ``attempts-exhausted``,
    ``terminal-instrument``) is the amnesty case and needs ``--because``: that
    text is a human saying what changed, and only then does the refusal row for
    the unit's last input key get cleared (§4.5).

    ``--by`` names who is putting the unit back in flight. It does NOT rewrite
    the unit's ``submitted_by``: the person who asked for the work still owns it,
    and re-attributing on resubmit would erase the requester from every
    ``wq status`` block the moment somebody else helped them out.
    """
    unit = q.get_unit(args.unit)
    if not unit:
        raise SystemExit(f"submit: no such unit {args.unit}")
    state = unit.get("state")
    reason = unit.get("terminal_reason")
    if state in ("done", "cancelled"):
        raise SystemExit(f"submit: {args.unit} is {state} — enqueue a new unit instead")
    if state != "parked":
        print(f"{args.unit} is {state}; nothing to do")
        return 0
    if reason and not args.because:
        raise SystemExit(
            f"submit: {args.unit} is parked as {reason} — TERMINAL. Say what changed:\n"
            f"  wq submit --unit {args.unit} --because '<what you fixed>'"
        )
    if reason:
        # The amnesty: a human said what changed, so the refusal row for this
        # unit's last input key is cleared and the counters go back to zero.
        q.unpark(args.unit, args.because)
        note = f"unparked from {reason} — ledger cleared for this input: {args.because}"
    else:
        # Soft park: nothing ran, so nothing is forgiven. The refusal row stands,
        # which is what lets the storm cap be reached at all.
        q.requeue(args.unit)
        note = "soft park — the refusal ledger keeps its count"
        if args.because:
            # Say so rather than swallowing it: on a soft park the text changes
            # nothing, and a human who means the amnesty must ask for it by name.
            note += f"\n   (noted, not applied: {args.because} — `wq unpark` clears the ledger)"
    print(f"{args.unit} re-queued by {args.by} (submitted_by {unit.get('submitted_by') or '—'})")
    print(f"   {note}")
    return 0


def cmd_status(q: Any, args: argparse.Namespace) -> int:
    if not args.watch:
        payload = q.status()
        print(json.dumps(payload, indent=1) if args.json else render_status(payload))
        return 0
    while True:  # --watch is the ONE sanctioned refresh loop: a human is reading it
        payload = q.status()
        sys.stdout.write("\033[2J\033[H")
        print(
            f"wq status  {time.strftime('%H:%M:%S')}  (refresh {WATCH_INTERVAL_S}s, ctrl-c to stop)"
        )
        print(render_status(payload))
        sys.stdout.flush()
        time.sleep(WATCH_INTERVAL_S)


def cmd_machines(q: Any, args: argparse.Namespace) -> int:
    rows = q.list_machines()
    print(json.dumps(rows, indent=1) if args.json else render_machines(rows))
    return 0


def cmd_drain(q: Any, args: argparse.Namespace) -> int:
    q.set_drain(args.machine_id, not args.undo)
    # No lease is broken and nothing is force-reclaimed — that would be the exact
    # fail-open behaviour §3.4 is careful to avoid.
    print(
        f"{args.machine_id} drain={'0 (resumed)' if args.undo else '1 (finishing in-flight work)'}"
    )
    return 0


def cmd_unpark(q: Any, args: argparse.Namespace) -> int:
    if not args.because.strip():
        raise SystemExit("unpark: --because must carry text (there is no automatic unpark)")
    q.unpark(args.unit, args.because)
    print(f"{args.unit} unparked: {args.because}")
    return 0


def cmd_alerts(q: Any, args: argparse.Namespace) -> int:
    rows = q.alerts()
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for row in rows:
        print(
            f"{_pick(row, 'alerted_at', 'last_seen_at', default='?')}  "
            f"{_pick(row, 'terminal_reason', 'refusal_class', default='park')}  "
            f"{_pick(row, 'unit_id', 'input_key', default='?')}\n"
            f"    {_pick(row, 'remedy', 'park_remedy', default='')}"
        )
    print(f"-- {len(rows)} alerts")
    return 0


def cmd_cancel(q: Any, args: argparse.Namespace) -> int:
    q.cancel(args.unit)
    print(f"{args.unit} cancel requested")
    return 0


def cmd_reap(q: Any, args: argparse.Namespace) -> int:
    count = q.reap_expired()
    print(f"reclaimed {count} expired lease(s)")
    return 0


# ------------------------------------------------------------------- parser --
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.workqueue.cli", description="shared work queue operator CLI"
    )
    parser.add_argument("--server", default=os.environ.get("WQ_SERVER"), help="wq-server base URL")
    parser.add_argument(
        "--db", default=os.environ.get("WQ_DB"), help="path to the queue sqlite file"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create/upgrade the queue DB").set_defaults(func=cmd_init)

    by_help = "who is offloading this work (default: env WQ_USER, else '(unattributed)')"

    enqueue = sub.add_parser("enqueue", help="submit units from a JSONL file or one JSON object")
    enqueue.add_argument("--file")
    enqueue.add_argument("--json")
    enqueue.add_argument("--by", default=default_submitter(), help=by_help)
    enqueue.set_defaults(func=cmd_enqueue)

    submit = sub.add_parser("submit", help="re-enqueue a parked unit")
    submit.add_argument("--unit", required=True)
    submit.add_argument("--because", default="")
    submit.add_argument("--by", default=default_submitter(), help=by_help)
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status", help="the pool at a glance")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    machines = sub.add_parser("machines", help="enrolled machines and their capacity")
    machines.add_argument("--json", action="store_true")
    machines.set_defaults(func=cmd_machines)

    drain = sub.add_parser("drain", help="stop a machine claiming new work")
    drain.add_argument("machine_id")
    drain.add_argument("--undo", action="store_true")
    drain.set_defaults(func=cmd_drain)

    unpark = sub.add_parser("unpark", help="clear a park (requires a reason)")
    unpark.add_argument("unit")
    unpark.add_argument("--because", required=True)
    unpark.set_defaults(func=cmd_unpark)

    alerts = sub.add_parser("alerts", help="alerts sent (one per park)")
    alerts.add_argument("--json", action="store_true")
    alerts.set_defaults(func=cmd_alerts)

    cancel = sub.add_parser("cancel", help="request cancellation of a unit")
    cancel.add_argument("unit")
    cancel.set_defaults(func=cmd_cancel)

    sub.add_parser("reap", help="reclaim expired leases once").set_defaults(func=cmd_reap)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.server or args.db):
        raise SystemExit("one of --server URL (env WQ_SERVER) or --db PATH (env WQ_DB) is required")
    queue = open_queue(args.server, args.db)
    try:
        return int(args.func(queue, args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
