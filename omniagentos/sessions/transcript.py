"""Live CLI transcript resolution for claude-bridge sessions.

The supervisor writes NO per-session stream artifact; the live activity
transcript of a claude-bridge session is the CLI's own stream-json JSONL at

    <account config_dir>/projects/<slug(project_dir)>/<session_ref>.jsonl

* ``slug`` is the CLI's project-dir munging (every non-alphanumeric character
  becomes ``-``) — see :func:`project_dir_slug`.
* ``config_dir`` is the ``claude_accounts.config_dir`` of the session's
  account (``~/.claude`` when the session has no account or the account
  carries no config_dir).

The ``ledger/sessions/<id>.jsonl`` manifest is the TERMINAL AUDIT RECORD (one
metadata line written by :class:`omniagentos.sessions.manifest.SessionManifest`
at termination) — it is NOT the live transcript. Readers that want assistant
activity (chat turn bridge, transcript API routes, supervisor output harvest)
must resolve the path through here; there is ONE resolution rule and it lives
in this module.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omniagentos.sessions.transcript_delta import assistant_text, result_text

#: Fallback CLAUDE_CONFIG_DIR when the session has no account-backed one.
DEFAULT_CONFIG_DIR = "~/.claude"


def project_dir_slug(project_dir: str) -> str:
    """The CLI's project-dir munging: every non-alphanumeric char becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", project_dir)


def is_provider_session(session: Mapping[str, Any]) -> bool:
    """True for non-claude provider-exec sessions (they have no JSONL file)."""
    provider = str(session.get("provider") or "").strip().lower()
    return bool(provider) and provider != "claude"


def _default_account_lookup(account_id: str) -> Mapping[str, Any] | None:
    """Resolve a claude_accounts row on the default DB (best-effort)."""
    try:
        from omniagentos.accounts.service import get_account

        return get_account(account_id)
    except Exception:  # noqa: BLE001 — resolution must never break the caller
        return None


def config_dir_candidates(
    session: Mapping[str, Any],
    *,
    account_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    extra_config_dirs: tuple[str | None, ...] = (),
) -> list[str]:
    """CLAUDE_CONFIG_DIR candidates for ``session``, precedence order, deduped.

    Order: ``extra_config_dirs`` (caller-known launch facts, e.g. the live
    process handle's account_config_dir) → the session's claude_accounts row
    (via ``account_lookup``; default: accounts service on the default DB) →
    ``~/.claude``. Every candidate is ``~``-expanded.
    """
    dirs: list[str] = [d for d in extra_config_dirs if isinstance(d, str) and d.strip()]
    account_id = session.get("account_id")
    if isinstance(account_id, str) and account_id:
        lookup = account_lookup or _default_account_lookup
        try:
            row = lookup(account_id)
        except Exception:  # noqa: BLE001 — a lookup fault degrades to the default
            row = None
        cfg = row.get("config_dir") if isinstance(row, Mapping) else None
        if isinstance(cfg, str) and cfg.strip():
            dirs.append(cfg)
    dirs.append(DEFAULT_CONFIG_DIR)
    seen: set[str] = set()
    out: list[str] = []
    for candidate in dirs:
        expanded = os.path.expanduser(candidate)
        if expanded not in seen:
            seen.add(expanded)
            out.append(expanded)
    return out


def live_transcript_path(
    session: Mapping[str, Any] | None,
    *,
    account_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    config_dir: str | None = None,
) -> Path | None:
    """Resolve the LIVE CLI transcript for a claude-bridge session, or None.

    None for provider (non-claude) sessions — the caller then yields nothing —
    and for sessions missing ``session_ref``/``project_dir``. The returned
    path need not exist yet (a starting session's file appears late); readers
    tolerate absence. ``config_dir`` pins the directory explicitly (tests,
    live launch facts); otherwise the account row / ``~/.claude`` chain
    resolves it.
    """
    if session is None or is_provider_session(session):
        return None
    session_ref = session.get("session_ref")
    project_dir = session.get("project_dir")
    if not isinstance(session_ref, str) or not session_ref:
        return None
    if not isinstance(project_dir, str) or not project_dir:
        return None
    extra = (config_dir,) if config_dir else ()
    dirs = config_dir_candidates(
        session, account_lookup=account_lookup, extra_config_dirs=extra
    )
    return (
        Path(dirs[0])
        / "projects"
        / project_dir_slug(project_dir)
        / f"{session_ref}.jsonl"
    )


def final_reply_text(
    session: Mapping[str, Any] | None,
    *,
    account_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    config_dir: str | None = None,
) -> str | None:
    """Best-effort harvest of the session's final reply from the live transcript.

    Last ``{"type": "result"}`` result text wins; otherwise the last assistant
    text block. None when the transcript cannot be resolved or read, or when
    it carries no reply text.
    """
    path = live_transcript_path(
        session, account_lookup=account_lookup, config_dir=config_dir
    )
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    final: str | None = None
    assistant: str | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        result = result_text(entry)
        if result:
            final = result
        text = assistant_text(entry)
        if text:
            assistant = text
    return final or assistant


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "config_dir_candidates",
    "final_reply_text",
    "is_provider_session",
    "live_transcript_path",
    "project_dir_slug",
]
