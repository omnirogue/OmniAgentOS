"""Correlate GitHub repo/PR/commit/check identifiers back to originating sessions.

Precedence (first match wins; never guess):

1. Explicit embedded marker in text/ref: ``ses_<id>`` or ``run_<id>`` / ``swr_<id>``
2. Branch name containing a session id (``ses_…``) or swarm run id
3. PR head ref matching a known session/worktree branch pattern
4. Commit message trailer ``Omni-Session: ses_…``
5. Otherwise: ``unattributed``

Gated by ``OMNIAGENTOS_GITHUB_COMMS_MODE`` at the call site (this module is pure).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "Attribution",
    "CorrelationResult",
    "attribute_github_event",
    "extract_markers",
]

Attribution = Literal["session", "run", "swarm_run", "unattributed"]

_SES_RE = re.compile(r"\b(ses_[a-f0-9]{8,})\b", re.IGNORECASE)
_RUN_RE = re.compile(r"\b(run_[a-f0-9]{8,})\b", re.IGNORECASE)
_SWR_RE = re.compile(r"\b(swr_[a-f0-9]{8,})\b", re.IGNORECASE)
_TRAILER_RE = re.compile(r"(?im)^\s*Omni-Session:\s*(ses_[a-f0-9]{8,})\s*$")


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    attribution: Attribution
    session_id: str | None = None
    run_id: str | None = None
    swarm_run_id: str | None = None
    matched_via: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attribution": self.attribution,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "swarm_run_id": self.swarm_run_id,
            "matched_via": self.matched_via,
        }


def extract_markers(text: str) -> dict[str, str | None]:
    session = _SES_RE.search(text or "")
    run = _RUN_RE.search(text or "")
    swr = _SWR_RE.search(text or "")
    trailer = _TRAILER_RE.search(text or "")
    return {
        "session_id": (trailer.group(1) if trailer else (session.group(1) if session else None)),
        "run_id": run.group(1) if run else None,
        "swarm_run_id": swr.group(1) if swr else None,
    }


def attribute_github_event(
    payload: Mapping[str, Any],
    *,
    known_sessions: Mapping[str, Any] | None = None,
) -> CorrelationResult:
    """Resolve originating session/run from a GitHub webhook-shaped payload.

    ``known_sessions`` is an optional ``{session_id: meta}`` map used only to
    confirm a candidate exists; absence still allows returning the marker
    (callers may soft-bind). When no marker is found, result is unattributed.
    """
    texts: list[str] = []
    for key in ("body", "title", "message", "head_ref", "ref", "branch"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            texts.append(val)

    # Nested GitHub shapes
    pr = payload.get("pull_request")
    if isinstance(pr, Mapping):
        head = pr.get("head")
        if isinstance(head, Mapping):
            for k in ("ref", "label"):
                if isinstance(head.get(k), str):
                    texts.append(str(head[k]))
        if isinstance(pr.get("body"), str):
            texts.append(str(pr["body"]))
        if isinstance(pr.get("title"), str):
            texts.append(str(pr["title"]))

    comment = payload.get("comment")
    if isinstance(comment, Mapping) and isinstance(comment.get("body"), str):
        texts.append(str(comment["body"]))

    check = payload.get("check_run") or payload.get("workflow_run")
    if isinstance(check, Mapping):
        for k in ("name", "head_branch", "head_sha"):
            if isinstance(check.get(k), str):
                texts.append(str(check[k]))
        head_commit = check.get("head_commit")
        if isinstance(head_commit, Mapping) and isinstance(head_commit.get("message"), str):
            texts.append(str(head_commit["message"]))

    joined = "\n".join(texts)
    markers = extract_markers(joined)

    if markers["session_id"]:
        sid = markers["session_id"]
        if known_sessions is not None and sid not in known_sessions:
            # Marker present but not in known set — still report the marker;
            # callers decide whether to deliver. Never invent a different session.
            pass
        return CorrelationResult(
            attribution="session",
            session_id=sid,
            run_id=markers["run_id"],
            swarm_run_id=markers["swarm_run_id"],
            matched_via="marker",
        )
    if markers["swarm_run_id"]:
        return CorrelationResult(
            attribution="swarm_run",
            swarm_run_id=markers["swarm_run_id"],
            run_id=markers["run_id"],
            matched_via="swarm_marker",
        )
    if markers["run_id"]:
        return CorrelationResult(
            attribution="run",
            run_id=markers["run_id"],
            matched_via="run_marker",
        )
    return CorrelationResult(attribution="unattributed", matched_via=None)
