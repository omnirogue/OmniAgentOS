"""Append-only capmap run records and atomic current-state snapshots.

``staleness_slo_seconds`` is intentionally an *absolute* threshold: a row is
stale when now minus last_verified is greater than that number of seconds.  It
is not multiplied by cadence; keeping the threshold explicit makes the
registry's SLO reviewable and avoids hidden arithmetic in alert paths.
"""

import datetime
import json
import os

HOME = "/Users/youruser"
DEFAULT_BASE_DIR = HOME + "/Work/Ops/var/capmap"
RUNS_FILENAME = "runs.jsonl"
STATE_FILENAME = "state.json"

OK = "OK"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
CANNOT_EVALUATE = "CANNOT_EVALUATE"
UNVERIFIED = "UNVERIFIED"
STALE = "STALE"
STATUSES = {OK, DEGRADED, DOWN, CANNOT_EVALUATE, UNVERIFIED, STALE}
_SEMANTIC_TO_STATUS = {
    "ok": OK,
    "degraded": DEGRADED,
    "down": DOWN,
    "cannot_evaluate": CANNOT_EVALUATE,
}


def _base_dir(base_dir=None):
    return os.path.expanduser(base_dir or DEFAULT_BASE_DIR)


def _paths(base_dir=None):
    base = _base_dir(base_dir)
    return os.path.join(base, RUNS_FILENAME), os.path.join(base, STATE_FILENAME)


def now_iso(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def append_run(record, base_dir=None):
    """Append one JSONL record.  Existing run history is never rewritten."""
    runs_path, _ = _paths(base_dir)
    os.makedirs(os.path.dirname(runs_path), exist_ok=True)
    required = ("ts", "capability_id", "exit_code", "status", "latency_ms", "metric", "evidence")
    missing = [field for field in required if field not in record]
    if missing:
        raise ValueError("run record missing fields: {}".format(", ".join(missing)))
    if record["status"] not in STATUSES:
        raise ValueError("invalid run status {!r}".format(record["status"]))
    with open(runs_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_snapshot(rows, base_dir=None):
    """Atomically replace current state using tmp + fsync + os.replace."""
    _, state_path = _paths(base_dir)
    directory = os.path.dirname(state_path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{state_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, state_path)


def read_snapshot(base_dir=None):
    """Return current state, or an empty mapping before the first verification."""
    _, state_path = _paths(base_dir)
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def compute_status(capability, exit_code_or_status):
    """Map a declared observed value to a store status; undeclared means unverified."""
    verification = capability.get("verification")
    if not isinstance(verification, dict) or verification.get("type") not in ("command", "snapshot_field"):
        return UNVERIFIED
    semantics = capability.get("exit_semantics")
    if not isinstance(semantics, dict):
        return UNVERIFIED
    if verification["type"] == "command":
        key = str(exit_code_or_status)
    else:
        key = str(exit_code_or_status or "").upper()
    declared = semantics.get(key, semantics.get("default"))
    return _SEMANTIC_TO_STATUS.get(declared, UNVERIFIED)


def is_stale(capability, last_verified_iso, now=None):
    """Whether an absolute ``staleness_slo_seconds`` threshold has been exceeded."""
    verified = parse_iso(last_verified_iso)
    if verified is None:
        return False
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    threshold = capability.get("staleness_slo_seconds")
    if not isinstance(threshold, (int, float)) or threshold <= 0:
        return False
    return (current.astimezone(datetime.timezone.utc) - verified).total_seconds() > threshold
