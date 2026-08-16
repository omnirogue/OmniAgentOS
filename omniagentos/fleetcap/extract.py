"""Parse captured CLI transcripts and upsert attributed rows into fleet.sqlite."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from omniagentos.db.busy import execute_write_transaction
from omniagentos.fleetcap.attribution import attribute
from omniagentos.fleetcap.migrate import default_db_path, migrate
from omniagentos.fleetcap.profiles import Profile, existing_profiles
from omniagentos.fleetcap.schema import EXTRACT_COLUMNS

GAP_CAP = 4 * 3600.0
IDLE_CAP = 24 * 3600.0
BG_MARKERS = ("<task-notification>", "SYSTEM NOTIFICATION - NOT USER INPUT")
HUMAN_MARKERS = ("[Request interrupted by user",)
PARSABLE = frozenset({"claude", "codex", "kimi"})
# Sessions a dev uploaded themselves (omniagentos/team/transcript_uploader.py)
# out of an ai-transcripts clone the hub reads in place — not rsync-pulled, so
# they carry their own capture method and their own per-dev device label.
DEV_UPLOAD_CAPTURE = "dev-upload-v1"


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e12 else float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return ""


@dataclass
class Stats:
    cli: str
    account: str
    path: Path
    cwd: str | None = None
    agent: str | None = None
    models: set[str] = field(default_factory=set)
    first: float | None = None
    last: float | None = None
    n_user: int = 0
    n_assistant: int = 0
    n_tool: int = 0
    n_err: int = 0
    n_compact: int = 0
    n_sidechain: int = 0
    human_markers: int = 0
    chain: list[tuple[float, str]] = field(default_factory=list)
    open_tools: dict[str, tuple[float, str]] = field(default_factory=dict)
    tools: dict[str, list[float]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tokens: dict[str, float] = field(default_factory=dict)

    def see(self, timestamp: float | None, kind: str | None = None) -> None:
        if timestamp is None:
            return
        self.first = timestamp if self.first is None else min(self.first, timestamp)
        self.last = timestamp if self.last is None else max(self.last, timestamp)
        if kind:
            self.chain.append((timestamp, kind))

    def open_tool(self, tool_id: Any, timestamp: float | None, name: str) -> None:
        if tool_id is not None and timestamp is not None:
            self.open_tools[str(tool_id)] = (timestamp, name)

    def close_tool(
        self, tool_id: Any, timestamp: float | None, error: bool, detail: str = ""
    ) -> None:
        opened = self.open_tools.pop(str(tool_id), None)
        name = opened[1] if opened else "unknown"
        metric = self.tools.setdefault(name, [0.0, 0.0, 0.0, 0.0])
        metric[0] += 1
        if opened and timestamp is not None:
            duration = min(max(timestamp - opened[0], 0), GAP_CAP)
            metric[1] += duration
            metric[2] = max(metric[2], duration)
        if error:
            metric[3] += 1
            self.n_err += 1
            if detail:
                self.errors.append(detail[:160])

    def row(self) -> dict[str, Any]:
        model_s = tool_s = human_s = 0.0
        chain = sorted(self.chain)
        for previous, current in zip(chain, chain[1:], strict=False):
            gap = min(max(current[0] - previous[0], 0), IDLE_CAP)
            if current[1] == "user":
                human_s += gap
            elif current[1] == "tool_output":
                tool_s += min(gap, GAP_CAP)
            else:
                model_s += min(gap, GAP_CAP)
        wall = max((self.last or 0) - (self.first or 0), 0)
        return {
            "cli": self.cli,
            "account": self.account,
            "path": str(self.path),
            "cwd": self.cwd,
            "agent": self.agent,
            "models": sorted(self.models),
            "start": self.first,
            "end": self.last,
            "wall_s": round(wall, 1),
            "model_s": round(model_s, 1),
            "tool_s": round(tool_s, 1),
            "human_s": round(human_s, 1),
            "n_user": self.n_user,
            "n_assistant": self.n_assistant,
            "n_tool": self.n_tool,
            "n_err": self.n_err,
            "n_compact": self.n_compact,
            "n_sidechain": self.n_sidechain,
            "human_markers": self.human_markers,
            "tokens": self.tokens,
            "tools": self.tools,
            "errors": self.errors[:40],
        }


def parse_claude(stats: Stats, handle: TextIO) -> None:
    for line in handle:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            # A valid-JSON but non-object line (crafted upload, corruption) must
            # skip, never reach record.get() and abort the whole sweep.
            continue
        timestamp = _epoch(record.get("timestamp"))
        sidechain = bool(record.get("isSidechain"))
        stats.n_sidechain += int(sidechain)
        stats.cwd = stats.cwd or record.get("cwd")
        kind = record.get("type")
        raw_message = record.get("message")
        message = raw_message if isinstance(raw_message, dict) else {}
        content = message.get("content")
        if kind == "assistant":
            stats.n_assistant += 1
            if message.get("model"):
                stats.models.add(str(message["model"]))
            has_tool = False
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        has_tool = True
                        stats.n_tool += 1
                        stats.open_tool(
                            block.get("id"), timestamp, str(block.get("name") or "unknown")
                        )
            stats.see(timestamp, None if sidechain else ("tool_call" if has_tool else "model"))
        elif kind == "user":
            results = (
                [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                if isinstance(content, list)
                else []
            )
            if results:
                for result in results:
                    detail = _text(result.get("content"))
                    stats.close_tool(
                        result.get("tool_use_id"), timestamp, bool(result.get("is_error")), detail
                    )
                stats.see(timestamp, None if sidechain else "tool_output")
            elif not record.get("isMeta"):
                value = _text(content)
                stats.human_markers += sum(marker in value for marker in HUMAN_MARKERS)
                synthetic = any(marker in value for marker in BG_MARKERS) or value.startswith(
                    "<local-command-"
                )
                if not synthetic:
                    stats.n_user += 1
                stats.see(timestamp, None if sidechain else ("model" if synthetic else "user"))
        elif kind == "system" and record.get("level") == "error":
            stats.n_err += 1
            stats.errors.append(str(record.get("content") or "system error")[:160])
            stats.see(timestamp)
        else:
            stats.n_compact += int(record.get("subtype") == "compact_boundary")
            stats.see(timestamp)


def parse_codex(stats: Stats, handle: TextIO) -> None:
    for line in handle:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            # A valid-JSON but non-object line (crafted upload, corruption) must
            # skip, never reach record.get() and abort the whole sweep.
            continue
        timestamp = _epoch(record.get("timestamp"))
        raw_payload = record.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        kind = record.get("type")
        subtype = payload.get("type")
        if kind == "session_meta":
            stats.cwd = payload.get("cwd") or stats.cwd
            stats.agent = payload.get("agent_nickname") or stats.agent
        elif kind == "turn_context" and payload.get("model"):
            stats.models.add(str(payload["model"]))
        elif kind == "event_msg":
            if subtype == "user_message":
                stats.n_user += 1
                stats.see(timestamp, "user")
                continue
            if subtype == "agent_message":
                stats.n_assistant += 1
                stats.see(timestamp, "model")
                continue
            if subtype and "error" in str(subtype):
                stats.n_err += 1
                stats.errors.append(str(payload.get("message") or subtype)[:160])
        elif kind == "response_item":
            if subtype in {"function_call", "custom_tool_call", "local_shell_call"}:
                stats.n_tool += 1
                stats.open_tool(
                    payload.get("call_id"), timestamp, str(payload.get("name") or subtype)
                )
                stats.see(timestamp, "tool_call")
                continue
            if subtype in {"function_call_output", "custom_tool_call_output"}:
                output = str(payload.get("output") or "")
                is_error = bool(re.search(r"error|traceback|exit.code.?1", output[:400], re.I))
                stats.close_tool(payload.get("call_id"), timestamp, is_error, output)
                stats.see(timestamp, "tool_output")
                continue
        stats.n_compact += int(kind == "compacted")
        stats.see(timestamp)


def parse_kimi(stats: Stats, handle: TextIO) -> None:
    for line in handle:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            # A valid-JSON but non-object line (crafted upload, corruption) must
            # skip, never reach record.get() and abort the whole sweep.
            continue
        timestamp = _epoch(record.get("time") or record.get("created_at") or record.get("ts"))
        kind = str(record.get("type") or "")
        if kind == "turn.prompt":
            stats.n_user += 1
            stats.see(timestamp, "user")
        elif kind == "llm.request":
            if record.get("model"):
                stats.models.add(str(record["model"]))
            stats.see(timestamp, "model")
        elif kind == "usage.record":
            raw_usage = record.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else record
            for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                if isinstance(usage.get(key), (int, float)):
                    stats.tokens[key] = stats.tokens.get(key, 0) + float(usage[key])
            stats.see(timestamp)
        else:
            stats.n_compact += int(kind == "full_compaction.begin")
            stats.n_err += int("error" in kind)
            stats.cwd = stats.cwd or (record.get("cwd") if kind == "metadata" else None)
            stats.see(timestamp)


PARSERS = {"claude": parse_claude, "codex": parse_codex, "kimi": parse_kimi}


LOG = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 256 * 1024 * 1024  # a single transcript above this is skipped, not parsed


def parse_file(path: Path, cli: str, account: str) -> dict[str, Any] | None:
    stats = Stats(cli, account, path)
    if path.parent.name == "subagents" and path.stem.startswith("agent-"):
        stats.agent = path.stem
    try:
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            LOG.warning("fleetcap: skipping oversized transcript %s", path)
            return None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            PARSERS[cli](stats, handle)
    except OSError:
        return None
    except Exception as exc:  # noqa: BLE001 — a crafted upload must skip one file, never abort the sweep
        LOG.warning("fleetcap: skipping unparseable transcript %s (%s)", path, exc)
        return None
    return stats.row() if stats.first is not None else None


def _device_for(path: Path, ingest_root: Path) -> str:
    try:
        return path.resolve().relative_to(ingest_root.resolve()).parts[0]
    except (ValueError, IndexError):
        return "unknown"


def _config_identity(config: Path) -> tuple[dict[str, str], str]:
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, "mac-studio"
    hub = raw.get("hub") if isinstance(raw.get("hub"), dict) else {}
    hub_device = str(hub.get("device") or "mac-studio")
    owners = {
        str(item["device"]): str(item["owner"])
        for item in raw.get("devices", [])
        if isinstance(item, dict) and item.get("device") and item.get("owner")
    }
    if hub.get("owner"):
        owners[hub_device] = str(hub["owner"])
    return owners, hub_device


def _owners(config: Path) -> dict[str, str]:
    return _config_identity(config)[0]


def _hooks(spool: Path, ingest_root: Path | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    paths = list(spool.glob("hooks-*.jsonl")) if spool.is_dir() else []
    if ingest_root and ingest_root.is_dir():
        paths.extend(ingest_root.glob("*/spool/*/hooks-*.jsonl"))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            session_id = str(event.get("session_id") or "")
            if session_id:
                result.setdefault(session_id, {}).update(event)
    return result


def _session_id(
    device: str, cli: str, account: str, path: Path, *, hub_device: str = "mac-studio"
) -> str:
    stem = path.parent.name if path.name == "wire.jsonl" else path.stem
    if device == hub_device:
        return stem
    return f"native:{device}:{cli}:{account}:{stem}"


def _outcome(row: Mapping[str, Any]) -> str:
    if int(row.get("n_err") or 0) > 0 or not row.get("end"):
        return "failed?"
    if int(row.get("n_user") or 0) > 0:
        return "success?"
    return "unknown?"


def _estimate_profile(
    connection: sqlite3.Connection,
    profile: Profile,
    *,
    ingest_root: Path,
    owners: Mapping[str, str],
    cutoff: float,
    columns: set[str],
    hub_device: str,
    local_profile: bool,
) -> int:
    """Record one coarse freshness observation; never claim transcript metrics."""
    signals: list[Path] = []
    for pattern in profile.globs:
        for path in profile.root.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime >= cutoff:
                    signals.append(path)
            except OSError:
                continue
    if not signals:
        return 0
    latest = max(signals, key=lambda path: path.stat().st_mtime)
    observed = latest.stat().st_mtime
    device = hub_device if local_profile else _device_for(latest, ingest_root)
    _write_row(
        connection,
        {
            "session_id": (f"estimated-v2:{device}:{profile.cli}:{profile.account_label}"),
            "cli": profile.cli,
            "account": profile.account_label,
            "start_ts": observed,
            "end_ts": observed,
            "wall_s": 0,
            "active_s": 0,
            "outcome": "unknown?",
            "outcome_note": (
                "estimated observation only; semantic parser pending; "
                f"source_count={len(signals)}; latest_signal_mtime={observed:.6f}"
            ),
            "capture_method": "estimated-v2",
            "created_ts": time.time(),
            "device": device,
            "device_owner": owners.get(device),
            "dispatch_class": "unknown",
            "dispatcher": None,
            "dispatch_evidence": "estimator has no session-level dispatch evidence",
        },
        columns,
    )
    return 1


def _session_row(
    parsed: Mapping[str, Any],
    *,
    session_id: str,
    cli: str,
    account: str,
    device: str,
    owner: str | None,
    capture_method: str,
    hook: Mapping[str, Any],
) -> dict[str, Any]:
    """The sessions-table row for one parsed transcript, attribution included."""
    dispatch_class, dispatcher, evidence = attribute(
        dict(parsed) | {"device_owner": owner, "hook": hook}
    )
    tokens = parsed.get("tokens") or {}
    row: dict[str, Any] = {
        "session_id": session_id,
        "cli": cli,
        "account": account,
        "models": json.dumps(parsed.get("models") or []),
        "start_ts": parsed.get("start"),
        "end_ts": parsed.get("end"),
        "wall_s": parsed.get("wall_s"),
        "active_s": float(parsed.get("model_s") or 0) + float(parsed.get("tool_s") or 0),
        "model_s": parsed.get("model_s"),
        "tool_s": parsed.get("tool_s"),
        "human_s": parsed.get("human_s"),
        "n_user": parsed.get("n_user"),
        "n_assistant": parsed.get("n_assistant"),
        "n_tool": parsed.get("n_tool"),
        "n_err": parsed.get("n_err"),
        "n_compact": parsed.get("n_compact"),
        "tools": json.dumps(parsed.get("tools") or {}),
        "events": json.dumps(parsed.get("errors") or []),
        "outcome": _outcome(parsed),
        "capture_method": capture_method,
        "created_ts": time.time(),
        "device": device,
        "device_owner": owner,
        "dispatch_class": dispatch_class,
        "dispatcher": dispatcher,
        "dispatch_evidence": evidence,
    }
    cwd = parsed.get("cwd") or hook.get("cwd")
    if cwd is not None:
        row["cwd"] = cwd
    if parsed.get("agent") is not None:
        row["agent"] = parsed["agent"]
    if tokens:  # unmeasured tokens stay NULL rather than being written as 0
        row["tokens_in"] = tokens.get("input_tokens", tokens.get("total_tokens"))
        row["tokens_out"] = tokens.get("output_tokens")
        row["tokens_cached"] = tokens.get("cached_tokens")
    return row


def _write_row(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    columns: set[str] | None = None,
    *,
    hub_enrichment_only: bool = False,
) -> None:
    """Write one session row inside its own busy-retried write transaction.

    Busy-seam adoption (tests/db/test_busy_seam_adoption.py): each row is one
    BEGIN IMMEDIATE unit via ``execute_write_transaction`` — no module-level
    ``commit()`` — so a concurrent reader/writer on the telemetry DB gets the
    seam's retry policy instead of a raw ``database is locked`` error.
    """
    if columns is None:
        columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(sessions)")}
    unknown = set(row) - columns
    if unknown:
        raise RuntimeError(f"fleetcap schema missing extractor columns: {sorted(unknown)}")
    execute_write_transaction(
        connection,
        lambda conn: _write_row_body(conn, row, hub_enrichment_only=hub_enrichment_only),
        op="fleetcap_write_row",
    )


def _write_row_body(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    hub_enrichment_only: bool,
) -> None:
    existing = connection.execute(
        "SELECT outcome FROM sessions WHERE session_id = ?", (row["session_id"],)
    ).fetchone()
    if existing and hub_enrichment_only:
        legacy_metrics = {
            "models",
            "wall_s",
            "active_s",
            "human_s",
            "n_err",
            "n_compact",
            "rate_limit_max_pct",
            "outcome",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for name, value in row.items():
            if name == "session_id" or name in legacy_metrics:
                continue
            assignments.append(f"{name} = ?")
            params.append(value)
        params.append(row["session_id"])
        connection.execute(
            f"UPDATE sessions SET {', '.join(assignments)} WHERE session_id = ?", params
        )
        return
    if existing and existing[0] and not str(existing[0]).endswith("?"):
        row = row | {"outcome": existing[0]}
    values = row
    if existing:
        update_sql = ", ".join(f"{name} = ?" for name in values if name != "session_id")
        params = [values[name] for name in values if name != "session_id"] + [row["session_id"]]
        connection.execute(f"UPDATE sessions SET {update_sql} WHERE session_id = ?", params)
    else:
        names = list(values)
        placeholders = ", ".join("?" for _ in names)
        connection.execute(
            f"INSERT INTO sessions ({', '.join(names)}) VALUES ({placeholders})",
            [values[name] for name in names],
        )


def _iter_profiles(sources: list[Path] | None, ingest_root: Path) -> Iterable[tuple[Profile, bool]]:
    if not sources:
        yield from ((profile, True) for profile in existing_profiles())
        if ingest_root.is_dir():
            for device in ingest_root.iterdir():
                if not device.is_dir():
                    continue
                for cli_dir in device.iterdir():
                    if not cli_dir.is_dir():
                        continue
                    if cli_dir.name not in {*PARSABLE, "grok", "gemini", "spool"}:
                        print(f"fleetcap extract: skipping unknown cli directory {cli_dir}")
                        continue
                    if cli_dir.name == "spool":
                        continue
                    for account in cli_dir.iterdir():
                        if account.is_dir():
                            yield (
                                Profile(cli_dir.name, account.name, account, ("**/*.jsonl",)),
                                False,
                            )
        return
    for source in sources:
        for cli_name in ("claude", "codex", "kimi", "grok", "gemini"):
            cli_root = source / cli_name
            if cli_root.is_dir():
                accounts = [p for p in cli_root.iterdir() if p.is_dir()]
                for account in accounts or [cli_root]:
                    yield (
                        Profile(
                            cli_name,
                            account.name if accounts else "default",
                            account,
                            ("**/*.jsonl",),
                        ),
                        False,
                    )


def _local_clones(config: Path) -> list[tuple[Path, dict[str, str]]]:
    """(clone root, dev-short → employee id) for every ``mode: local`` device."""
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    devices = raw.get("devices") if isinstance(raw, dict) else None
    result: list[tuple[Path, dict[str, str]]] = []
    for item in devices if isinstance(devices, list) else []:
        if not isinstance(item, dict) or item.get("mode") != "local" or not item.get("path"):
            continue
        owner_map = item.get("owner_map")
        result.append(
            (
                Path(str(item["path"])).expanduser(),
                {str(key): str(value) for key, value in owner_map.items()}
                if isinstance(owner_map, dict)
                else {},
            )
        )
    return result


def _upload_labels(name: str) -> tuple[str, str] | None:
    """(hostname, cli) from an uploaded ``<host>__<cli>__<basename>`` filename."""
    parts = name.split("__", 2)
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _extract_dev_uploads(
    connection: sqlite3.Connection,
    *,
    config: Path,
    cutoff: float,
    columns: set[str],
) -> int:
    """Ingest ``transcripts/<dev>/<date>/<host>__<cli>__<file>`` from local clones.

    Ownership comes from the device's ``owner_map`` keyed on the ``<dev>``
    directory, so each dev's rows land with their own employee id and a
    ``dev-upload:<dev>`` device label. The uploading machine's hostname is the
    only per-machine identity these files carry, so it becomes ``account``.
    An absent or empty clone is a clean no-op: the clone is created during
    rollout and its absence must never fail the sweep for every other device.
    """
    count = 0
    for clone, owner_map in _local_clones(config):
        root = clone / "transcripts"
        try:
            dev_dirs = sorted(item for item in root.iterdir() if item.is_dir())
        except OSError:
            print(f"fleetcap extract: dev-upload clone not readable yet, skipped: {root}")
            continue
        skipped: dict[str, int] = {}
        for dev_dir in dev_dirs:
            dev = dev_dir.name
            device = f"dev-upload:{dev}"
            owner = owner_map.get(dev)
            if owner is None:
                print(f"fleetcap extract: {device} has no owner_map entry; rows stay unattributed")
            for path in sorted(dev_dir.glob("*/*")):
                try:
                    if not path.is_file() or path.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                labels = _upload_labels(path.name)
                if labels is None or labels[1] not in PARSABLE:
                    # sessions.cli carries a CHECK constraint, and grok/gemini
                    # have no semantic parser: an unrecognised label is counted
                    # and skipped rather than coerced into a CLI it is not.
                    key = labels[1] if labels else "unlabelled"
                    skipped[key] = skipped.get(key, 0) + 1
                    continue
                host, cli = labels
                parsed = parse_file(path, cli, host)
                if parsed is None:
                    continue
                # The full relative path (dev/date/filename), not just the stem:
                # two uploads with the same basename on different dates are
                # distinct sessions and must not overwrite one another.
                try:
                    rel = path.relative_to(clone).as_posix()
                except ValueError:
                    rel = f"{dev}/{path.name}"
                row = _session_row(
                    parsed,
                    session_id=f"dev-upload:{rel}",
                    cli=cli,
                    account=host,
                    device=device,
                    owner=owner,
                    capture_method=DEV_UPLOAD_CAPTURE,
                    hook={},
                )
                row["outcome_note"] = f"dev upload by {dev} from {host}; file={path.name}"
                _write_row(connection, row, columns)
                count += 1
        if skipped:
            breakdown = ", ".join(f"{name}={total}" for name, total in sorted(skipped.items()))
            print(
                f"fleetcap extract: dev-upload skipped {sum(skipped.values())} file(s) "
                f"with no parser for their cli label: {breakdown}"
            )
    return count


def extract(
    connection: sqlite3.Connection,
    *,
    sources: list[Path] | None,
    ingest_root: Path,
    spool: Path,
    config: Path,
    since_days: float = 7,
) -> int:
    migrate(connection)
    columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(sessions)")}
    if not set(EXTRACT_COLUMNS) <= columns:
        raise RuntimeError("fleetcap migration did not install the complete extractor schema")
    owners, hub_device = _config_identity(config)
    hooks = _hooks(spool, ingest_root)
    cutoff = time.time() - since_days * 86400
    count = 0
    try:
        profiles = list(_iter_profiles(sources, ingest_root))
    except OSError as exc:
        print(f"fleetcap extract: profile enumeration failed: {exc}")
        profiles = []
    for profile, local_profile in profiles:
        if profile.cli not in PARSABLE:
            count += _estimate_profile(
                connection,
                profile,
                ingest_root=ingest_root,
                owners=owners,
                cutoff=cutoff,
                columns=columns,
                hub_device=hub_device,
                local_profile=local_profile,
            )
            continue  # Grok/Gemini semantic parsers remain follow-up work.
        for pattern in profile.globs:
            for path in profile.root.glob(pattern):
                try:
                    if not path.is_file() or path.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                parsed = parse_file(path, profile.cli, profile.account_label)
                if parsed is None:
                    continue
                device = hub_device if local_profile else _device_for(path, ingest_root)
                stem = path.parent.name if path.name == "wire.jsonl" else path.stem
                hook = hooks.get(stem, {})
                db_row = _session_row(
                    parsed,
                    session_id=_session_id(
                        device,
                        profile.cli,
                        profile.account_label,
                        path,
                        hub_device=hub_device,
                    ),
                    cli=profile.cli,
                    account=profile.account_label,
                    device=device,
                    owner=owners.get(device),
                    capture_method="fleetcap-v2",
                    hook=hook,
                )
                _write_row(
                    connection,
                    db_row,
                    columns,
                    hub_enrichment_only=device == hub_device,
                )
                count += 1
    count += _extract_dev_uploads(connection, config=config, cutoff=cutoff, columns=columns)
    # Every row already committed via its own execute_write_transaction unit —
    # no batch commit remains (busy-seam adoption).
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--sources", type=Path, nargs="+")
    parser.add_argument(
        "--ingest-root", type=Path, default=Path("~/.omniagentos/ops/telemetry/ingest").expanduser()
    )
    parser.add_argument(
        "--spool", type=Path, default=Path("~/.omniagentos/ops/telemetry/spool").expanduser()
    )
    parser.add_argument("--config", type=Path, default=Path("configs/fleetcap/devices.yaml"))
    parser.add_argument("--since-days", type=float, default=7)
    args = parser.parse_args(argv)
    args.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        count = extract(
            connection,
            sources=args.sources,
            ingest_root=args.ingest_root,
            spool=args.spool,
            config=args.config,
            since_days=args.since_days,
        )
    print(f"fleetcap extract: {count} transcript(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
