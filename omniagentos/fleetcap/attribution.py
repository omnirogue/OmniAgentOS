"""Deterministic dispatcher attribution for a parsed fleet session."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

Dispatch = tuple[str, str | None, str]
_DAEMON_HINTS = (
    (re.compile(r"(?:^|/)var/swarm/([^/]+)"), "swarm"),
    (re.compile(r"(?:^|/)var/runtime/([^/]+)"), "runtime"),
    (re.compile(r"(?:^|/)(?:bld-[^/]+|[^/]+-wt)(?:/|$)"), "worktree"),
    (re.compile(r"(?:^|/)(?:loop|loops|worktrees?)(?:/|[-_])([^/]+)"), "loop"),
    (re.compile(r"launchd|LaunchAgents|LaunchDaemons", re.I), "launchd"),
)


def _parent_session(path: str) -> str | None:
    candidate = Path(path)
    if candidate.parent.name == "subagents" and candidate.name.startswith("agent-"):
        parent = candidate.parent.parent
        if parent.suffix == ".jsonl":
            return parent.stem
        if parent.name:
            return parent.name
    match = re.search(r"/([^/]+)\.jsonl/subagents/agent-[^/]+\.jsonl$", path)
    return match.group(1) if match else None


def attribute(facts: Mapping[str, Any]) -> Dispatch:
    """Classify a session using strongest evidence first.

    Sidechain location is authoritative, followed by hook source/cwd daemon
    evidence, then real-human-message evidence tied to the registered owner.
    """
    path = str(facts.get("path") or "")
    parent = _parent_session(path)
    sidechains = int(facts.get("n_sidechain") or facts.get("is_sidechain_count") or 0)
    if parent or (sidechains and "/subagents/" in path):
        dispatcher = f"subagent:{parent}" if parent else "subagent:unknown"
        return "subagent", dispatcher, f"sidechain transcript; parent={parent or 'unknown'}"

    raw_hook = facts.get("hook")
    hook: Mapping[str, Any] = raw_hook if isinstance(raw_hook, Mapping) else {}
    cwd = str(hook.get("cwd") or facts.get("cwd") or "")
    source = str(hook.get("source") or "")
    daemon_label = str(facts.get("daemon_label") or "")
    if not daemon_label:
        for pattern, prefix in _DAEMON_HINTS:
            match = pattern.search(cwd)
            if match:
                suffix = match.group(1) if match.lastindex else Path(cwd).name
                daemon_label = f"{prefix}-{suffix}".strip("-")
                break
    tty = str(hook.get("tty") or "").strip()
    has_hook = bool(hook)
    interactive_value = hook.get("interactive")
    interactive = interactive_value is True or (
        interactive_value is None and tty not in {"", "?", "??", "-"}
    )
    headless = interactive_value is False or (
        interactive_value is None and has_hook and tty in {"", "?", "??", "-"}
    )
    human_markers = int(facts.get("human_markers") or 0)
    n_user = int(facts.get("n_user") or 0)
    owner = facts.get("device_owner") or facts.get("owner")
    if interactive and owner:
        return "human", str(owner), f"interactive hook evidence: tty={tty!r}"
    if headless:
        label = daemon_label or source.lower()
        label = label or "headless"
        return (
            "daemon",
            f"daemon:{label}",
            (f"headless hook/cwd heuristic: tty={tty!r}, cwd={cwd!r}, source={source!r}"),
        )
    if daemon_label:
        return "daemon", f"daemon:{daemon_label}", f"hook/cwd daemon heuristic: cwd={cwd!r}"
    if owner and (n_user > 0 or human_markers > 0):
        basis = "n_user>0" if n_user > 0 else "human_markers>0"
        return (
            "human",
            str(owner),
            f"{basis}, no hook evidence; {n_user} user messages, "
            f"{human_markers} human markers; device owner",
        )
    return "unknown", None, "no decisive sidechain, daemon, or owned-human signal"
