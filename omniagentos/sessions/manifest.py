"""Exactly-once terminal audit artifacts for interactive sessions."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from omniagentos.contracts import ApprovalState, default_ledger_dir


class SessionManifest:
    """Write one immutable JSONL record for each terminal session."""

    def __init__(self, ledger_dir: str | Path | None = None) -> None:
        root = Path(ledger_dir) if ledger_dir is not None else Path(default_ledger_dir())
        self.directory = root / "sessions"

    def path_for(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.jsonl"

    def write(
        self,
        session: dict[str, Any],
        approvals: Iterable[dict[str, Any]],
        *,
        killed_by: str | None = None,
    ) -> Path:
        session_id = str(session["id"])
        path = self.path_for(session_id)
        if path.exists():
            return path
        records = list(approvals)
        payload = {
            "session_id": session_id,
            "source": session["source"],
            "project_dir": session["project_dir"],
            "provider": session["provider"],
            "session_ref": session.get("session_ref"),
            "final_state": session["state"],
            "model": session.get("model"),
            "started_at": session["created_at"],
            "finished_at": session["updated_at"],
            # cost_usd is nullable (migration 088). Do not coerce NULL → 0.0:
            # a missing price is not a measured free run. Budget enforcement
            # still uses `or 0.0`; manifests must stay honest telemetry.
            "cost_usd": (
                None
                if session.get("cost_usd") is None
                else float(session["cost_usd"])
            ),
            # Effort/usage telemetry (migration 049). All nullable ON PURPOSE:
            # not every provider reports tokens, and a 0 here would be read as
            # "spent nothing" rather than "did not say". `usage_source` is what
            # separates a measurement from a gap — filter on it before
            # comparing effort levels, or the quietest provider wins.
            # Note wall_ms is PROCESS time, which is not the same as
            # finished_at - started_at (a session row can outlive its process);
            # both are kept so neither has to be inferred from the other.
            "effort": session.get("effort"),
            "input_tokens": session.get("input_tokens"),
            "output_tokens": session.get("output_tokens"),
            "wall_ms": session.get("wall_ms"),
            "usage_source": session.get("usage_source"),
            "approvals_requested": len(records),
            "approvals_granted": sum(
                row.get("state") == ApprovalState.APPROVED.value for row in records
            ),
            "approvals_denied": sum(
                row.get("state") in {ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value}
                for row in records
            ),
            "killed_by": killed_by,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        temporary = self.directory / f".{session_id}.{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as manifest_file:
                manifest_file.write(line)
                manifest_file.flush()
                os.fsync(manifest_file.fileno())
            try:
                # A hard link atomically creates the target without replacing an
                # existing record. SIGKILL can only strand an ignored temp file.
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return path


__all__ = ["SessionManifest"]
