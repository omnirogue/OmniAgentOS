"""Gate execution engine — executing mechanical / verification gates."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.adapters.common import _scrubbed_env

__all__ = [
    "EvidenceVerdict",
    "GateResult",
    "GateSpec",
    "blocking_failures",
    "evaluate_evidence",
    "run_gates",
]


@dataclass(frozen=True, slots=True)
class GateSpec:
    """Mechanical gate specification.

    ``argv`` is the only execution form and is always run with ``shell=False``.
    The deprecated ``command`` field remains solely to produce a clear
    fail-closed error for stale callers; no shell-string execution rail exists.
    A bare str/bytes scalar is refused because those types are Sequences too.
    """

    command: str | None = None
    cwd: str | None = None
    timeout_s: int = 600
    expected_exit: int = 0
    blocking: bool = True
    name: str = ""
    argv: Sequence[str] | None = None

    def __post_init__(self) -> None:
        if self.command is not None:
            raise ValueError("GateSpec.command shell execution is disabled; provide argv")
        raw = self.argv
        # Critical: str/bytes are Sequences — refuse them so a scalar cannot
        # select the safe-argv branch by accident.
        if isinstance(raw, (str, bytes)):
            raise TypeError("GateSpec.argv must be a non-string sequence of str, not a scalar")
        if not isinstance(raw, Sequence):
            raise TypeError("GateSpec.argv must be a non-string sequence of str")
        if len(raw) == 0:
            raise ValueError("GateSpec.argv must be a non-empty sequence")
        items: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise TypeError(f"GateSpec.argv items must be str, got {type(item).__name__}")
            items.append(item)
        object.__setattr__(self, "argv", tuple(items))

    def display_command(self) -> str:
        """Human/evidence command text without shell interpretation."""
        assert self.argv is not None  # frozen post-init invariant
        return shlex.join(self.argv)


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    command: str
    ok: bool
    exit_code: int | None
    output: str
    duration_ms: float
    blocking: bool
    infra_error: str | None = None
    output_artifact_path: str | None = None
    output_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceVerdict:
    ok: bool
    blocking: bool
    reason: str
    missing: tuple[str, ...]


def _parse_pytest_counts(output: str) -> str | None:
    """Cheap regex parse of pytest pass/fail/skip/warning counts out of stdout."""
    summary_pattern = re.compile(r"==+ (.*) in \d+(?:\.\d+)?s ==+")
    match = None
    lines = output.splitlines()
    for line in reversed(lines):
        m = summary_pattern.search(line)
        if m:
            match = m.group(1)
            break
    if not match:
        return None

    counts: dict[str, int] = {}
    for part in match.split(","):
        part = part.strip()
        part_match = re.match(r"(\d+)\s+(passed|failed|skipped|error|warning)s?", part)
        if part_match:
            count = int(part_match.group(1))
            key = part_match.group(2)
            counts[key] = count

    if counts:
        return json.dumps(counts)
    return None


def run_gates(
    specs: Sequence[GateSpec],
    workdir: str,
    *,
    conn: sqlite3.Connection | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    attempt_ref: str | None = None,
    artifact_root: str | Path | None = None,
    commit_sha: str | None = None,
) -> list[GateResult]:
    """Run all specified gates in sequence and return their individual results.

    Note: Unlike default_verifier, run_gates executes all specs in sequence
    and does not short-circuit. Short-circuiting logic should be handled by
    the caller.
    """
    results: list[GateResult] = []
    for spec in specs:
        t0 = time.perf_counter()
        exit_code = None
        output = ""
        ok = False
        infra_error = None

        display_cmd = spec.display_command()
        try:
            cwd = spec.cwd if spec.cwd is not None else workdir
            assert spec.argv is not None  # frozen post-init invariant
            proc = subprocess.run(  # noqa: S603
                list(spec.argv),
                shell=False,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=spec.timeout_s,
                # H3(b): a gate's argv is PLANNER-AUTHORED (swarm ``verify_command``
                # / the detected mechanical suite), so this is an untrusted command
                # running on the operator's machine. It inherited the full parent
                # environment — every payment/bank/infra credential, provider API
                # key, admin DSN and OPERATOR_TOKEN — while the two other spawn
                # sites for planner-influenced processes (adapters/common.py and
                # harnesses/fusion.py) had already been scrubbed. Same allowlist
                # helper, not a second one: one definition of "safe env var" is the
                # only way the layers cannot drift apart.
                env=_scrubbed_env(),
            )
            exit_code = proc.returncode
            ok = exit_code == spec.expected_exit
            output = (proc.stdout + "\n" + proc.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            ok = False
            infra_error = f"mechanical command could not run: {exc}"
            output = ""
        except Exception as exc:
            ok = False
            infra_error = f"unexpected error running command: {exc}"
            output = ""

        duration_ms = (time.perf_counter() - t0) * 1000.0

        output_artifact_path = None
        output_sha256 = None

        if conn is not None:
            try:
                # Determine artifact root
                if artifact_root is None:
                    # Anchor to parent of omniagentos package (up 3 levels from engine.py)
                    repo_root = os.path.dirname(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    )
                    art_root_path = Path(repo_root) / "var" / "gate-evidence"
                else:
                    art_root_path = Path(artifact_root)

                run_dir = run_id if run_id else "unscoped"
                task_dir = task_id if task_id else "unscoped"
                gate_dir = art_root_path / run_dir / task_dir
                gate_dir.mkdir(parents=True, exist_ok=True)

                safe_gate_name = "".join(
                    c if c.isalnum() or c in "-_" else "_" for c in (spec.name or "gate")
                )
                short_uuid = uuid.uuid4().hex[:8]
                filename = f"{safe_gate_name}-{short_uuid}.log"
                gate_file = gate_dir / filename

                # Write untruncated output with fsync
                output_bytes = output.encode("utf-8")
                output_sha256 = hashlib.sha256(output_bytes).hexdigest()

                fd = os.open(str(gate_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    os.write(fd, output_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)

                output_artifact_path = str(gate_file)

                # Parse test counts
                test_counts_json = _parse_pytest_counts(output)

                # Determine env digest
                env_raw = f"{sys.version_info} {sys.platform}".encode()
                env_digest = hashlib.sha256(env_raw).hexdigest()

                from omniagentos.contracts import new_id, utc_now_iso

                ev_id = new_id("gev")
                created_at = utc_now_iso()

                conn.execute(
                    """
                    INSERT INTO gate_evidence (
                        id, created_at, run_id, task_id, attempt_ref, gate_name, command,
                        exit_code, duration_ms, output_artifact_path, output_sha256,
                        test_counts_json, commit_sha, env_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ev_id,
                        created_at,
                        run_id,
                        task_id,
                        attempt_ref,
                        spec.name,
                        display_cmd,
                        exit_code,
                        duration_ms,
                        output_artifact_path,
                        output_sha256,
                        test_counts_json,
                        commit_sha,
                        env_digest,
                    ),
                )
                conn.commit()

            except Exception as exc:
                logging.getLogger("omniagentos.gates.engine").debug(
                    "Failed to record gate evidence: %s", exc, exc_info=True
                )

        results.append(
            GateResult(
                name=spec.name,
                command=display_cmd,
                ok=ok,
                exit_code=exit_code,
                output=output,
                duration_ms=duration_ms,
                blocking=spec.blocking,
                infra_error=infra_error,
                output_artifact_path=output_artifact_path,
                output_sha256=output_sha256,
            )
        )
    return results


def evaluate_evidence(criteria: Sequence[Any], results: Sequence[GateResult]) -> EvidenceVerdict:
    """Evaluate if required evidence for acceptance criteria exists in successfully executed gates."""
    required_criteria = []
    for c in criteria:
        try:
            if getattr(c, "evidence_required", False):
                required_criteria.append(c)
        except Exception:
            pass

    if not required_criteria:
        return EvidenceVerdict(ok=True, blocking=False, reason="", missing=())

    missing_ids: list[str] = []
    failed_reasons: list[str] = []

    for c in required_criteria:
        try:
            c_id = getattr(c, "id", None)
        except Exception:
            c_id = None

        if c_id is None:
            missing_ids.append("")
            failed_reasons.append("Required criterion is missing an id")
            continue

        matching_results = [r for r in results if r.name == c_id]
        if not matching_results:
            missing_ids.append(c_id)
            failed_reasons.append(f"No result found for required criterion '{c_id}'")
            continue

        satisfied = False
        for r in matching_results:
            if getattr(r, "ok", False) and getattr(r, "output_sha256", None):
                satisfied = True
                break

        if not satisfied:
            missing_ids.append(c_id)
            failed_reasons.append(f"Required criterion '{c_id}' lacks successful recorded evidence")

    if missing_ids:
        return EvidenceVerdict(
            ok=False,
            blocking=True,
            reason="; ".join(failed_reasons),
            missing=tuple(missing_ids),
        )

    return EvidenceVerdict(ok=True, blocking=False, reason="", missing=())


def blocking_failures(results: Sequence[GateResult]) -> list[GateResult]:
    """Filter gate results to return only those that failed and are marked as blocking."""
    return [r for r in results if not r.ok and r.blocking]
