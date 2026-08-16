"""Build a `VerificationGate` from a Fusion session's `status.json`.

Fusion workers in this repo (see the shared worker brief) write a
`status.json` into their session dir shaped roughly like:

    {"sessionId": ..., "package": ..., "state": "done"|"partial"|"failed",
     "commits": [{"hash": ..., "message": ...}, ...], "validation": {...}}

`state == "done"` is this repo's existing convention for "the package's
verification (tests/lint/build) actually passed" — see e.g.
`.fusion/runs/*/sessions/*/status.json` in real runs. This module reads that
file and maps it onto the package-local `VerificationGate` model so
`capture_skill` / `append_constraint` can enforce the HARD RULE without
hard-coding the status.json shape into their own logic (callers with a
differently-shaped verification artifact build a `VerificationGate` directly
instead of going through this adapter).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omniagentos.selfimprove.models import GateStatus, VerificationGate

STATUS_FILENAME = "status.json"

# "partial"/"failed" are deliberately both mapped to FAILED (not PENDING):
# the self-improving-loop HARD RULE only trusts a *complete* pass. An
# unrecognized/missing state maps to PENDING rather than FAILED so a caller
# inspecting the gate can tell "not yet verified" apart from "verification
# ran and found a problem".
_STATE_TO_STATUS: dict[str, GateStatus] = {
    "done": GateStatus.PASSED,
    "partial": GateStatus.FAILED,
    "failed": GateStatus.FAILED,
    "running": GateStatus.PENDING,
}


def gate_from_status(data: dict[str, Any]) -> VerificationGate:
    """Build a `VerificationGate` from an already-parsed status.json dict.

    An explicit `"validated": false` overrides a `state: "done"` verdict
    (some worker schemas set `validated` independently of `state` — see the
    shared worker brief: "set validated=true ONLY if you actually ran the
    validation and it passed"). A missing/unknown `state` maps to PENDING.

    When present, `validated` MUST be an actual JSON boolean. A malformed
    status artifact (e.g. `validated: 0` or `validated: "false"` — either a
    tampered file or a buggy writer) is never trusted for a PASSED gate: this
    raises `ValueError` rather than silently failing open, because `0 is
    False` and `"false" is False` are both `False` in Python, and a naive
    `data.get("validated") is False` check would have let those two forms
    slip through and still map `state: "done"` to PASSED.
    """
    state = str(data.get("state", "")).strip().lower()
    status = _STATE_TO_STATUS.get(state, GateStatus.PENDING)
    if "validated" in data:
        validated = data["validated"]
        if not isinstance(validated, bool):
            raise ValueError(
                "status.json 'validated' must be a JSON boolean, got "
                f"{type(validated).__name__} ({validated!r}) — a malformed 'validated' value "
                "is never trusted for a PASSED verification gate"
            )
        if status is GateStatus.PASSED and validated is False:
            status = GateStatus.FAILED

    source_run_id = data.get("package") or data.get("sessionId") or data.get("session_id")

    evidence_bits = [f"state={state or 'unknown'}"]
    commits = data.get("commits")
    if isinstance(commits, list) and commits:
        evidence_bits.append(f"{len(commits)} commit(s)")
    validation = data.get("validation")
    if isinstance(validation, dict) and validation:
        preview = "; ".join(f"{k}={v}" for k, v in list(validation.items())[:3])
        evidence_bits.append(preview)

    return VerificationGate(
        status=status,
        source_run_id=str(source_run_id) if source_run_id else None,
        evidence="; ".join(evidence_bits),
    )


def gate_from_status_json(run_dir: str | Path) -> VerificationGate:
    """Read `<run_dir>/status.json` and build a `VerificationGate` from it.

    Raises `FileNotFoundError` when the file is missing — capture must never
    silently assume a pass just because there is no verification artifact at
    all.
    """
    path = Path(run_dir) / STATUS_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"no {STATUS_FILENAME} found in run_dir {str(run_dir)!r}; cannot build a "
            "VerificationGate without verification evidence"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} did not parse to a JSON object")
    return gate_from_status(raw)
