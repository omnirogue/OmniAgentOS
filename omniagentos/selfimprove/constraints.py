"""append_constraint — grow a per-project CONSTRAINTS.md with a
verification-fix rule.

Companion to `omniagentos.selfimprove.skills.capture_skill`, same HARD RULE:
only appends a rule distilled from a PASSED verification gate — never a
hunch or an unverified fix, which would poison the constraints file that
future runs are meant to trust and load automatically (the same role this
repo's own `~/.claude/CLAUDE.md` plays for this session).

`CONSTRAINTS.md` is plain markdown (no vault frontmatter — it is not a vault
note, it lives under `omniagentos.selfimprove.paths.default_constraints_dir()`,
a versioned, non-gitignored directory, one file per project), append-only:
existing content is never rewritten, so a human editing the file by hand is
always safe.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import IO

from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.gate import gate_from_status_json
from omniagentos.selfimprove.models import ConstraintEntry, GateStatus, VerificationGate
from omniagentos.selfimprove.paths import (
    constraints_path,
    default_constraints_dir,
    ensure_safe_write_target,
    open_no_follow,
)

_HEADER_TEMPLATE = (
    "# CONSTRAINTS — {project}\n\n"
    "Rules distilled from verification-gate fixes (self-improving-loop "
    "method). Auto-grown by `omniagentos.selfimprove.append_constraint` — "
    "append-only, newest last. Human edits above this point are always safe; "
    "this file is never rewritten wholesale.\n"
)


def _lock_exclusively(handle: IO[str]) -> None:
    """Advisory cross-process exclusive lock (mirrors
    `omniagentos.ledger._lock_exclusively`); no-op on platforms without
    `fcntl` (e.g. Windows) — `ensure_safe_write_target` + the marker-based
    dedup check below still make a single-process retry idempotent there,
    just not race-free against a concurrent process."""
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: IO[str]) -> None:
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _constraint_marker(project: str, rule_text: str) -> str:
    """Stable dedup key for one (project, rule) pair, embedded as an HTML
    comment (invisible in rendered Markdown) at the end of each entry (F5)."""
    digest = hashlib.sha256(f"{project}\x1f{rule_text}".encode()).hexdigest()[:16]
    return f"constraint:{digest}"


def append_constraint(
    project: str,
    rule: str,
    gate: VerificationGate,
    *,
    constraints_dir: str | None = None,
    source_run_id: str | None = None,
) -> Path:
    """Append `rule` to `<constraints_dir>/<project-slug>/CONSTRAINTS.md`
    (default `constraints_dir`:
    `omniagentos.selfimprove.paths.default_constraints_dir()`). Creates the
    file (with a header) on first write for the project, appends a dated
    entry on every subsequent call.

    Raises `UnverifiedCaptureError` — and writes nothing — unless
    `gate.status == GateStatus.PASSED` (self-improving-loop HARD RULE,
    identical guard to `capture_skill`). Raises `ValueError` for a blank
    rule.

    Idempotent: calling this again with the same (`project`, `rule`) is a
    no-op that returns the existing path unchanged rather than appending a
    second duplicate entry (F5). Initialization (header creation) and the
    duplicate check + append are serialized under an exclusive file lock, so
    two callers racing to create the same project's file for the first time
    cannot truncate/overwrite each other's entry (F6).
    """
    # F1: same direct-status enforcement as capture_skill — see that
    # function's comment for why `.passed` is not trustworthy here.
    if gate.status is not GateStatus.PASSED:
        raise UnverifiedCaptureError(
            f"refusing to append constraint for project {project!r}: verification gate "
            f"status is {gate.status.value!r}, not 'passed' (self-improving-loop HARD "
            "RULE — only capture after a verified gate)"
        )
    rule_text = rule.strip()
    if not rule_text:
        raise ValueError("rule must be non-empty")

    entry = ConstraintEntry(
        project=project,
        rule=rule_text,
        source_run_id=source_run_id or gate.source_run_id,
    )
    root = Path(constraints_dir) if constraints_dir is not None else Path(default_constraints_dir())
    path = constraints_path(project, constraints_dir=constraints_dir)
    # F3: refuse before writing if a pre-existing symlink (the project
    # directory, or CONSTRAINTS.md itself) would redirect this write outside
    # constraints_dir.
    ensure_safe_write_target(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_safe_write_target(root, path)

    marker = _constraint_marker(project, rule_text)
    marker_comment = f"<!-- {marker} -->"

    # F3 (leaf TOCTOU) + F6 (concurrent first-write / partial-write):
    # open_no_follow refuses a pre-existing symlink at the leaf; the
    # exclusive flock below serializes header-creation and the
    # duplicate-check-then-append critical section across processes; the
    # entry text is built once and handed to a single write() + fsync (same
    # pattern as omniagentos.ledger.append_manifest) so an interrupted write
    # cannot land a half-written entry mixed with a half-written header.
    with open_no_follow(path, "a+", encoding="utf-8") as handle:
        _lock_exclusively(handle)
        try:
            handle.seek(0)
            existing = handle.read()
            if not existing:
                handle.write(_HEADER_TEMPLATE.format(project=project))
                handle.flush()
                os.fsync(handle.fileno())
            elif marker_comment in existing:
                # F5: same (project, rule) already recorded — idempotent no-op.
                return path

            handle.write(_render_entry(entry, gate, marker))
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            _unlock(handle)

    return path


def append_constraint_from_run_dir(
    run_dir: str,
    project: str,
    rule: str,
    *,
    constraints_dir: str | None = None,
) -> Path:
    """Convenience wrapper: reads the `VerificationGate` from
    `<run_dir>/status.json` (the shared Fusion worker status schema, see
    `omniagentos.selfimprove.gate`) and calls `append_constraint`."""
    gate = gate_from_status_json(run_dir)
    return append_constraint(project, rule, gate, constraints_dir=constraints_dir)


def _render_entry(entry: ConstraintEntry, gate: VerificationGate, marker: str) -> str:
    header = f"\n## {entry.created}"
    if entry.source_run_id:
        header += f" — {entry.source_run_id}"
    lines = [header, f"- {entry.rule}"]
    if gate.evidence:
        lines.append(f"- _gate evidence: {gate.evidence}_")
    lines.append(f"<!-- {marker} -->")
    return "\n".join(lines) + "\n"
