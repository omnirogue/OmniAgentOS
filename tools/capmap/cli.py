#!/usr/bin/env python3
"""The capmap command-line interface.

Verify exit codes are deliberately diagnostic: 0 means all evaluated entries
are OK (or explicitly named by --accept); 2 means a DOWN, CANNOT_EVALUATE, or
STALE result; 3 means UNVERIFIED with no harder result; and 4 means only
DEGRADED results remain.  Thus an unrecognised exit/status cannot be mistaken
for green, while a deliberate "could not evaluate" is distinct from an outage.
"""

import argparse
import json
import os
import subprocess
import sys
import time

import registry
import store

EXIT_OK = 0
EXIT_HARD_FAILURE = 2
EXIT_UNVERIFIED = 3
EXIT_DEGRADED = 4


def _iso_now():
    return store.now_iso()


def _field(data, dotted):
    value = data
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _verification_result(capability):
    """Evaluate one spec without printing opaque probe values."""
    spec = capability.get("verification") or {}
    kind = spec.get("type")
    started = time.monotonic()
    result = {"observed": None, "evidence": None, "last_verified": _iso_now(), "metric": {}}
    if kind == "command":
        argv = spec.get("argv")
        command = spec.get("command")
        try:
            if argv is not None:
                completed = subprocess.run(argv, capture_output=True,
                                           timeout=spec.get("timeout_seconds", 30))
            elif command:
                completed = subprocess.run(command, shell=True, capture_output=True,
                                           timeout=spec.get("timeout_seconds", 30))
            else:
                completed = None
            if completed is not None:
                result["observed"] = completed.returncode
        except (OSError, subprocess.TimeoutExpired):
            # No synthetic success code: lacking an explicit mapping resolves UNVERIFIED.
            result["observed"] = None
        result["metric"] = {"verification_type": "command"}
    elif kind == "snapshot_field":
        source = os.path.expanduser(spec.get("source", ""))
        result["evidence"] = source or None
        try:
            with open(source, encoding="utf-8") as fh:
                snapshot = json.load(fh)
            result["observed"] = _field(snapshot, spec.get("field", ""))
            checked_at = _field(snapshot, spec.get("checked_at_field", ""))
            if store.parse_iso(checked_at) is not None:
                result["last_verified"] = checked_at
        except (OSError, ValueError, json.JSONDecodeError):
            result["observed"] = None
        result["metric"] = {"verification_type": "snapshot_field"}
    else:
        result["metric"] = {"verification_type": kind or "unset"}
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _effective_status(capability, row):
    status = (row or {}).get("status", store.UNVERIFIED)
    if store.is_stale(capability, (row or {}).get("last_verified")):
        return store.STALE
    return status if status in store.STATUSES else store.UNVERIFIED


def _select(entries, args):
    selected = entries
    if getattr(args, "company", None):
        selected = [entry for entry in selected if entry["company"] == args.company]
    if getattr(args, "kind", None):
        selected = [entry for entry in selected if entry["kind"] == args.kind]
    if getattr(args, "id", None):
        selected = [entry for entry in selected if entry["id"] == args.id]
    return selected


def _rows(entries, snapshot):
    return [{"company": cap["company"], "id": cap["id"],
             "status": _effective_status(cap, snapshot.get(cap["id"])),
             "last_verified": snapshot.get(cap["id"], {}).get("last_verified")}
            for cap in entries]


def _render_table(rows):
    headers = ("company", "id", "status", "last_verified")
    body = [[str(row.get(header) or "-") for header in headers] for row in rows]
    widths = [len(header) for header in headers]
    for row in body:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    lines = [" | ".join(header.ljust(width) for header, width in zip(headers, widths)),
             "-+-".join("-" * width for width in widths)]
    lines.extend(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)) for row in body)
    return "\n".join(lines)


def _emit(rows, as_json):
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(_render_table(rows))


def _verify(entries, args, base_dir):
    state = store.read_snapshot(base_dir)
    rows = []
    for capability in entries:
        checked = _verification_result(capability)
        status = store.compute_status(capability, checked["observed"])
        if status != store.UNVERIFIED and store.is_stale(capability, checked["last_verified"]):
            status = store.STALE
        run = {
            "ts": _iso_now(), "capability_id": capability["id"],
            "exit_code": checked["observed"] if capability["verification"].get("type") == "command" else None,
            "status": status, "latency_ms": checked["latency_ms"], "metric": checked["metric"],
            "evidence": checked["evidence"],
        }
        store.append_run(run, base_dir)
        state[capability["id"]] = {
            "status": status, "last_verified": checked["last_verified"],
            "latency_ms": checked["latency_ms"], "evidence": checked["evidence"],
            "updated_at": run["ts"],
        }
    store.write_snapshot(state, base_dir)
    rows = _rows(entries, state)
    _emit(rows, args.json)
    accepted = set(args.accept or [])
    unresolved = [row for row in rows if row["id"] not in accepted and row["status"] == store.UNVERIFIED]
    hard = [row for row in rows if row["id"] not in accepted and row["status"] in (store.DOWN, store.CANNOT_EVALUATE, store.STALE)]
    degraded = [row for row in rows if row["id"] not in accepted and row["status"] == store.DEGRADED]
    if hard:
        return EXIT_HARD_FAILURE
    if unresolved:
        return EXIT_UNVERIFIED
    if degraded:
        return EXIT_DEGRADED
    return EXIT_OK


def _list(entries, args):
    rows = [{"id": cap["id"], "company": cap["company"], "kind": cap["kind"],
             "what_it_does": cap["what_it_does"][:96]} for cap in entries]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        headers = ("id", "company", "kind", "what_it_does")
        widths = [len(header) for header in headers]
        body = [[row[header] for header in headers] for row in rows]
        for row in body:
            widths = [max(width, len(value)) for width, value in zip(widths, row)]
        print("\n".join([" | ".join(header.ljust(width) for header, width in zip(headers, widths)),
                           "-+-".join("-" * width for width in widths)] +
                          [" | ".join(value.ljust(width) for value, width in zip(row, widths)) for row in body]))
    return EXIT_OK


def _audit(entries, args, base_dir):
    unverified = [cap for cap in entries if (cap.get("verification") or {}).get("type") not in ("command", "snapshot_field")]
    verified = [cap for cap in entries if cap not in unverified]
    state = store.read_snapshot(base_dir) if args.evidence else {}
    payload = {
        "unverified": [{"company": cap["company"], "id": cap["id"],
                          "reason": "no usable verification spec"} for cap in unverified],
        "verified": [{"company": cap["company"], "id": cap["id"],
                      **({"evidence": state.get(cap["id"], {}).get("evidence")} if args.evidence else {})}
                     for cap in verified],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("UNVERIFIED CAPABILITIES")
        if unverified:
            for cap in unverified:
                print("- {} | {} | no usable verification spec".format(cap["company"], cap["id"]))
        else:
            print("- none")
        print("VERIFIED CAPABILITIES")
        for cap in verified:
            line = "- {} | {}".format(cap["company"], cap["id"])
            if args.evidence:
                line += " | evidence: %s" % (state.get(cap["id"], {}).get("evidence") or "-")
            print(line)
    return EXIT_OK


def _required(value, label):
    if value is not None and str(value).strip():
        return value
    try:
        supplied = input(label + ": ").strip()
    except EOFError:
        supplied = ""
    if not supplied:
        raise ValueError("missing required --{}".format(label.lower().replace(" ", "-")))
    return supplied


def _add(args, registry_dir):
    values = {
        "id": _required(args.id, "id"), "company": _required(args.company, "company"),
        "kind": _required(args.kind, "kind"), "host": _required(args.host, "host"),
        "repo": _required(args.repo, "repo"), "what_it_does": _required(args.what, "what"),
        "why_it_matters": _required(args.why, "why"), "blast_radius": _required(args.blast_radius, "blast radius"),
        "owner": _required(args.owner, "owner"), "cadence_seconds": int(_required(args.cadence_seconds, "cadence seconds")),
        "staleness_slo_seconds": int(_required(args.staleness_seconds, "staleness seconds")),
        "escalation_tier": _required(args.escalation_tier, "escalation tier"),
        "verification": {"type": "unset"}, "exit_semantics": {}, "last_verified": None,
        "last_status": store.UNVERIFIED,
    }
    registry._validate(values, "new capability")
    safe_name = values["id"].replace("/", "-")
    path = os.path.join(registry_dir, safe_name + ".json")
    if os.path.exists(path):
        raise ValueError(f"capability file already exists: {path}")
    os.makedirs(registry_dir, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(values, fh, indent=2, sort_keys=True)
        fh.write("\n")
    if args.json:
        print(json.dumps(values, indent=2, sort_keys=True))
    else:
        print(f"created {path}")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(prog="capmap")
    parser.add_argument("--registry-dir", default=os.environ.get("CAPMAP_REGISTRY_DIR", registry.CAPABILITIES_DIR), help=argparse.SUPPRESS)
    parser.add_argument("--store-dir", default=os.environ.get("CAPMAP_STORE_DIR", store.DEFAULT_BASE_DIR), help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--all", action="store_true")
    verify.add_argument("--company")
    verify.add_argument("--kind")
    verify.add_argument("--id")
    verify.add_argument("--accept", action="append", default=[])
    verify.add_argument("--json", action="store_true")

    listing = subparsers.add_parser("list")
    listing.add_argument("--company")
    listing.add_argument("--kind")
    listing.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--company")
    status.add_argument("--json", action="store_true")

    audit = subparsers.add_parser("audit")
    audit.add_argument("--company")
    audit.add_argument("--evidence", action="store_true")
    audit.add_argument("--json", action="store_true")

    add = subparsers.add_parser("add")
    for flag in ("id", "company", "kind", "host", "repo", "owner", "cadence_seconds", "staleness_seconds", "escalation_tier"):
        add.add_argument("--" + flag.replace("_", "-"), dest=flag)
    add.add_argument("--what")
    add.add_argument("--why")
    add.add_argument("--blast-radius")
    add.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "add":
            return _add(args, args.registry_dir)
        entries = registry.load_registry(args.registry_dir)
        entries = _select(entries, args)
        if args.subcommand == "verify":
            return _verify(entries, args, args.store_dir)
        if args.subcommand == "list":
            return _list(entries, args)
        if args.subcommand == "status":
            _emit(_rows(entries, store.read_snapshot(args.store_dir)), args.json)
            return EXIT_OK
        if args.subcommand == "audit":
            return _audit(entries, args, args.store_dir)
    except (ValueError, OSError) as exc:
        print(f"capmap: {exc}", file=sys.stderr)
        return 64
    return 64


if __name__ == "__main__":
    sys.exit(main())
