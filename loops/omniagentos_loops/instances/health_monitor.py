"""W3 health monitor & self-heal loop instance — error detection + launchctl kickstart.

Trigger: 10-min tick + health-sentinel snapshot.
Inputs: var/health-sentinel/latest.json (latest component statuses) + var/log/*.log.

Template: monitor_diagnose_repair_verify
- monitor: read snapshot; emit component-level observations.
- diagnose: known-remedy vs unknown (deterministic first; LLM only for ambiguous).
- repair (T1 auto): ONLY launchctl kickstart for allowlisted labels.
  Allowlist: api, runner, routines, health-sentinel, reflection-nightly,
  reflection-watchdog, agent-watchdog, backlog-executor, hygiene,
  planner-canary, swarm-optimizer, fable-curator.
  Anything else → escalate (T3, parks + notifies).
- verify: re-probe the launchd job the repair acted on; record MTTR.
- Retry: transient ×2 then park.
- Incident identity: component+failure-signature+day (dedupe via receipt key).

Two contracts this module must keep, both learned from live failures:

* **The diagnosis is the effect's payload and is a FUNCTION OF ITS INCIDENT ID.**
  ``repair``/``escalate`` receive ``(remedy, diagnosis)`` and nothing else, and
  every field of a diagnosis is derived from the same (component, signature,
  day) preimage as its incident id. That is not tidiness: the approvals row is
  keyed by (remedy, incident) and records the args digest at creation, so a
  field that drifts between the tick that ASKS and the tick that RESUMES makes
  an approved escalation unexecutable forever. Never put a timestamp, a log
  tail or a "20 minutes ago" string in the diagnosis.
* **Verification is an external probe, never the sentinel snapshot.**
  ``var/health-sentinel/latest.json`` is rewritten every 30 minutes; seconds
  after a kickstart it still describes the outage that triggered it, so reading
  it back inside the same tick can only ever report the failure again.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from datetime import UTC, date, datetime
from typing import Any

from omniagentos_loops import REPO_ROOT
from omniagentos_loops.contracts import RiskTier
from omniagentos_loops.tools import LoopTool, ToolRegistry

logger = logging.getLogger(__name__)

# ========== constants ==========

__all__ = ["REPO_ROOT", "KICKSTART_ALLOWLIST", "register"]

# Allowed remedies: only these labels can be kickstarted automatically
API_LABEL = "com.omniagentos.api"
KICKSTART_ALLOWLIST = frozenset((
    API_LABEL,
    "com.omniagentos.runner",
    "com.omniagentos.routines",
    "com.omniagentos.health-sentinel",
    # Fleet repair labels (previously decayed, now enabled for auto-repair)
    "com.omniagentos.reflection-nightly",
    "com.omniagentos.reflection-watchdog",
    "com.omniagentos.agent-watchdog",
    "com.omniagentos.backlog-executor",
    "com.omniagentos.hygiene",
    "com.omniagentos.planner-canary",
    "com.omniagentos.swarm-optimizer",
    "com.omniagentos.fable-curator",
))

#: Verification's bounded wait for a kickstarted component to come back, and the
#: gap between attempts. ``launchctl kickstart`` returns the moment launchd
#: ACCEPTS the request, so an immediate probe reads a job that is still
#: spawning; uvicorn needs a few seconds more before it binds its port.
PROBE_TIMEOUT_S = 25.0
PROBE_INTERVAL_S = 1.0

#: The health sentinel is a launchd job with ``StartInterval 1800``
#: (``scripts/health-sentinel/install.sh``: ``HEALTH_SENTINEL_INTERVAL:-1800``,
#: rendered into ``com.omniagentos.health-sentinel.plist``), so a snapshot
#: is expected every 30 minutes. Blindness starts at TWO intervals: one missed
#: pass is a blip, two means the sentinel is not running and the file on disk is
#: a MEMORY, not an observation. This is the whole reason the threshold is
#: derived from the cadence instead of picked — it must move when the job does.
SENTINEL_INTERVAL_S = 1800
SNAPSHOT_MAX_AGE_S = 2 * SENTINEL_INTERVAL_S

#: Tolerated clock skew for a snapshot stamped in the FUTURE. Anything beyond it
#: is not freshness, it is a broken clock or a hand-edited file, and trusting it
#: would make the staleness check bypassable by a single field.
SNAPSHOT_FUTURE_SKEW_S = 60

#: The API's readiness endpoint — the same one the health sentinel checks
#: (``scripts/health-sentinel/health_sentinel.py::check_api``). Deliberately a
#: constant rather than ``os.environ["OMNIAGENTOS_API_PORT"]``: the loop worker is
#: launched with a scrubbed environment, so an env read here would always take
#: the default anyway, and this package names no environment variable outside
#: ``OMNIAGENTOS_LOOPS_*`` (``tests/test_security_seam.py``).
API_HEALTH_URL = "http://127.0.0.1:8485/api/health"

# Component status thresholds for diagnosis
COMPONENT_STATUS = {
    "api": {"critical": False, "can_kickstart": True},
    "runner": {"critical": False, "can_kickstart": True},
    "scheduler": {"critical": False, "can_kickstart": False},
    "claude_pool": {"critical": False, "can_kickstart": False},
    "reflection": {"critical": False, "can_kickstart": False},
    "launchd": {"critical": True, "can_kickstart": True},
}

# ========== Monitor: read snapshot & logs ==========


def _snapshot_age_seconds(data: dict[str, Any]) -> float | None:
    """Seconds since the sentinel WROTE this snapshot, or None if it cannot say.

    The field is ``ts`` — top level, ISO-8601 with ``Z``
    (``health_sentinel.build_snapshot``). This function used to be absent and
    the caller read ``data["timestamp"]``, a key the sentinel has never
    written: the freshness evidence was sitting in the file, being fetched, and
    thrown away.
    """
    raw = str(data.get("ts") or data.get("timestamp") or "")
    if not raw:
        return None
    try:
        stamped = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamped).total_seconds()


def _read_snapshot() -> dict[str, Any]:
    """Read the latest health-sentinel snapshot, and REFUSE a stale one.

    A snapshot older than :data:`SNAPSHOT_MAX_AGE_S` is not health data: if the
    sentinel dies, ``latest.json`` keeps saying every check was ok at the moment
    it stopped looking, and a monitor that trusts it reports a perfectly healthy
    fleet forever while nothing is watching. Age is therefore part of
    availability, exactly like readability — an unreadable file and an
    unmaintained one are the same fact ("I cannot see"), and this module already
    renders that as a failed check rather than as no-failures.
    """
    snapshot_path = REPO_ROOT / "var" / "health-sentinel" / "latest.json"
    if not snapshot_path.exists():
        return {
            "available": False,
            "checks": [],
            "error": "snapshot file not found",
        }
    try:
        with open(snapshot_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {
            "available": False,
            "checks": [],
            "error": f"snapshot read failed: {e}",
        }

    age = _snapshot_age_seconds(data)
    # Ages are written with an ``s`` suffix so ``_signature`` normalizes them to
    # ``N``: a growing age must not mint a new incident (and therefore a new
    # approval) on every tick.
    if age is None:
        return {
            "available": False,
            "checks": [],
            "ts": data.get("ts"),
            "error": "snapshot carries no readable 'ts'; its age cannot be established",
        }
    if age > SNAPSHOT_MAX_AGE_S:
        return {
            "available": False,
            "checks": [],
            "ts": data.get("ts"),
            "age_seconds": age,
            "error": (
                f"health-sentinel snapshot is {int(age)}s old "
                f"(max {SNAPSHOT_MAX_AGE_S}s = 2 sentinel intervals) — "
                "the sentinel is not writing, so this file is a memory"
            ),
        }
    if age < -SNAPSHOT_FUTURE_SKEW_S:
        return {
            "available": False,
            "checks": [],
            "ts": data.get("ts"),
            "age_seconds": age,
            "error": (
                f"health-sentinel snapshot is stamped {int(-age)}s in the FUTURE — "
                "a clock this wrong cannot establish freshness"
            ),
        }
    return {
        "available": True,
        "checks": data.get("checks", []),
        "ts": data.get("ts"),
        "age_seconds": age,
    }


def _tail_logs(num_lines: int = 50) -> dict[str, list[str]]:
    """Tail recent log files to help diagnosis."""
    log_dir = REPO_ROOT / "var" / "log"
    log_files = {
        "routines": log_dir / "routines.log",
        "health_sentinel": log_dir / "health-sentinel.log",
        "api": log_dir / "api.log",
    }

    tails: dict[str, list[str]] = {}
    for name, path in log_files.items():
        if not path.exists():
            tails[name] = []
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tails[name] = [line.rstrip() for line in lines[-num_lines:]]
        except Exception as e:
            tails[name] = [f"tail failed: {e}"]
    return tails


def monitor_health(params: dict[str, Any]) -> dict[str, Any]:
    """T0 read: snapshot + tail logs.

    Args:
        params: unused (kept for template compatibility)

    Returns:
        Snapshot dict with checks list and log tails.
    """
    snapshot = _read_snapshot()
    logs = _tail_logs(num_lines=50)

    # A snapshot we cannot read is BLINDNESS, not health. Reporting an empty
    # failure list here would make the self-heal loop announce all-clear
    # exactly when it can see nothing -- the non-result-as-favourable shape
    # this repo has been bitten by repeatedly. Surface it as its own failed
    # check so diagnose routes it to escalate (no remedy matches it).
    if not snapshot.get("available"):
        return {
            "snapshot": snapshot,
            "failed_checks": [
                {
                    "name": "health_snapshot",
                    "status": "fail",
                    "evidence": snapshot.get("error", "snapshot unavailable"),
                }
            ],
            "logs": logs,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # Extract failed checks for easier access
    failed_checks = [
        c for c in snapshot.get("checks", [])
        if c.get("status") in ("fail", "warn")
    ]

    return {
        "snapshot": snapshot,
        "failed_checks": failed_checks,
        "logs": logs,
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ========== Diagnose: classify failures ==========

_REMEDY_PATTERNS = {
    # Pattern matching for deterministic diagnosis
    "api_dead": {
        "component": "api",
        "status_filter": "fail",
        "evidence_patterns": [
            r"unreachable",
            r"HTTP \d{3}",
            r"connection refused",
        ],
        "remedy": "kickstart_api",
        "label": "com.omniagentos.api",
    },
    "runner_dead": {
        "component": "runner",
        "status_filter": "fail",
        "evidence_patterns": [
            r"no.*omniagentos\.runner",
            r"queued runs will never move",
        ],
        "remedy": "kickstart_runner",
        "label": "com.omniagentos.runner",
    },
    "scheduler_stale": {
        "component": "scheduler",
        "status_filter": "fail",
        "evidence_patterns": [
            r"older than",
            r"ticked.*ago",
        ],
        "remedy": "unknown_scheduler",  # Can't auto-fix scheduler stale
        "label": None,
    },
    "launchd_issue": {
        "component": "launchd",
        "status_filter": "fail",
        "evidence_patterns": [
            r"not loaded",
            r"not installed",
            r"exited \d+",
        ],
        "remedy": "unknown_launchd",  # Needs human judgment
        "label": None,
    },
}


def _match_remedy(check: dict[str, Any]) -> tuple[str, str | None]:
    """Deterministic pattern matching for remedy classification.

    Returns:
        (remedy_id, launchd_label) or ("unknown_failure", None)
    """
    name = check.get("name", "")
    status = check.get("status", "")
    evidence = check.get("evidence", "").lower()

    for _remedy_key, pattern in _REMEDY_PATTERNS.items():
        if pattern["component"] != name:
            continue
        if status != pattern.get("status_filter"):
            continue

        # Check evidence patterns
        for pat in pattern.get("evidence_patterns", []):
            if re.search(pat, evidence):
                return pattern.get("remedy", "unknown"), pattern.get("label")

    # No pattern matched
    return "unknown_failure", None


def _signature(evidence: Any) -> str:
    """Time-normalized, length-capped evidence: the stable identity of a failure.

    The SAME string is the middle field of the incident id and the ``evidence``
    field of the diagnosis, on purpose — see :func:`diagnose_failure`. If these
    two ever diverge, an approvals row can outlive the arguments it authorised.
    """
    return re.sub(r"\d+[smhd]", "N", str(evidence or "unknown").lower())[:100]


def _compute_incident_id(check: dict[str, Any], remedy: str) -> str:
    """Business key: component+failure-signature+day.

    Same failure across ticks on the same day = same incident (dedup).
    New day or new signature = new incident (may repair again).
    """
    component = check.get("name", "unknown")
    day = date.today().isoformat()
    return f"{component}:{_signature(check.get('evidence', 'unknown'))}:{day}"


def _is_actionable(label: str | None) -> bool:
    """True when this failure has an auto-remedy the repair tool may execute.

    The code allowlist, not the instance's ``allowed_remedies`` param: this is
    "is there a remedy at all", and the template still enforces the instance's
    (possibly narrower) allowlist before anything runs.
    """
    return bool(label) and label in KICKSTART_ALLOWLIST


def _failure_rank(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
    """Sort key for one failed check. Lower sorts first.

    ``(actionable, severity, criticality, snapshot order)``:

    1. **Actionable first.** A failure we can auto-repair outranks one we can
       only escalate — that is the whole point of the loop, and escalation
       parks the tick, so an unactionable failure examined first BLOCKS every
       repairable failure behind it. Live: ``reflection`` (no remedy) parked W3
       on the same approval for five consecutive ticks while ``providers`` and
       ``launchd`` were never diagnosed at all.
    2. **fail before warn** — the sentinel's own verdict about this
       observation, which is fresher than any static opinion we hold.
    3. **critical components before the rest** (``COMPONENT_STATUS``).
    4. **snapshot order**, so the choice is deterministic and testable.
    """
    index, check = indexed
    _remedy, label = _match_remedy(check)
    component = COMPONENT_STATUS.get(str(check.get("name") or ""), {})
    return (
        0 if _is_actionable(label) else 1,
        0 if check.get("status") == "fail" else 1,
        0 if component.get("critical") else 1,
        index,
    )


def _ordered_failures(failed_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every failed check, most-worth-acting-on first (see :func:`_failure_rank`)."""
    return [check for _index, check in sorted(enumerate(failed_checks), key=_failure_rank)]


def diagnose_failure(snapshot: dict[str, Any]) -> dict[str, Any]:
    """T0 read: classify failures and return the ONE incident this tick acts on.

    Considers EVERY failed check and picks the highest-ranked one rather than
    the first (head-of-line blocking, see :func:`_failure_rank`). One incident
    per tick is deliberate: one tick = one approval = one receipt = one
    verifiable outcome.

    The returned diagnosis is the payload of the repair/escalate effect, so
    every field here is a pure function of the incident id's preimage
    (component, normalized signature, day) plus the remedy it selects — which
    is itself part of the effect's business key. Nothing that drifts between
    ticks may be added: the args digest is bound into the approvals row.

    Args:
        snapshot: from monitor (includes failed_checks, logs)

    Returns:
        Diagnosis dict: remedy, label, incident, component, evidence.
    """
    failed_checks = list(snapshot.get("failed_checks") or [])

    # No failures = healthy
    if not failed_checks:
        return {
            "remedy": "",  # Empty remedy = no action needed
            "incident": None,
            "healthy": True,
        }

    check = _ordered_failures(failed_checks)[0]
    remedy, label = _match_remedy(check)
    return {
        "remedy": remedy,
        "label": label,
        "incident": _compute_incident_id(check, remedy),
        "component": check.get("name"),
        # The normalized signature, NOT the raw evidence: the raw string can
        # drift ("restarted 30s ago") while the incident id stays put, and a
        # diagnosis that drifts inside one incident invalidates the approval
        # that incident already minted.
        "evidence": _signature(check.get("evidence")),
    }


# ========== Repair: launchctl kickstart (T1 auto) ==========

#: Captured before any test can patch it, so the guard below can tell a FAKED
#: ``subprocess.run`` (fine — that is what the unit tests do) from one that
#: would really reach launchd.
_REAL_SUBPROCESS_RUN = subprocess.run


def _get_uid() -> int | None:
    """Get the current user UID for launchctl kickstart."""
    try:
        return os.getuid()
    except Exception:
        return None


def _live_effect_refusal() -> str | None:
    """Refuse a REAL ``launchctl`` issued from inside a test process.

    W3's ``gate_command`` is a pytest suite that drives these production tools,
    and the gate runs on the serving box. On 2026-08-05 one gate test iterated
    ``KICKSTART_ALLOWLIST`` calling ``repair_component`` without faking
    ``subprocess.run``: every gate tick issued a real ``launchctl kickstart -k``
    at all twelve live jobs, SIGTERMing the fleet. The API was the one service
    that could not survive it and sat at ``exit=-15``.

    Hermetic tests remain free to exercise the whole tool: a patched
    ``subprocess.run`` is not the real one, so this returns ``None``. It trips
    only when the call would actually hit launchd from a test process.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return None
    if subprocess.run is not _REAL_SUBPROCESS_RUN:
        return None  # faked at the boundary — the test is hermetic
    return (
        "refusing a real launchctl kickstart from a test process: fake "
        "subprocess.run in this test (see _live_effect_refusal)"
    )


def repair_component(remedy: str, diagnosis: dict[str, Any]) -> dict[str, Any]:
    """T1 effect: run launchctl kickstart for allowlisted labels.

    Args:
        remedy: remedy id from diagnosis (must be "kickstart_*")
        diagnosis: the diagnosis the template routed here — this tool reads the
            launchd label out of it. It used to be handed the monitor SNAPSHOT
            and look for ``snapshot["diagnosis"]``, a key nothing produced, so
            ``label`` was always ``None`` and the allowlist check below refused
            every repair in production while the fixture-built unit tests passed.

    Returns:
        Result dict with success/failure, output, mttr_start.
    """
    label = (diagnosis or {}).get("label")

    # Safety: label MUST be in allowlist
    if not label or label not in KICKSTART_ALLOWLIST:
        return {
            "success": False,
            "error": f"remedy {remedy!r}: label {label!r} not in allowlist {KICKSTART_ALLOWLIST}",
        }

    uid = _get_uid()
    if uid is None:
        return {
            "success": False,
            "error": "could not determine user UID",
        }

    refusal = _live_effect_refusal()
    if refusal is not None:
        return {"success": False, "label": label, "error": refusal}

    # Build and run kickstart command
    cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
    mttr_start = datetime.now(UTC).isoformat()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )

        if result.returncode == 0:
            return {
                "success": True,
                "label": label,
                "command": " ".join(cmd),
                "stdout": result.stdout,
                "mttr_start": mttr_start,
            }
        else:
            return {
                "success": False,
                "label": label,
                "command": " ".join(cmd),
                "returncode": result.returncode,
                "stderr": result.stderr,
            }
    except Exception as e:
        return {
            "success": False,
            "label": label,
            "error": f"kickstart failed: {e}",
        }


# ========== Escalate: unknown remedy (T3, parks) ==========

def escalate_unknown(remedy: str, diagnosis: dict[str, Any]) -> dict[str, Any]:
    """T3 effect: record escalation for unknown remedies.

    This is always called for unknown remedies; LangGraph will park it for
    human approval due to the T3 tier. We record it but never execute blind.

    Args:
        remedy: remedy id that was unknown
        diagnosis: the diagnosis being escalated. Same defect as
            :func:`repair_component`: reading it out of the snapshot meant every
            approval a human was asked to decide carried
            ``component="unknown", evidence="no evidence"``.

    Returns:
        Escalation record (for audit trail).
    """
    diagnosis = diagnosis or {}
    return {
        "escalated": True,
        "remedy": remedy,
        "component": diagnosis.get("component", "unknown"),
        "evidence": diagnosis.get("evidence", "no evidence"),
        "incident": diagnosis.get("incident"),
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ========== Verify: probe the repaired job ==========

def _probe_api() -> tuple[bool, str]:
    """Does the API ANSWER? Not "did launchd spawn something"."""
    url = API_HEALTH_URL
    try:
        with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
            code = int(response.getcode() or 0)
            body = response.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001 - any failure is "not ready yet"
        return False, f"{url}: {type(exc).__name__}: {exc}"
    if code != 200:
        return False, f"{url}: HTTP {code}"
    try:
        status = str((json.loads(body) or {}).get("status") or "unknown")
    except ValueError:
        status = "unparseable"
    return True, f"{url}: HTTP 200 status={status}"


def _probe_job(label: str) -> tuple[bool, str]:
    """Is the launchd job *running*, per launchd itself?

    ``state = running`` only. A kickstart returns as soon as launchd accepts it,
    so the job is briefly ``state = xpcproxy`` with a pid already allocated —
    accepting a bare pid would call a job "recovered" while it is still being
    spawned, which is the same optimism this loop exists to remove.
    """
    uid = _get_uid()
    if uid is None:
        return False, "could not determine user UID"
    try:
        probe = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        return False, f"launchctl print failed: {exc}"
    if probe.returncode != 0:
        return False, f"launchctl print rc={probe.returncode}: {probe.stderr.strip()[:200]}"
    state = re.search(r"^\s*state\s*=\s*(\S+)", probe.stdout, re.MULTILINE)
    pid = re.search(r"^\s*pid\s*=\s*(\d+)", probe.stdout, re.MULTILINE)
    running = bool(state) and state.group(1) == "running"
    return running, f"state={state.group(1) if state else 'unknown'} pid={pid.group(1) if pid else '-'}"


def _probe_label(label: str) -> dict[str, Any]:
    """Is the component *label* names healthy RIGHT NOW? External evidence.

    Deliberately NOT the sentinel snapshot: that file is rewritten on a
    30-minute interval, so within the tick that repaired something it can only
    still describe the outage that triggered the repair.

    The probe asks what "healthy" MEANS for the component — the API has to
    answer, everything else has to be a running job — because "launchd says it
    is running" and "the service works" differ by exactly the outage being
    repaired, and the receipt guard means a mis-verified incident is not
    revisited until the next day.

    Bounded-wait: a kickstart returns the instant launchd accepts it, so the
    first attempt reads a process that is still starting up.
    """
    deadline = time.monotonic() + PROBE_TIMEOUT_S
    while True:
        ok, evidence = _probe_api() if label == API_LABEL else _probe_job(label)
        if ok or time.monotonic() >= deadline:
            return {"running": ok, "evidence": evidence}
        time.sleep(PROBE_INTERVAL_S)


def _mttr(mttr_start: Any) -> dict[str, Any]:
    """Elapsed seconds since the kickstart, when the receipt recorded one.

    A replayed receipt (or a repair result from an older code path) can carry no
    ``mttr_start``; ``datetime.fromisoformat(None)`` raised TypeError *inside*
    the verify read, which turned a successful repair into a crashed tick.
    """
    end = datetime.now(UTC)
    try:
        started = datetime.fromisoformat(str(mttr_start))
    except (TypeError, ValueError):
        return {"mttr_seconds": None, "mttr_start": mttr_start, "mttr_end": end.isoformat()}
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return {
        "mttr_seconds": (end - started).total_seconds(),
        "mttr_start": mttr_start,
        "mttr_end": end.isoformat(),
    }


def verify_repair(remedy: str, result: dict[str, Any]) -> dict[str, Any]:
    """T0 read: prove the repair's post-condition holds. The tick's verdict.

    ``verified`` is load-bearing: the template turns it into the tick's status
    (``common.verification_outcome``), so anything other than an explicit True
    here settles the tick as FAILED rather than completed. Say True only about
    something actually observed.

    Args:
        remedy: the remedy that was applied (or "" if healthy)
        result: repair result (or None if healthy)

    Returns:
        Verification dict with post-repair status.
    """
    if not remedy:
        # No repair was needed
        return {
            "verified": True,
            "state": "healthy",
        }

    if remedy.startswith("unknown"):
        # Unknown remedy was escalated, not repaired — this node is not on the
        # escalate path, so reaching it means the routing itself is wrong.
        return {
            "verified": False,
            "state": "escalated",
            "remedy": remedy,
        }

    result = result or {}
    if not result.get("success"):
        return {
            "verified": False,
            "state": "repair_failed",
            "error": str(result.get("error") or "no repair result"),
        }

    label = str(result.get("label") or "")
    if not label:
        return {
            "verified": False,
            "state": "unverified",
            "remedy": remedy,
            "error": "repair result carries no launchd label to probe",
        }

    probe = _probe_label(label)
    if not probe.get("running"):
        return {
            "verified": False,
            "state": "still_failing",
            "label": label,
            "evidence": probe.get("evidence"),
        }
    return {
        "verified": True,
        "state": "recovered",
        "label": label,
        "component": (result.get("component") or label.rsplit(".", 1)[-1]),
        "evidence": probe.get("evidence"),
        **_mttr(result.get("mttr_start")),
    }


# ========== Receipt-level verification ==========

def _repair_verified(result: Any, args: dict[str, Any]) -> dict[str, Any]:
    """External evidence that the kickstart took effect. Declared as ``verify=``.

    Without it, a ``launchctl kickstart`` that exits 0 while the job stays down
    is filed as a SUCCEEDED receipt — and because the business key is
    component+signature+day, that receipt then suppresses every retry until the
    day rolls over: the service stays down and the incident reads as handled.
    The tick's status already refuses this case (``verify_repair`` probes too),
    but a status and a receipt that disagree about the same tick means the loop
    argues with itself and the receipt wins the next tick.

    Two properties this function must have, both learned from the code above it:

    * It uses the FULL component probe, not a bare ``launchctl print``. This
      runs milliseconds after the kickstart, when launchd reports the job as
      ``state = xpcproxy`` — still spawning. A probe without
      :data:`PROBE_TIMEOUT_S`'s bounded wait would file a working repair as
      failed and kickstart a healthy service again on the next tick.
    * It NEVER raises. The receipt guard reads an exception from a verification
      predicate as "the effect ran and its outcome cannot be established"
      (fail-closed, row left claimed). For a probe that is the wrong answer:
      "I looked and it is not running" is a verdict, and every path here returns
      one.
    """
    label = str(
        (args.get("diagnosis") or {}).get("label")
        or (result or {}).get("label")
        or ""
    )
    if not label:
        return {"ok": False, "detail": "repair produced no launchd label to probe"}
    probe = _probe_label(label)
    return {
        "ok": bool(probe.get("running")),
        "detail": f"{label}: {probe.get('evidence') or 'no evidence'}",
    }


# ========== Idempotency keys ==========

def _repair_idempotency_key(args: dict[str, Any]) -> str:
    """Business key for repair/escalate: ``remedy:incident``.

    Must stay byte-identical to the template's ``_repair_key`` — the receipt and
    the approvals row are both keyed by it. (The runtime derives the business
    key from the TEMPLATE's ``key_fn``; this stays defined and correct so the
    two cannot silently disagree if that ever changes.)
    """
    diagnosis = args.get("diagnosis") or {}
    return f"{args.get('remedy', '')}:{diagnosis.get('incident') or 'now'}"


# ========== Registration ==========

def register(ctx: Any) -> None:
    """Register W3 loop tools on ctx.tools.

    Tools:
    - monitor (T0): read snapshot
    - diagnose (T0): classify failure → remedy
    - repair (T1): launchctl kickstart (allowlisted only)
    - escalate (T3): park unknown remedies
    - verify (T0): re-check after repair

    Args:
        ctx: LoopContext with tools registry
    """
    if not hasattr(ctx, "tools"):
        ctx.tools = ToolRegistry()

    ctx.tools.register(
        LoopTool(
            name="monitor",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "monitor",  # Reads are not receipted
            call=monitor_health,
            description="Read health-sentinel snapshot + tail logs",
        )
    )

    ctx.tools.register(
        LoopTool(
            name="diagnose",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "diagnose",  # Reads are not receipted
            call=diagnose_failure,
            description="Classify failure and return remedy (deterministic first)",
        )
    )

    # ``verify=`` is the receipt guard's external-evidence hook: it decides
    # whether the RECEIPT records this attempt as succeeded or failed, which is
    # what governs whether the next tick may try again. It is passed
    # conditionally only because the field itself arrives with the receipt-
    # outcome lane; the predicate is defined unconditionally so this module
    # keeps ONE definition of "did the kickstart actually work". Once that field
    # is everywhere, inline it.
    repair_fields: dict[str, Any] = {}
    if "verify" in {field.name for field in dataclasses.fields(LoopTool)}:
        repair_fields["verify"] = _repair_verified

    ctx.tools.register(
        LoopTool(
            name="repair",
            tier=RiskTier.T1,
            idempotency_key=_repair_idempotency_key,
            call=repair_component,
            description="launchctl kickstart for allowlisted labels",
            **repair_fields,
        )
    )

    ctx.tools.register(
        LoopTool(
            name="escalate",
            tier=RiskTier.T3,
            idempotency_key=_repair_idempotency_key,
            call=escalate_unknown,
            description="Park unknown remedies for human approval",
        )
    )

    ctx.tools.register(
        LoopTool(
            name="verify",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "verify",  # Reads are not receipted
            call=verify_repair,
            description="Probe the repaired launchd job (external post-condition)",
        )
    )
