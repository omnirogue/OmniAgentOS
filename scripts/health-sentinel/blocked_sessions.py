#!/usr/bin/env python3
"""Blocked-session detector — the health-sentinel's first CONSUMER.

The sentinel has reported ``reflection FAILED`` 115 runs in a row and done
nothing about it, because it is a detector with no consumer. This module is one
of the two consumers being bolted on. It answers exactly one question, cheaply,
every five minutes:

    Is a Claude CLI session sitting on a tool call that nobody will ever answer?

Measured cost of the miss: the fully-equipped ``~/.claude`` account still burns
3.92 the operator-hours a week parked. The canonical instance is account-3 session
``8279631d``: an ``assistant`` record carrying a ``tool_use`` for
``ExitPlanMode`` at 2026-08-03T00:34:51Z, and the next record in the transcript
lands 111.2 MINUTES later. Nobody was notified.

WHY THIS IS NOT A STALENESS ALERT
---------------------------------
Staleness alone is useless here. In ``~/.claude-account-3`` alone, over three
days, there are 44 inter-record gaps longer than 15 minutes; 25 of them are
preceded by a ``system``/``turn_duration`` record. That record means the turn
ENDED — nothing was queued, the human went to bed. An alert keyed on "the file
has not changed in 15 minutes" fires on all 44 and is muted by Friday.

Only FOUR of the 44 are preceded by an ``assistant`` record carrying an
unanswered ``tool_use``. Those four are the entire signal.

THE PREDICATE (all four conditions required)
--------------------------------------------
(i)   TRANSCRIPT SHAPE. The LAST complete record of the transcript is
      ``type == "assistant"`` and its message carries a ``tool_use`` block.
      Because it is the last record, that ``tool_use`` is necessarily
      unanswered — a ``tool_result`` would be a later ``user`` record. This is
      the condition that kills 40 of the 44 gaps, and it is decided from ONE
      record, so the same transcript always yields the same answer.

(ii)  STALENESS. ``now - last_record_timestamp > T``. ``T`` is a config value in
      ``configs/audit-checks.yaml`` and is MEANT to be the p90 of answered
      ``assistant``/``tool_use`` -> next-record gaps. That distribution has not
      been computed, so T ships at 15 minutes carrying
      ``provenance: default-15min-unmeasured``. ``--gap-scan`` is the arm that
      produces the distribution when someone wants to replace the guess.

(iii) BACKGROUND EXCLUSION. The most recent ``system``/``turn_duration`` record
      reports ``pendingBackgroundAgentCount == 0`` (an absent field reads as 0 —
      the CLI omits it when there is nothing pending). A session waiting on its
      own ``run_in_background`` job is WORKING, not blocked. This is a declared
      FIELD, never an inference.

      ONE DOCUMENTED SUPERSESSION, and it is why the retrodiction passes. The
      turn_duration record is emitted at the END of a turn; a pending tool_use
      belongs to a turn still IN FLIGHT, so the count we can read always
      describes the PREVIOUS turn. For session 8279631d the previous turn ended
      with ``pendingBackgroundAgentCount: 2`` — a literal reading would have
      excluded the single most expensive block on the box. The resolution is not
      a fudge factor but a second declared field, the tool's own NAME: an
      unanswered ``ExitPlanMode`` or ``AskUserQuestion`` can only ever be
      answered by the operator. No amount of the session's own background work can
      unblock a question addressed to a human. Those tools are listed under
      ``human_input_tools`` in ``configs/audit-checks.yaml``; for every other
      tool the background exclusion applies literally.

(iv)  LIVE OWNER. Some process still owns the session. A session whose process
      is gone is FINISHED, not blocked, and alerting on it is noise. Probed in
      declared order — see :func:`probe_liveness`.

Conditions (i) and (iii) are a PURE FUNCTION of the transcript file
(:func:`transcript_verdict`); conditions (ii) and (iv) are environmental gates
applied on top. That split is deliberate: it is what makes ``--replay``
reproducible and what the acceptance suite pins.

COST
----
Enumeration reads the LAST 64 KB of each ``*.jsonl`` under ``~/.claude*/projects``
whose mtime is inside the window, and parses the final complete record. It NEVER
reads a transcript whole (the 8279631d transcript alone is 6.8 MB). The only
exception is ``--gap-scan``, a calibration/diagnostic arm that is not on the
launchd path and says so.

PAYLOAD DISCIPLINE (non-negotiable)
-----------------------------------
An alert carries EXACTLY ``{sessionId, account, cwd, gitBranch, tool_name,
minutes_blocked}``. These stores hold credentials and customer data; the banner
has to be safe to read on a phone on a train. No transcript content, no tool
input, no message text, ever. :func:`build_alert` is the only constructor and
:data:`ALERT_KEYS` is asserted by the acceptance suite.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_REGISTRY = REPO_ROOT / "configs" / "audit-checks.yaml"
ALERT_LOG = REPO_ROOT / "var" / "log" / "blocked-session-alerts.jsonl"

# The ONLY keys an alert may carry. Asserted by tests/acceptance/s23_blocked_detector.sh.
ALERT_KEYS = ("sessionId", "account", "cwd", "gitBranch", "tool_name", "minutes_blocked")

DEFAULT_THRESHOLD_MINUTES = 15.0
DEFAULT_THRESHOLD_PROVENANCE = "default-15min-unmeasured"
# Tools only a human can answer; see condition (iii) above.
DEFAULT_HUMAN_INPUT_TOOLS = ("ExitPlanMode", "AskUserQuestion")
# Tail read per transcript. One escalation to 1 MB if no complete record is found
# (a single record can be large); past that the file is reported undecidable.
TAIL_BYTES = 64 * 1024
TAIL_ESCALATED_BYTES = 1024 * 1024
DEFAULT_WINDOW_DAYS = 1
SUBPROCESS_TIMEOUT = 8.0

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


# --------------------------------------------------------------------------- config


def load_detector_config(registry: Path = AUDIT_REGISTRY) -> dict[str, Any]:
    """Read T and the human-input tool list from the audit registry.

    A missing/unparseable registry NEVER crashes the detector: it degrades to the
    documented defaults and says so in ``provenance``, because a detector that
    refuses to run because its config file is malformed is a detector that is not
    detecting.
    """
    cfg: dict[str, Any] = {
        "threshold_minutes": DEFAULT_THRESHOLD_MINUTES,
        "provenance": DEFAULT_THRESHOLD_PROVENANCE,
        "human_input_tools": list(DEFAULT_HUMAN_INPUT_TOOLS),
        "source": "built-in-default",
    }
    try:
        import yaml  # noqa: PLC0415 - optional at import time by design
    except Exception:  # noqa: BLE001
        cfg["source"] = "built-in-default (pyyaml unavailable)"
        return cfg
    try:
        raw = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
    except (OSError, Exception):  # noqa: BLE001
        cfg["source"] = f"built-in-default ({registry} unreadable)"
        return cfg
    node = ((raw.get("detectors") or {}).get("blocked_session") or {}) if isinstance(raw, dict) else {}
    if isinstance(node, dict):
        try:
            cfg["threshold_minutes"] = float(node.get("threshold_minutes", DEFAULT_THRESHOLD_MINUTES))
        except (TypeError, ValueError):
            pass
        if node.get("provenance"):
            cfg["provenance"] = str(node["provenance"])
        tools = node.get("human_input_tools")
        if isinstance(tools, list) and tools:
            cfg["human_input_tools"] = [str(t) for t in tools]
        cfg["source"] = str(registry)
    return cfg


# --------------------------------------------------------------------------- io


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _epoch(value: Any) -> float | None:
    parsed = _parse_iso(value)
    return parsed.timestamp() if parsed else None


def read_tail_records(path: Path, *, tail_bytes: int = TAIL_BYTES) -> tuple[list[dict], str]:
    """Parse every complete JSON record inside the last *tail_bytes* of *path*.

    Returns ``(records_in_file_order, note)``. Never reads the file whole (the
    single exception in this module is ``--gap-scan``). The first fragment is
    discarded whenever the read did not start at byte 0, because it is almost
    certainly half a record.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], f"stat failed: {type(exc).__name__}"
    if size == 0:
        return [], "empty file"
    start = max(0, size - tail_bytes)
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            blob = handle.read()
    except OSError as exc:
        return [], f"read failed: {type(exc).__name__}"
    lines = blob.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    records: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    note = "ok" if records else "no complete record in tail"
    return records, note


def read_tail_records_escalating(path: Path, *, tail_bytes: int = TAIL_BYTES) -> tuple[list[dict], str]:
    """:func:`read_tail_records` with ONE escalation to 1 MB, then give up."""
    records, note = read_tail_records(path, tail_bytes=tail_bytes)
    if records or tail_bytes >= TAIL_ESCALATED_BYTES:
        return records, note
    records, note = read_tail_records(path, tail_bytes=TAIL_ESCALATED_BYTES)
    return records, (note if not records else "ok (escalated tail)")


# --------------------------------------------------------------------------- predicate


@dataclass
class TranscriptVerdict:
    """Conditions (i) and (iii): a pure function of the transcript file."""

    blocked_shape: bool
    reason: str
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    last_ts: str | None = None
    last_epoch: float | None = None
    pending_background_agents: int = 0
    pbac_source: str = "absent-reads-zero"
    human_input_supersession: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_shape": self.blocked_shape,
            "reason": self.reason,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "gitBranch": self.git_branch,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "last_ts": self.last_ts,
            "pending_background_agents": self.pending_background_agents,
            "pbac_source": self.pbac_source,
            "human_input_supersession": self.human_input_supersession,
            "detail": self.detail,
        }


def _tool_uses(record: dict) -> list[dict]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _last_turn_duration(records: list[dict], *, before_index: int) -> dict | None:
    for record in reversed(records[:before_index]):
        if record.get("type") == "system" and record.get("subtype") == "turn_duration":
            return record
    return None


def transcript_verdict(
    records: list[dict],
    *,
    human_input_tools: tuple[str, ...] | list[str] = DEFAULT_HUMAN_INPUT_TOOLS,
) -> TranscriptVerdict:
    """Decide conditions (i) and (iii) over *records* (file order, last is last).

    PURE. No clock, no filesystem, no process table. Given the same records this
    returns the same verdict forever, which is what makes ``--replay`` a
    retrodiction rather than a re-run.
    """
    if not records:
        return TranscriptVerdict(False, "no complete record parsed")
    last_index = len(records) - 1
    last = records[last_index]
    last_type = last.get("type")
    if last_type != "assistant":
        return TranscriptVerdict(
            False,
            f"last-record-not-assistant:{last_type}",
            session_id=last.get("sessionId"),
            last_ts=last.get("timestamp"),
            last_epoch=_epoch(last.get("timestamp")),
        )
    uses = _tool_uses(last)
    if not uses:
        return TranscriptVerdict(
            False,
            "assistant-without-tool-use",
            session_id=last.get("sessionId"),
            last_ts=last.get("timestamp"),
            last_epoch=_epoch(last.get("timestamp")),
        )
    pending = uses[-1]
    tool_name = str(pending.get("name") or "")
    tool_use_id = str(pending.get("id") or "")

    turn = _last_turn_duration(records, before_index=last_index)
    if turn is None:
        pbac, pbac_source = 0, "no-turn_duration-in-tail-reads-zero"
    elif "pendingBackgroundAgentCount" not in turn:
        pbac, pbac_source = 0, "field-absent-reads-zero"
    else:
        try:
            pbac = int(turn.get("pendingBackgroundAgentCount") or 0)
        except (TypeError, ValueError):
            pbac = 0
        pbac_source = f"turn_duration@{turn.get('timestamp')}"

    supersedes = tool_name in set(human_input_tools)
    verdict = TranscriptVerdict(
        blocked_shape=True,
        reason="unanswered tool_use is the last record",
        session_id=last.get("sessionId"),
        cwd=last.get("cwd"),
        git_branch=last.get("gitBranch"),
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        last_ts=last.get("timestamp"),
        last_epoch=_epoch(last.get("timestamp")),
        pending_background_agents=pbac,
        pbac_source=pbac_source,
        human_input_supersession=supersedes,
    )
    if pbac > 0 and not supersedes:
        verdict.blocked_shape = False
        verdict.reason = f"background-work-pending:{pbac} (session is working, not blocked)"
    elif pbac > 0 and supersedes:
        verdict.reason = (
            f"unanswered {tool_name} is the last record; "
            f"human-input tool supersedes pendingBackgroundAgentCount={pbac}"
        )
    return verdict


# --------------------------------------------------------------------------- liveness


@dataclass
class ProcessWorld:
    """One snapshot of the process table, reused across every candidate."""

    procs: list[tuple[int, int, str]] = field(default_factory=list)
    claude_pids: list[int] = field(default_factory=list)
    cwd_by_pid: dict[int, str] = field(default_factory=dict)
    store_by_pid: dict[int, str] = field(default_factory=dict)
    error: str | None = None


_CLAUDE_CLI_MARKERS = ("/.local/bin/claude", "/claude.app/Contents/MacOS/claude", "/bin/claude")


def snapshot_processes() -> ProcessWorld:
    """One ``ps`` + one bounded ``lsof -d cwd``. Total measured cost ~70 ms."""
    world = ProcessWorld()
    try:
        proc = subprocess.run(
            ["/bin/ps", "-Ao", "pid,ppid,command"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        world.error = f"ps failed: {type(exc).__name__}"
        return world
    for line in proc.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        world.procs.append((pid, ppid, parts[2]))
    world.claude_pids = [
        pid for pid, _ppid, cmd in world.procs if any(marker in cmd for marker in _CLAUDE_CLI_MARKERS)
    ]
    if not world.claude_pids:
        return world

    # Attribute a CLI pid to an account store via any descendant that names the
    # store on its command line (the CLI's own argv never does).
    parent = {pid: ppid for pid, ppid, _cmd in world.procs}
    claude = set(world.claude_pids)
    for pid, _ppid, cmd in world.procs:
        match = re.search(r"(/Users/[^/\s]+/\.claude[A-Za-z0-9._-]*)/", cmd)
        if not match:
            continue
        store = match.group(1)
        walker, hops = pid, 0
        while walker and walker != 1 and hops < 32:
            if walker in claude:
                world.store_by_pid[walker] = store
                break
            walker = parent.get(walker, 0)
            hops += 1

    csv = ",".join(str(pid) for pid in world.claude_pids)
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", csv, "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return world
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
        elif line.startswith("n") and current is not None:
            world.cwd_by_pid[current] = line[1:]
    return world


def probe_liveness(
    world: ProcessWorld,
    *,
    session_id: str | None,
    cwd: str | None,
    store: Path,
) -> tuple[bool, str]:
    """Condition (iv), probed in DECLARED order, cheapest and most exact first.

    1. ``pgrep -f <sessionId>`` equivalent — any process whose command line names
       the session (its scratchpad path does). Exact when it fires, but a truly
       blocked session has no running child, so it usually does not.
    2. account-attributed CLI — a ``claude`` CLI process whose descendants name
       this account's store AND whose cwd matches the session's recorded cwd.
    3. cwd match against an unattributed CLI process.

    ``lsof`` on the transcript itself was tried first and REJECTED: measured on
    this box the CLI does not hold its own transcript open (verified against a
    live session), so it produces a false "dead" for every session.
    """
    if session_id:
        for _pid, _ppid, cmd in world.procs:
            if session_id in cmd:
                return True, "process-cmdline-names-session"
    if not cwd:
        return False, "no cwd on record; cannot attribute a process"
    store_str = str(store)
    for pid in world.claude_pids:
        if world.cwd_by_pid.get(pid) != cwd:
            continue
        attributed = world.store_by_pid.get(pid)
        if attributed and os.path.realpath(attributed) != os.path.realpath(store_str):
            continue
        return True, ("cli-cwd-match+account-attributed" if attributed else "cli-cwd-match")
    return False, "no live CLI process owns this cwd"


# --------------------------------------------------------------------------- alerts


def build_alert(
    *,
    session_id: str,
    account: str,
    cwd: str,
    git_branch: str,
    tool_name: str,
    minutes_blocked: float,
) -> dict[str, Any]:
    """The ONLY alert constructor. Six keys, all metadata, zero transcript body."""
    return {
        "sessionId": str(session_id or ""),
        "account": str(account or ""),
        "cwd": str(cwd or ""),
        "gitBranch": str(git_branch or ""),
        "tool_name": str(tool_name or ""),
        "minutes_blocked": round(float(minutes_blocked), 1),
    }


def _read_alert_log(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def should_alert(
    *, now_iso: str, last_notified_at: str | None, threshold: float
) -> tuple[bool, str]:
    """"One alert per (sessionId, tool_use uuid), re-alert only at 4xT."

    De-duplication is derived from the append-only log itself, never a side-car
    state file, so the log IS the state and the push-to-action interval stays
    computable from one file.

    The re-alert clock runs from the PREVIOUS alert, not from the block's start.
    Keying it to absolute ``minutes_blocked`` looks equivalent and is not: a
    fixture (or a genuinely ancient session) whose block is 300,000 minutes old
    would be "due" for its 5,000th tier on every single sweep and would re-alert
    forever, five minutes apart. Measured against the real transcripts before
    this was written.
    """
    if not last_notified_at:
        return True, "first sighting"
    now = _parse_iso(now_iso)
    last = _parse_iso(last_notified_at)
    if now is None or last is None:
        return False, "unparseable notified_at; refusing to re-alert"
    elapsed = (now - last).total_seconds() / 60.0
    window = 4.0 * max(threshold, 0.0)
    if elapsed >= window:
        return True, f"re-alert: {elapsed:.1f}m since the last alert >= 4xT ({window:.0f}m)"
    return False, f"deduped: {elapsed:.1f}m since the last alert < 4xT ({window:.0f}m)"


def append_alert_log(path: Path, *, session_id: str, tool_use_id: str, notified_at: str) -> None:
    """Append EXACTLY the three declared keys, so push-to-action is computable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"sessionId": session_id, "tool_use_id": tool_use_id, "notified_at": notified_at}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _push(alert: dict[str, Any]) -> str | None:
    """The EXISTING transport: omniagentos.sessions.notify.push. Never in tests."""
    try:
        from omniagentos.sessions.notify import push  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return f"import push failed: {type(exc).__name__}: {exc}"
    try:
        push(
            f"Session blocked {alert['minutes_blocked']:.0f}m",
            f"{alert['tool_name']} unanswered in {alert['cwd']}",
            subtitle=f"{alert['account']} · {alert['gitBranch']}",
            group=f"blocked-session:{alert['sessionId']}",
            kind="blocked",
        )
    except Exception as exc:  # noqa: BLE001 - delivery is presentation, never control flow
        return f"push failed: {type(exc).__name__}: {exc}"
    return None


# --------------------------------------------------------------------------- sweep


def default_stores() -> list[Path]:
    """Every ``~/.claude*`` directory that actually has a ``projects/`` tree."""
    home = Path.home()
    found = sorted(p for p in home.glob(".claude*") if (p / "projects").is_dir())
    return found


def account_name(store: Path) -> str:
    name = store.name
    return name[1:] if name.startswith(".") else name


def _transcripts(
    store: Path, *, window_days: int, now: float, include_subagents: bool = False
) -> list[tuple[Path, float]]:
    """Session transcripts in *store* modified inside the window.

    ``projects/<slug>/<sessionId>.jsonl`` only. Subagent transcripts live one
    level deeper at ``projects/<slug>/<sessionId>/subagents/agent-*.jsonl`` and
    are EXCLUDED by default on purpose: a subagent cannot block the operator. It is driven
    by its parent, and its last record being an unanswered tool_use means the
    parent is mid-turn, not that a human decision is outstanding. Alerting on
    them would triple the file count and add only noise. ``--include-subagents``
    is available for cost measurement.
    """
    root = store / "projects"
    if not root.is_dir():
        return []
    cutoff = now - window_days * 86400
    patterns = ["*/*.jsonl"] + (["*/*/**/*.jsonl"] if include_subagents else [])
    out: list[tuple[Path, float]] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                out.append((path, mtime))
    return out


def sweep(
    *,
    stores: list[Path],
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold_minutes: float = DEFAULT_THRESHOLD_MINUTES,
    human_input_tools: tuple[str, ...] | list[str] = DEFAULT_HUMAN_INPUT_TOOLS,
    liveness: str = "probe",
    now: float | None = None,
    tail_bytes: int = TAIL_BYTES,
    include_subagents: bool = False,
) -> dict[str, Any]:
    """One full pass. Returns a JSON-able report; mutates NOTHING."""
    started = time.monotonic()
    now = time.time() if now is None else now
    world = snapshot_processes() if liveness == "probe" else ProcessWorld()
    scanned = 0
    undecidable: list[str] = []
    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for store in stores:
        account = account_name(store)
        for path, mtime in _transcripts(
            store, window_days=window_days, now=now, include_subagents=include_subagents
        ):
            scanned += 1
            records, note = read_tail_records_escalating(path, tail_bytes=tail_bytes)
            if not records:
                undecidable.append(f"{account}:{path.name}: {note}")
                continue
            verdict = transcript_verdict(records, human_input_tools=human_input_tools)
            if not verdict.blocked_shape:
                continue
            last_epoch = verdict.last_epoch if verdict.last_epoch is not None else mtime
            minutes = max(0.0, (now - last_epoch) / 60.0)
            entry: dict[str, Any] = {
                "account": account,
                "store": str(store),
                "transcript": str(path),
                "minutes_blocked": round(minutes, 1),
                "verdict": verdict.as_dict(),
            }
            candidates.append(entry)
            if minutes <= threshold_minutes:
                entry["excluded_by"] = f"below-threshold ({minutes:.1f}m <= {threshold_minutes}m)"
                continue
            if liveness == "assume-dead":
                entry["excluded_by"] = "liveness=assume-dead"
                continue
            if liveness == "probe":
                live, why = probe_liveness(
                    world,
                    session_id=verdict.session_id,
                    cwd=verdict.cwd,
                    store=store,
                )
            else:
                live, why = True, "liveness=assume-live (declared by caller)"
            entry["liveness"] = why
            if not live:
                entry["excluded_by"] = f"no live owner ({why}) — session is FINISHED, not blocked"
                continue
            entry["alert"] = build_alert(
                session_id=verdict.session_id or "",
                account=account,
                cwd=verdict.cwd or "",
                git_branch=verdict.git_branch or "",
                tool_name=verdict.tool_name or "",
                minutes_blocked=minutes,
            )
            blocked.append(entry)

    return {
        "ts": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stores": [str(s) for s in stores],
        "window_days": window_days,
        "threshold_minutes": threshold_minutes,
        "liveness_mode": liveness,
        "scanned": scanned,
        "undecidable": undecidable,
        "candidates": candidates,
        "blocked": blocked,
        "process_snapshot_error": world.error,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def dispatch(
    report: dict[str, Any],
    *,
    alert_log: Path = ALERT_LOG,
    arm_push: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply de-duplication and record/push. Writes ONLY to *alert_log*."""
    threshold = float(report.get("threshold_minutes") or DEFAULT_THRESHOLD_MINUTES)
    existing = _read_alert_log(alert_log)
    last_seen: dict[tuple[str, str], str] = {}
    for row in existing:
        key = (str(row.get("sessionId") or ""), str(row.get("tool_use_id") or ""))
        stamp = str(row.get("notified_at") or "")
        if stamp and stamp > last_seen.get(key, ""):
            last_seen[key] = stamp
    emitted: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    push_errors: list[str] = []
    notified_at = report.get("ts") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    for entry in report.get("blocked", []):
        alert = entry.get("alert")
        if not alert:
            continue
        tool_use_id = str((entry.get("verdict") or {}).get("tool_use_id") or "")
        key = (alert["sessionId"], tool_use_id)
        fire, why = should_alert(
            now_iso=notified_at, last_notified_at=last_seen.get(key), threshold=threshold
        )
        if not fire:
            suppressed.append({"key": list(key), "reason": why})
            continue
        if dry_run:
            emitted.append({"alert": alert, "recorded": False, "pushed": False, "reason": "dry-run"})
            continue
        append_alert_log(alert_log, session_id=key[0], tool_use_id=key[1], notified_at=notified_at)
        last_seen[key] = notified_at
        pushed = False
        if arm_push:
            err = _push(alert)
            if err:
                push_errors.append(err)
            else:
                pushed = True
        emitted.append({"alert": alert, "recorded": True, "pushed": pushed})

    return {"emitted": emitted, "suppressed": suppressed, "push_errors": push_errors}


# --------------------------------------------------------------------------- replay


def replay(
    path: Path,
    *,
    at_tool: str | None = None,
    at_index: int | None = None,
    occurrence: int = -1,
    human_input_tools: tuple[str, ...] | list[str] = DEFAULT_HUMAN_INPUT_TOOLS,
    threshold_minutes: float = DEFAULT_THRESHOLD_MINUTES,
) -> dict[str, Any]:
    """RETRODICTION: truncate a historical transcript and re-decide the predicate.

    ``now`` is simulated as the timestamp of the record that actually followed the
    truncation point, so ``minutes_blocked`` is the block the human really ate.

    Condition (iv) cannot be observed for a process that exited days ago; replay
    therefore evaluates (i), (ii) and (iii) and reports
    ``liveness: not-observable-in-replay``. That is stated in the output rather
    than silently assumed.
    """
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except OSError as exc:
        return {"error": f"cannot read {path}: {type(exc).__name__}: {exc}"}

    if at_index is None:
        if not at_tool:
            at_index = len(records) - 1
        else:
            matches = [
                i
                for i, r in enumerate(records)
                if r.get("type") == "assistant"
                and any(b.get("name") == at_tool for b in _tool_uses(r))
            ]
            if not matches:
                return {"error": f"no assistant/tool_use record named {at_tool!r} in {path}"}
            at_index = matches[occurrence]
    if at_index < 0 or at_index >= len(records):
        return {"error": f"index {at_index} out of range (0..{len(records) - 1})"}

    prefix = records[: at_index + 1]
    verdict = transcript_verdict(prefix, human_input_tools=human_input_tools)
    next_epoch = None
    next_ts = None
    for record in records[at_index + 1 :]:
        candidate = _epoch(record.get("timestamp"))
        if candidate is not None:
            next_epoch, next_ts = candidate, record.get("timestamp")
            break
    minutes = None
    if verdict.last_epoch is not None and next_epoch is not None:
        minutes = round(max(0.0, (next_epoch - verdict.last_epoch) / 60.0), 1)

    result: dict[str, Any] = {
        "transcript": str(path),
        "records": len(records),
        "replay_at_index": at_index,
        "replay_at_tool": at_tool,
        "next_record_ts": next_ts,
        "minutes_blocked": minutes,
        "threshold_minutes": threshold_minutes,
        "liveness": "not-observable-in-replay",
        "verdict": verdict.as_dict(),
    }
    result["blocked"] = bool(
        verdict.blocked_shape and minutes is not None and minutes > threshold_minutes
    )
    if result["blocked"]:
        result["alert"] = build_alert(
            session_id=verdict.session_id or "",
            account=account_name(_store_of(path)),
            cwd=verdict.cwd or "",
            git_branch=verdict.git_branch or "",
            tool_name=verdict.tool_name or "",
            minutes_blocked=minutes or 0.0,
        )
    return result


def _store_of(path: Path) -> Path:
    """Walk up from a transcript to the ``~/.claude*`` store that contains it."""
    for parent in path.resolve().parents:
        if parent.name.startswith(".claude"):
            return parent
    return path.parent


# --------------------------------------------------------------------------- gap scan


def gap_scan(
    *,
    stores: list[Path],
    window_days: int,
    gap_minutes: float,
    human_input_tools: tuple[str, ...] | list[str] = DEFAULT_HUMAN_INPUT_TOOLS,
    now: float | None = None,
    include_subagents: bool = False,
) -> dict[str, Any]:
    """CALIBRATION ARM — the only place a transcript is read whole. NOT on the
    launchd path.

    For every inter-record gap longer than *gap_minutes*, replay the predicate at
    the record that precedes the gap. This is what produces (a) the
    false-positive evidence the acceptance suite asserts and (b) the p90
    distribution that is meant to replace T's ``default-15min-unmeasured``
    provenance.
    """
    now = time.time() if now is None else now
    gaps: list[dict[str, Any]] = []
    answered_tool_gaps: list[float] = []
    for store in stores:
        account = account_name(store)
        for path, _mtime in _transcripts(
            store, window_days=window_days, now=now, include_subagents=include_subagents
        ):
            records: list[dict] = []
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(obj, dict):
                            records.append(obj)
            except OSError:
                continue
            prev: tuple[int, float] | None = None
            for index, record in enumerate(records):
                epoch = _epoch(record.get("timestamp"))
                if epoch is None:
                    continue
                if prev is not None:
                    delta = (epoch - prev[1]) / 60.0
                    before = records[prev[0]]
                    if before.get("type") == "assistant" and _tool_uses(before):
                        answered_tool_gaps.append(delta)
                    if delta > gap_minutes:
                        verdict = transcript_verdict(
                            records[: prev[0] + 1], human_input_tools=human_input_tools
                        )
                        gaps.append(
                            {
                                "account": account,
                                "transcript": str(path),
                                "index": prev[0],
                                "gap_minutes": round(delta, 1),
                                "preceding_type": before.get("type"),
                                "preceding_subtype": before.get("subtype"),
                                "blocked_shape": verdict.blocked_shape,
                                "reason": verdict.reason,
                                "tool_name": verdict.tool_name,
                            }
                        )
                prev = (index, epoch)
    answered_tool_gaps.sort()

    def _pct(fraction: float) -> float | None:
        if not answered_tool_gaps:
            return None
        index = min(len(answered_tool_gaps) - 1, int(fraction * len(answered_tool_gaps)))
        return round(answered_tool_gaps[index], 3)

    p90 = _pct(0.90)
    return {
        "stores": [str(s) for s in stores],
        "window_days": window_days,
        "gap_minutes": gap_minutes,
        "total_gaps": len(gaps),
        "turn_duration_preceded": sum(1 for g in gaps if g["preceding_subtype"] == "turn_duration"),
        "assistant_tool_use_preceded": sum(
            1 for g in gaps if g["preceding_type"] == "assistant" and g["tool_name"]
        ),
        "flagged": [g for g in gaps if g["blocked_shape"]],
        "gaps": gaps,
        "assistant_tool_use_gap_p90_minutes": p90,
        "assistant_tool_use_gap_p99_minutes": _pct(0.99),
        "assistant_tool_use_gap_p999_minutes": _pct(0.999),
        "assistant_tool_use_gap_max_minutes": round(answered_tool_gaps[-1], 2) if answered_tool_gaps else None,
        "assistant_tool_use_gap_samples": len(answered_tool_gaps),
    }


# --------------------------------------------------------------------------- cli


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``--watch-blocked`` arm's own flags, attached to the sentinel's parser."""
    parser.add_argument(
        "--watch-blocked",
        action="store_true",
        help="run the blocked-session detector instead of the health checks",
    )
    parser.add_argument(
        "--store",
        action="append",
        default=None,
        metavar="DIR",
        help="a ~/.claude* store to sweep (repeatable; default: every store with projects/)",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"only consider transcripts modified inside this window (default {DEFAULT_WINDOW_DAYS})",
    )
    parser.add_argument("--threshold-minutes", type=float, default=None, help="override T")
    parser.add_argument("--replay", default=None, metavar="JSONL", help="retrodict one transcript")
    parser.add_argument(
        "--replay-at-record",
        default=None,
        metavar="ToolName",
        help="truncate the replay at an assistant/tool_use record for this tool",
    )
    parser.add_argument("--replay-at-index", type=int, default=None, help="truncate at a record index")
    parser.add_argument(
        "--replay-occurrence",
        type=int,
        default=-1,
        help="which match of --replay-at-record to use (default -1 = the most recent)",
    )
    parser.add_argument(
        "--liveness",
        choices=("probe", "assume-live", "assume-dead"),
        default="probe",
        help="condition (iv) mode; assume-live is for fixtures whose process is long gone",
    )
    parser.add_argument(
        "--gap-scan",
        action="store_true",
        help="calibration arm: read transcripts whole and report every gap (NOT the launchd path)",
    )
    parser.add_argument("--gap-minutes", type=float, default=15.0, help="--gap-scan gap floor")
    parser.add_argument("--alert-log", default=None, help="override var/log/blocked-session-alerts.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="decide everything, record nothing")
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="also sweep projects/*/<session>/subagents/*.jsonl (cost measurement; a subagent "
        "cannot block a human, so they are excluded by default)",
    )


def resolve_stores(values: list[str] | None) -> list[Path]:
    if not values:
        return default_stores()
    return [Path(os.path.expanduser(v)).resolve() for v in values]


def run_watch_blocked(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Entry point for ``--watch-blocked``. Returns ``(exit_code, report)``."""
    cfg = load_detector_config()
    threshold = (
        float(args.threshold_minutes)
        if getattr(args, "threshold_minutes", None) is not None
        else float(cfg["threshold_minutes"])
    )
    tools = cfg["human_input_tools"]

    if getattr(args, "replay", None):
        report = replay(
            Path(os.path.expanduser(args.replay)),
            at_tool=args.replay_at_record,
            at_index=args.replay_at_index,
            occurrence=args.replay_occurrence,
            human_input_tools=tools,
            threshold_minutes=threshold,
        )
        report["threshold_provenance"] = cfg["provenance"]
        return (2 if report.get("error") else 0), report

    stores = resolve_stores(getattr(args, "store", None))
    if getattr(args, "gap_scan", False):
        report = gap_scan(
            stores=stores,
            window_days=args.window_days,
            gap_minutes=args.gap_minutes,
            human_input_tools=tools,
            include_subagents=bool(getattr(args, "include_subagents", False)),
        )
        report["threshold_provenance"] = cfg["provenance"]
        return 0, report

    report = sweep(
        stores=stores,
        window_days=args.window_days,
        threshold_minutes=threshold,
        human_input_tools=tools,
        liveness=args.liveness,
        include_subagents=bool(getattr(args, "include_subagents", False)),
    )
    report["threshold_provenance"] = cfg["provenance"]
    report["config_source"] = cfg["source"]
    alert_log = Path(os.path.expanduser(args.alert_log)) if getattr(args, "alert_log", None) else ALERT_LOG
    arm_push = bool(getattr(args, "arm_push", False)) and not bool(getattr(args, "no_push", True))
    report["dispatch"] = dispatch(
        report,
        alert_log=alert_log,
        arm_push=arm_push,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    report["push_armed"] = arm_push
    return 0, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blocked-session detector (standalone entry point)")
    add_arguments(parser)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--no-push", action="store_true", default=True, help="never push (DEFAULT)")
    parser.add_argument(
        "--arm-push",
        action="store_true",
        help="CONSEQUENTIAL: actually deliver notifications (leaves the machine)",
    )
    args = parser.parse_args(argv)
    if args.arm_push:
        args.no_push = False
    code, report = run_watch_blocked(args)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
