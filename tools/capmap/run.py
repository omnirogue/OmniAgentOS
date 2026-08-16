#!/usr/bin/env python3
"""capmap scheduled runner: verify the registry, then tell the alert transport.

This module is the *wiring*, not new policy.  It owns exactly three decisions:

1. **Completeness of input.**  Every capability in the registry is fed to
   ``alert.Alerter.send`` on every run, with the previous snapshot status as
   ``from_status`` -- including the ones that are fine, because a recovery is
   only visible to the transport if the good result is delivered to it.  What
   counts as news (dedupe, escalation, incident open/close) stays in alert.py.

2. **Proof that checking happened.**  A heartbeat file in the same one-file /
   one-timestamp shape ``alert.Alerter.check_liveness`` reads, plus a
   once-per-calendar-day digest.  Silence is only evidence of health when
   something positively says the checker ran; a missing heartbeat therefore
   reads stale, never healthy.

3. **Not paging a human dozens of times to say hello.**  66 capabilities that
   have never been verified are 66 "bad" statuses on the very first run.  So a
   run with no recorded baseline seeds itself: state is written, nothing is
   sent, and the flag is recorded so it happens once.  ``--seed-only`` forces
   the same behaviour explicitly.

Exit codes are launchd-shaped: 0 means *the run completed*, however unhealthy
the estate turned out to be, and non-zero means the runner itself broke.  An
outage is a successful check, and a scheduler that treats it as a crash learns
to distrust the wrong signal.
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:                    # launchd runs this by absolute path
    sys.path.insert(0, _HERE)

import alert  # noqa: E402  (path set above)
import cli  # noqa: E402
import registry  # noqa: E402
import store  # noqa: E402

HOME = "/Users/youruser"
RUNNER_STATE_FILENAME = "runner.json"
HEARTBEAT_FILENAME = "heartbeat.json"
DEFAULT_HEARTBEAT_FIELD = "capmap_ran_at"
DEFAULT_CADENCE_SECONDS = 900                # matches the plist StartInterval

EXIT_OK = 0
EXIT_RUNNER_FAILED = 1


class RunnerError(Exception):
    """The runner could not complete.  Distinct from 'the estate is unhealthy'."""


# --------------------------------------------------------------- runner state

def runner_state_path(store_dir):
    return os.path.join(os.path.expanduser(store_dir), RUNNER_STATE_FILENAME)


def heartbeat_path(store_dir):
    return os.path.join(os.path.expanduser(store_dir), HEARTBEAT_FILENAME)


def seeding_required(store_dir, seed_only=False):
    """True when this run must establish a baseline instead of paging.

    Exposed (rather than buried inside ``run``) because the *alerter* has to be
    constructed in seed mode too, and a caller that builds its own alerter must
    be able to ask the same question and get the same answer."""
    state = read_runner_state(runner_state_path(store_dir))
    return bool(seed_only or not state.get("baseline_established_at"))


def read_runner_state(path):
    """Missing or corrupt runner state reads as *no baseline*, which seeds."""
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_runner_state(path, payload):
    try:
        alert.atomic_write_json(path, payload)
    except OSError as exc:
        raise RunnerError(f"cannot write runner state {path}: {exc}") from None


# ------------------------------------------------------------------ heartbeat

def write_heartbeat(path, field, now, extra=None):
    """One JSON object, one timestamp field -- the shape check_liveness reads.

    Written last-thing-that-can-fail-loudly: if this raises, the run is a runner
    failure, because a heartbeat we believe we wrote but did not is worse than
    no heartbeat at all (the dead-man's switch would read the stale one and stay
    quiet)."""
    payload = dict(extra or {})
    payload[field] = alert.iso(now)
    try:
        alert.atomic_write_json(path, payload)
    except OSError as exc:
        raise RunnerError(f"cannot write heartbeat {path}: {exc}") from None
    return payload


# --------------------------------------------------------------- verification

def verify_all(entries, store_dir):
    """Run the CLI's verification path verbatim and return its result rows.

    Deliberately calls ``cli._verify`` rather than re-deriving status: the exit
    semantics, staleness rule, run-record shape and snapshot write all live
    there, and a second implementation of them would drift.  stdout is captured
    because the CLI prints for humans; the runner prints its own summary."""
    args = argparse.Namespace(json=True, accept=[])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        verify_exit = cli._verify(entries, args, store_dir)
    try:
        rows = json.loads(buffer.getvalue())
    except ValueError as exc:
        raise RunnerError(f"verification produced unreadable output: {exc}") from None
    return rows, verify_exit


def previous_statuses(entries, store_dir):
    """Effective status per capability *before* this run (STALE included)."""
    snapshot = store.read_snapshot(store_dir)
    return {row["id"]: row["status"] for row in cli._rows(entries, snapshot)}


# ------------------------------------------------------------- alert payloads

def _detail(capability, row):
    return "{}/{} on {} is {} (last verified {}) — {}".format(
        capability["company"], capability["kind"], capability["host"],
        row["status"], row.get("last_verified") or "never",
        capability["why_it_matters"])


def _remedy(capability):
    # Never echo the verification argv/command: it can name credential vars.
    return ("run: capmap verify --id {}  (owner {}, escalation tier {}, repo {})".format(capability["id"], capability["owner"], capability["escalation_tier"],
               capability["repo"]))


# ------------------------------------------------------------------- digest

def _today(now):
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d")


def _missed_days(last_date, today):
    """Whole calendar days between the last digest and today, exclusive.

    Reported rather than swallowed: a gap means runs did not happen, which is
    exactly the fact the digest exists to make visible."""
    try:
        previous = datetime.datetime.strptime(last_date, "%Y-%m-%d").date()
        current = datetime.datetime.strptime(today, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    return max(0, (current - previous).days - 1)


# ----------------------------------------------------------------- the run

def run(registry_dir=None, store_dir=None, dry_run=False, seed_only=False,
        alerter=None, verify_fn=None, now=None, heartbeat_file=None,
        heartbeat_field=DEFAULT_HEARTBEAT_FIELD, notify=True, out=None):
    """Verify everything, feed every result to the transport, prove we ran.

    Returns a summary dict.  Raises RunnerError only when the *runner* failed.
    """
    out = out if out is not None else sys.stdout
    registry_dir = registry_dir or registry.CAPABILITIES_DIR
    store_dir = store_dir or store.DEFAULT_BASE_DIR
    now = now or alert.now_utc()
    verify_fn = verify_fn or verify_all

    state_path = runner_state_path(store_dir)
    runner_state = read_runner_state(state_path)
    baseline = runner_state.get("baseline_established_at")
    seeding = bool(seed_only or not baseline)

    if alerter is None:
        alerter = _build_alerter(dry_run=dry_run, seed_only=seeding,
                                 store_dir=store_dir, out=out)
    elif seeding and getattr(alerter, "seed_only", None) is False:
        # Belt and braces: a caller that built its own Alerter without knowing a
        # baseline was missing would otherwise page once per capability on the
        # very first run.  The pre-declared safety requirement wins over the
        # caller's flag.
        alerter.seed_only = True

    try:
        entries = registry.load_registry(registry_dir)
    except ValueError as exc:
        raise RunnerError(f"registry unreadable: {exc}") from None
    if not entries:
        raise RunnerError(f"registry {registry_dir} contains no capabilities")

    before = previous_statuses(entries, store_dir)
    rows, verify_exit = verify_fn(entries, store_dir)
    by_id = {capability["id"]: capability for capability in entries}

    statuses = {}
    actions = []
    errors = []
    for row in rows:
        capability = by_id.get(row["id"])
        if capability is None:                 # cannot happen via verify_all
            continue
        statuses[row["id"]] = row["status"]
        from_status = before.get(row["id"], store.UNVERIFIED)
        try:
            result = alerter.send(
                row["id"], from_status, row["status"],
                detail=_detail(capability, row),
                blast_radius=capability["blast_radius"],
                remedy=_remedy(capability),
            )
        except Exception as exc:               # transport itself broke: runner problem
            errors.append("{}: {}: {}".format(row["id"], type(exc).__name__, exc))
            continue
        actions.append({"capability": row["id"], "from": from_status,
                        "to": row["status"], "action": result.get("action"),
                        "delivered": bool(result.get("delivered"))})

    # ---- digest: exactly once per calendar day, and never silently skipped ---
    today = _today(now)
    digest_line = None
    digest_emitted = False
    gap = _missed_days(runner_state.get("last_digest_date"), today) \
        if runner_state.get("last_digest_date") else 0
    if seeding:
        digest_reason = "seeding — no digest sent on a baseline run"
    elif runner_state.get("last_digest_date") == today:
        digest_reason = f"already emitted today ({today})"
    else:
        try:
            digest_line, delivered = alerter.digest(statuses=statuses)
            digest_emitted = True
            digest_reason = "emitted for {}{}".format(
                today, "" if delivered else " (DELIVERY FAILED)")
        except Exception as exc:
            errors.append(f"digest: {type(exc).__name__}: {exc}")
            digest_reason = f"digest raised {type(exc).__name__}"

    # ---- heartbeat ------------------------------------------------------
    unhealthy = sorted(name for name, status in statuses.items()
                       if not alert.is_good(status))
    hb_path = heartbeat_file or heartbeat_path(store_dir)
    heartbeat = None
    if not dry_run:
        heartbeat = write_heartbeat(hb_path, heartbeat_field, now, extra={
            "capabilities_checked": len(statuses),
            "unhealthy": len(unhealthy),
            "seeded": seeding,
        })

    # ---- persist runner state ------------------------------------------
    if not dry_run:
        runner_state["last_run_at"] = alert.iso(now)
        runner_state["last_run_checked"] = len(statuses)
        runner_state["last_run_unhealthy"] = len(unhealthy)
        if digest_emitted:
            runner_state["last_digest_date"] = today
            if gap:
                history = runner_state.setdefault("digest_gaps", [])
                history.append({"before": today, "missed_days": gap})
                runner_state["digest_gaps"] = history[-20:]
        if not baseline:
            runner_state["baseline_established_at"] = alert.iso(now)
        write_runner_state(state_path, runner_state)

    summary = {
        "ran_at": alert.iso(now),
        "registry_dir": registry_dir,
        "store_dir": store_dir,
        "dry_run": bool(dry_run),
        "seeding": seeding,
        "verify_exit": verify_exit,
        "checked": len(statuses),
        "unhealthy": unhealthy,
        "actions": actions,
        "sent": [a for a in actions
                 if a["action"] in ("alerted", "escalated", "recovered")],
        "digest_line": digest_line,
        "digest_emitted": digest_emitted,
        "digest_reason": digest_reason,
        "digest_gap_days": gap,
        "heartbeat_file": hb_path,
        "heartbeat_field": heartbeat_field,
        "heartbeat": heartbeat,
        "runner_errors": errors,
    }
    if gap and not seeding:
        summary.setdefault("warnings", []).append(
            f"digest gap: {gap} calendar day(s) had no digest — the runner was not "
            "running")
    return summary


def exit_code(summary):
    """0 when the run completed, whatever it found.

    An unhealthy estate is a SUCCESSFUL check; only the runner breaking is
    non-zero, so launchd's failure signal keeps meaning "the checker broke"
    rather than "something the checker watches is broken"."""
    return EXIT_RUNNER_FAILED if summary["runner_errors"] else EXIT_OK


def _build_alerter(dry_run, seed_only, store_dir, out=None):
    """Build the production Alerter, with one safety detour for dry runs.

    A dry run must not mutate production incident state.  ``DryRunChannel``
    reports success, so an Alerter pointed at the real incidents.json would mark
    incidents notified and thereby *suppress* the next genuine page.  So a dry
    run works against a throwaway COPY of the real incident state: dedupe still
    reflects reality, and reality is left untouched."""
    kwargs = {}
    if dry_run:
        scratch = tempfile.mkdtemp(prefix="capmap-dryrun-")
        incidents = os.path.join(scratch, "incidents.json")
        with contextlib.suppress(OSError):
            shutil.copyfile(alert.INCIDENTS_FILE, incidents)
        kwargs = {"incidents_path": incidents,
                  "ledger_path": os.path.join(scratch, "alerts.jsonl")}
        if out is not None:
            out.write(f"dry run: incident state copied to {scratch} (production "
                      "incidents.json untouched)\n")
    return alert.build_alerter(dry_run=dry_run, seed_only=seed_only, **kwargs)


# ------------------------------------------------------------------ rendering

def render(summary, alerter=None):
    lines = ["capmap run {}{}{}".format(
        summary["ran_at"],
        "  [DRY RUN — nothing sent]" if summary["dry_run"] else "",
        "  [SEEDING — baseline only, nothing sent]" if summary["seeding"] else "")]
    lines.append(
        f"checked: {summary['checked']} capabilities · "
        f"unhealthy: {len(summary['unhealthy'])} · verify exit: {summary['verify_exit']}"
    )
    interesting = [a for a in summary["actions"] if a["action"] != "no_incident"]
    if interesting:
        lines.append("transitions fed to alert.send():")
        for action in interesting:
            lines.append(
                f"  {action['capability']:<34} {action['from']:<14} -> "
                f"{action['to']:<14} {action['action']}"
                f"{'' if action['delivered'] else '  !! UNDELIVERED'}"
            )
    else:
        lines.append("transitions fed to alert.send(): none was news")
    lines.append("digest: {}".format(summary["digest_reason"]))
    if summary["digest_line"]:
        lines.append("  {}".format(summary["digest_line"]))
    for warning in summary.get("warnings", []):
        lines.append(f"WARNING: {warning}")
    lines.append("heartbeat: {} [{}]{}".format(
        summary["heartbeat_file"], summary["heartbeat_field"],
        "" if summary["heartbeat"] else "  (not written — dry run)"))
    if summary["runner_errors"]:
        lines.append("RUNNER ERRORS:")
        lines.extend(f"  {error}" for error in summary["runner_errors"])
    sink = list(getattr(alerter, "channels", []) or [])
    rendered = [text for channel in sink for text in getattr(channel, "sink", [])]
    if rendered:
        lines.append(f"--- would have been sent ({len(rendered)} message(s)) ---")
        for text in rendered:
            lines.append(text)
            lines.append("---")
    return "\n".join(lines)


# ------------------------------------------------------------------- liveness

def check_liveness(store_dir=None, heartbeat_file=None,
                   heartbeat_field=DEFAULT_HEARTBEAT_FIELD,
                   cadence_seconds=DEFAULT_CADENCE_SECONDS, dry_run=False,
                   alerter=None, out=None):
    """Dead-man's switch pointed at capmap's own heartbeat.

    alert.py's ``--check-liveness`` defaults to the integrations probe's file;
    this exposes the same switch with capmap's file/field so an *independent*
    launchd job can watch the watcher.  A missing heartbeat is stale by
    construction -- that check lives in alert.check_liveness and is not
    re-implemented here."""
    out = out if out is not None else sys.stdout
    store_dir = store_dir or store.DEFAULT_BASE_DIR
    path = heartbeat_file or heartbeat_path(store_dir)
    if alerter is None:
        alerter = _build_alerter(dry_run=dry_run, seed_only=False,
                                 store_dir=store_dir, out=out)
    result = alerter.check_liveness(
        checker="capmap", heartbeat_file=path, heartbeat_field=heartbeat_field,
        cadence_seconds=cadence_seconds,
        blast_radius="ALL capability monitoring — capmap has stopped checking, "
                     "so an outage anywhere would now go unreported",
        remedy="launchctl list | grep com.omniagentos.capmap ; then run "
               "/usr/bin/python3 {} once by hand".format(os.path.join(_HERE, "run.py")))
    result["heartbeat_file"] = path
    result["heartbeat_field"] = heartbeat_field
    return result


# ----------------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="capmap-run",
        description="verify every capability, feed the transport, prove we ran")
    parser.add_argument("--registry-dir",
                        default=os.environ.get("CAPMAP_REGISTRY_DIR",
                                               registry.CAPABILITIES_DIR))
    parser.add_argument("--store-dir",
                        default=os.environ.get("CAPMAP_STORE_DIR",
                                               store.DEFAULT_BASE_DIR))
    parser.add_argument("--seed-only", action="store_true",
                        help="establish baseline status for every capability "
                             "without sending anything")
    parser.add_argument("--dry-run", action="store_true",
                        help="render everything that would be sent; send "
                             "nothing, write no heartbeat, record no digest")
    parser.add_argument("--check-liveness", action="store_true",
                        help="dead-man's switch over capmap's OWN heartbeat "
                             "(for an independent launchd job)")
    parser.add_argument("--heartbeat-file",
                        help="default: <store-dir>/" + HEARTBEAT_FILENAME)
    parser.add_argument("--heartbeat-field", default=DEFAULT_HEARTBEAT_FIELD)
    parser.add_argument("--cadence-seconds", type=int,
                        default=DEFAULT_CADENCE_SECONDS)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.check_liveness:
            result = check_liveness(
                store_dir=args.store_dir, heartbeat_file=args.heartbeat_file,
                heartbeat_field=args.heartbeat_field,
                cadence_seconds=args.cadence_seconds, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(result, default=str, indent=2, sort_keys=True))
            else:
                print("capmap liveness: {} (age {}) — {}".format("STALE" if result.get("stale") else "alive",
                         result.get("age_seconds"), result.get("action")))
            return EXIT_OK

        seeding = seeding_required(args.store_dir, args.seed_only)
        alerter = _build_alerter(dry_run=args.dry_run, seed_only=seeding,
                                 store_dir=args.store_dir)
        summary = run(registry_dir=args.registry_dir, store_dir=args.store_dir,
                      dry_run=args.dry_run, seed_only=args.seed_only,
                      alerter=alerter, heartbeat_file=args.heartbeat_file,
                      heartbeat_field=args.heartbeat_field)
    except RunnerError as exc:
        print(f"capmap-run: {exc}", file=sys.stderr)
        return EXIT_RUNNER_FAILED
    except Exception as exc:                    # noqa: BLE001 — launchd needs a code
        print(f"capmap-run: unexpected {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_RUNNER_FAILED

    if args.json:
        print(json.dumps(summary, default=str, indent=2, sort_keys=True))
    else:
        print(render(summary, alerter))
    return exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
