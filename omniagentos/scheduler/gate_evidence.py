"""Signed, durable evidence that a routine's objective gate actually ran.

A routine is allowed to run unattended only because it declares an OBJECTIVE
gate.  That guarantee is worth nothing if the thing being graded can also write
its own report card, which is exactly what happens when the gate reads
``run.output_json``: the model produced that JSON, so ``{"exit_code": 0}`` or a
hand-written ``gate_evidence`` block certifies the run against itself.

This module is the trusted half of the chain::

    configured gate -> trusted executor -> durable signed evidence -> evaluator

:class:`GateEvidence` is a frozen record produced ONLY by
:mod:`omniagentos.scheduler.gate_runner`.  It binds, in one HMAC-covered
payload:

* the routine/run/iteration identity the work was done for,
* the normalized command and its resolved targets,
* the workspace SHA, clean tree status, and execution environment,
* an optional git candidate tip and merge-base for candidate-bound receipts,
* the node inventory digest and exact collected/passed/skipped/failed/deselected counts,
* the exit code and tool details,
* the execution window plus a single-use nonce.

:class:`GateEvidenceStore` persists one record per ``(routine_id, run_id)`` with
an exclusive create, so a second execution for the same run cannot overwrite the
first.  That makes recording idempotent and makes replay detectable: a record
whose signed identity does not match the run being settled is rejected, and a
record cannot be relocated to another run because ``run_id`` is inside the
signature.

The module also stores per-step merge-gate receipts (:class:`GateStepReceipt`):
one signed statement that one expensive gate step (ladder, counterfeit corpus,
dominance, doctrine, memlife) ran green for one exact ``(candidate SHA,
merge-base SHA, trial-merge tree, command)`` binding.  A step receipt is never
a verdict on its own — it only lets ``merge-gate.sh`` skip RE-RUNNING a step
the same candidate already ran green; any mismatch (SHA, tree, command,
signature, age) falls back to running the step for real, and the recorded
result must still satisfy the SAME verdict rule the live gate applies
(:func:`_counterfeit_verdict_rejections`), so reuse can never certify
something the live path would refuse.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import re
import secrets
import shlex
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import _repo_root, digest, utc_now_iso

SCHEMA = "omniagentos.gate-evidence.v3"
SCHEMA_V2 = "omniagentos.gate-evidence.v2"
SCHEMA_V1 = "omniagentos.gate-evidence.v1"
STEP_SCHEMA = "omniagentos.gate-step-receipt.v1"

MERGE_GATE_ROUTINE_ID = "merge-gate"
MERGE_GATE_TYPE = "merge_candidate"

#: The ONE verifier a merge-candidate receipt may have been produced by.
#:
#: This repository's merge gate grades THIS repository, which is Python, and
#: ``mint_candidate_receipt`` mints through ``PytestGateRunner`` unconditionally.
#: Pinning it here closes the other end of the M2 ecosystem work: a stored
#: ``merge_candidate`` routine that declared, say, ``ecosystem: go`` would settle
#: through the ecosystem dispatcher and — because merge-candidate evidence is
#: written under the FIXED merge-gate identity (routine_id ``merge-gate``,
#: run_id = candidate SHA) — would land a signed, structurally valid receipt at
#: exactly the path ``merge-gate.sh`` reads. Two go tests passing in some
#: subdirectory would then authorize a merge of the whole Python tree. The
#: admission path refuses to STORE such a routine; this refuses to BELIEVE one,
#: which is the half that also covers rows written before that validation
#: existed (stored rows are never re-validated).
MERGE_GATE_TOOL = "pytest"
MERGE_GATE_STEPS_DIR = "merge-gate-steps"

#: The one merge-gate step whose verdict is a corpus REPORT LINE plus an exit
#: code, not a pytest tail line. Named once so the live gate, the receipt
#: minter and the receipt verifier cannot disagree about which step it is.
COUNTERFEIT_STEP = "counterfeit-gate"

# The only gate steps expensive enough to earn a skip-on-reuse receipt.
# ``merge-gate.sh`` display names may differ (e.g. "ladder(api,swarm,sessions)");
# the step id here is the durable, path-safe identity inside the receipt.
#
# 2026-08-12: ``contracts-scripts`` was SPLIT into a parallel ``contracts`` leg
# and a SERIAL ``scripts`` step, and ``scheduler`` was lifted OUT of the xdist
# ladder into its own SERIAL step. Both moves exist because ``tests/scripts``
# (its merge-gate suites spawn the real ``merge-gate.sh``) and ``tests/scheduler``
# drive the real gate's ``os.killpg`` process-group reap, which false-refuses a
# whole train under xdist — a sibling worker exits before the pgroup leader is
# signalled, ``killpg`` returns EPERM, and the run raises
# ``GateExecutionInfraError``. Each serial step still earns its own
# skip-on-reuse receipt, so all three ids are allowlisted here. The retired
# ``contracts-scripts`` id is intentionally absent: its command string (both
# dirs, under ``-n``) no longer exists for any run to record or reuse.
MERGE_GATE_STEP_NAMES = (
    "ladder",
    COUNTERFEIT_STEP,
    "scheduler",
    "contracts",
    "scripts",
    "pipeline-tests",
    "dominance-corpus",
    "doctrine",
    "memlife",
)

MAX_EVIDENCE_AGE_SECONDS = 24 * 60 * 60
_MAX_CLAIM_BYTES = 16 * 1024
_CLAIM_ARTIFACT_GRACE_SECONDS = 120.0
_CLAIM_ARTIFACT_MAX_AGE_SECONDS = 24 * 60 * 60
_SKIP_ALLOWLIST_FILENAME = ".gate-skip-allowlist.yaml"

_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")
_TIMESTAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_GIT_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
#: ``skipped_platform`` IS OPTIONAL, and both spellings are the same verdict.
#: ``tests/counterfeits/harness.py`` grew that field when platform pinning
#: landed; this pattern kept requiring the four-field form, so a perfectly
#: healthy corpus that exited 0 parsed as "produced NO verdict line (did not
#: run)" and then as an exit-code/report DISAGREEMENT — a green run refused,
#: and, for the receipt verifier, every stored counterfeit receipt with it.
#: Old four-field receipts still exist inside their 24h freshness window and
#: still have to verify, so the field is optional rather than newly required:
#: this parser reads BOTH what the harness emits today and what it emitted
#: yesterday. Absent is read as 0 skips, which is what the four-field line meant.
_COUNTERFEIT_REPORT_RE = re.compile(
    r"total=(?P<total>\d+) +caught=(?P<caught>\d+) +"
    r"survived=(?P<survived>\d+) +"
    r"(?:skipped_platform=(?P<skipped_platform>\d+) +)?"
    r"other=(?P<other>\d+)"
)
# Anything that SMELLS like a corpus verdict but does not parse as one is
# judged by the counterfeit rule anyway — and fails it, because a partial or
# superseded report format cannot certify what it does not state. Without this,
# dropping a field from the report line would turn every corpus result into "not
# a counterfeit verdict, nothing to check". No pytest tail line contains these.
_COUNTERFEIT_HINT_RE = re.compile(r"\bsurvived=\d+|\bcaught=\d+")
# First line worth showing an operator when the corpus refuses. The harness
# prefixes its own refusals ("COUNTERFEIT GATE REFUSED:", "GATE RED:") and a
# timed-out entry surfaces as a bare traceback; a reason-grep that matches
# none of those hands the operator an empty string.
_HARNESS_DIAGNOSTIC_RE = re.compile(
    r"^(?:COUNTERFEIT GATE [A-Z ]+|GATE RED|Traceback \(most recent call last\)|"
    r"SURVIVED|APPLY_ERR|COLLECT|WRONG_FAIL|ERROR|FAILED|REFUSED|error).*$",
    re.MULTILINE,
)
# The first pytest node id the excerpt names. A control refusal's reason line
# says the corpus "points at broken or missing tests"; only this says WHICH.
_HARNESS_FAILED_NODE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
# Long enough to carry header + reason + one node id; still one report line.
_DIAGNOSTIC_MAX_CHARS = 400


class GateEvidenceError(RuntimeError):
    """Raised when trusted evidence cannot be produced, stored, or trusted."""


class GateEvidenceExists(GateEvidenceError):
    """Raised when a run already has recorded evidence (idempotency guard)."""


class GateEvidenceRefusal(GateEvidenceError):
    """Raised when gate configuration or environment cannot produce trustworthy execution."""


class GateWorkspaceUnusable(GateEvidenceRefusal):
    """The workspace itself could not be executed against — ABSENCE, not failure.

    A refusal normally says something about the CANDIDATE: its gate command is
    not a real verifier, it names a target that does not exist, its execution
    was manipulated. Callers are right to treat those as unfavourable.

    This subclass carves out the refusals that say something about the
    WORKSPACE instead — it is missing, it is not a git checkout, its tree is
    dirty. Nothing was judged, because nothing could be run. Distinguishing it
    matters because ``routines_settle`` maps a refusal to ``gate_passed=0``,
    which is counted in the 50% auto-pause acceptance floor: a workspace that
    goes dirty between configuration and settlement would otherwise auto-pause
    a healthy routine on a failure that never happened. Absence of evidence is
    not evidence of failure.

    It remains a ``GateEvidenceRefusal``, so every existing ``except`` clause
    still catches it and no caller becomes more permissive by accident. In
    particular ``integration/promote.py`` demands ``status == "evidence"`` and
    refuses everything else, so the merge gate still refuses a dirty candidate
    workspace exactly as hard as before.
    """


class GateExecutionInfraError(GateEvidenceError):
    """Raised when the executor itself fails (process leak, I/O, corrupted artifact)."""


@dataclass(frozen=True, slots=True)
class ClaimToken:
    """An active single-flight claim lease."""

    claim_path: Path
    holder_uuid: str
    routine_id: str
    run_id: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _ClaimSnapshot:
    """Identity and exact bytes of one opened claim filesystem object."""

    device: int
    inode: int
    raw: bytes
    data: dict[str, Any]

    @property
    def holder_uuid(self) -> str | None:
        value = self.data.get("holder_uuid")
        return value if isinstance(value, str) and value else None

    @property
    def expires_at(self) -> float | None:
        value = self.data.get("expires_at")
        if not isinstance(value, int | float | str):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """One immutable, signed statement about one gate execution."""

    schema: str
    routine_id: str
    run_id: str
    iteration: int
    gate_type: str
    command: str
    targets: tuple[str, ...]
    workspace_digest: str
    binding_digest: str
    tool: str
    tool_version: str
    exit_code: int
    checks_collected: int
    checks_passed: int
    checks_skipped: int
    checks_failed: int
    started_at: str
    finished_at: str
    nonce: str
    workspace_sha: str = ""
    workspace_tree_clean: bool = True
    interpreter: str = ""
    interpreter_version: str = ""
    node_inventory_digest: str = ""
    deselected_count: int = 0
    candidate_sha: str = ""
    merge_base_sha: str = ""
    signature: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["targets"] = list(self.targets)
        return payload


@dataclass(frozen=True, slots=True)
class GateStepReceipt:
    """One immutable, signed statement that one merge-gate step ran green.

    Binds, in one HMAC-covered payload: the step name, the exact candidate and
    merge-base SHAs, the trial-merge TREE the step actually executed against
    (the gate's suites run on the merge commit, so the same candidate over a
    moved main is different tested content), the normalized command, the
    workspace path digest, the digest of the step's captured output artifact,
    and the execution window.  ``exit_code`` is the step process's OWN exit
    status as measured by the gate — never an asserted constant — and for the
    counterfeit corpus it is judged together with ``summary`` by the single
    rule in :func:`_counterfeit_verdict_rejections`, so a receipt can never
    certify a run the live gate would refuse.
    """

    schema: str
    step: str
    candidate_sha: str
    merge_base_sha: str
    merge_tree_sha: str
    command: str
    workspace_digest: str
    output_digest: str
    exit_code: int
    summary: str
    started_at: str
    finished_at: str
    nonce: str
    signature: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def normalize_gate_command(command: str) -> str:
    """Canonical single-line form of a gate command."""
    if not isinstance(command, str):
        raise GateEvidenceRefusal("gate command must be a string")
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        raise GateEvidenceRefusal(f"gate command is unparseable: {exc}") from None
    if not parts:
        raise GateEvidenceRefusal("gate command is empty")
    return shlex.join(parts)


def binding_digest(
    *,
    routine_id: str,
    run_id: str,
    iteration: int,
    gate_type: str,
    command: str,
    targets: tuple[str, ...],
    workspace_digest: str,
    candidate_sha: str = "",
    merge_base_sha: str = "",
) -> str:
    """Digest binding one run's work to its workspace and optional git candidate."""
    return digest(
        json.dumps(
            {
                "schema": SCHEMA,
                "routine_id": routine_id,
                "run_id": run_id,
                "iteration": int(iteration),
                "gate_type": gate_type,
                "command": normalize_gate_command(command),
                "targets": list(targets),
                "workspace_digest": workspace_digest,
                "candidate_sha": candidate_sha,
                "merge_base_sha": merge_base_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _canonical_signing_bytes(evidence: GateEvidence) -> bytes:
    payload = evidence.to_payload()
    payload.pop("signature", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_step_signing_bytes(receipt: GateStepReceipt) -> bytes:
    payload = receipt.to_payload()
    payload.pop("signature", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def gate_evidence_dir() -> Path:
    """Runtime directory for gate evidence, honoring ``OMNIAGENTOS_VAR_DIR``."""
    base = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.path.join(_repo_root(), "var")
    return Path(base).expanduser() / "gate-evidence"


def _load_or_create_key(root: Path) -> bytes:
    """Read the installation's signing key, creating a 0600 key on first use."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = root / "signing.key"
    try:
        raw = key_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    if len(raw) >= 32:
        return raw
    key = secrets.token_bytes(32)
    try:
        handle = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return key_path.read_bytes()
    with os.fdopen(handle, "wb") as stream:
        stream.write(key)
    return key


def _load_existing_key(root: Path) -> bytes:
    """Load a verification key without creating trust state as a side effect."""
    key_path = root / "signing.key"
    try:
        raw = key_path.read_bytes()
    except OSError as exc:
        raise GateExecutionInfraError(f"gate evidence signing key is unavailable: {exc}") from exc
    if len(raw) < 32:
        raise GateExecutionInfraError(f"gate evidence signing key is invalid at {key_path}")
    return raw


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GateEvidenceError(f"{label} is required")
    if _ID_RE.search(value):
        raise GateEvidenceError(f"{label} contains unsafe characters: {value!r}")
    return value


def _read_claim_snapshot(path: Path) -> _ClaimSnapshot | None:
    """Open *path* without following links and bind parsed bytes to its inode."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CLAIM_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = _MAX_CLAIM_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if (
            len(raw) > _MAX_CLAIM_BYTES
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or len(raw) != after.st_size
        ):
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return _ClaimSnapshot(
            device=int(after.st_dev),
            inode=int(after.st_ino),
            raw=raw,
            data=data,
        )
    except OSError:
        return None
    finally:
        os.close(fd)


def _same_claim_object(left: _ClaimSnapshot, right: _ClaimSnapshot | None) -> bool:
    """Compare exact object identity and bytes, resisting UUID and inode ABA."""
    return (
        right is not None
        and left.device == right.device
        and left.inode == right.inode
        and hmac.compare_digest(left.raw, right.raw)
    )


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class GateEvidenceStore:
    """Durable, append-only store of signed gate evidence."""

    def __init__(self, root: Path | str | None = None, *, create_key: bool = True) -> None:
        self.root = Path(root).expanduser() if root is not None else gate_evidence_dir()
        self._key = _load_or_create_key(self.root) if create_key else _load_existing_key(self.root)

    def _record_path(self, routine_id: str, run_id: str) -> Path:
        return (
            self.root
            / "records"
            / _safe_component(routine_id, "routine_id")
            / f"{_safe_component(run_id, 'run_id')}.json"
        )

    def _claim_path(self, routine_id: str, run_id: str) -> Path:
        return (
            self.root
            / "claims"
            / _safe_component(routine_id, "routine_id")
            / f"{_safe_component(run_id, 'run_id')}.json"
        )

    def _step_receipt_path(self, step: str, candidate_sha: str) -> Path:
        return (
            self.root
            / "records"
            / MERGE_GATE_STEPS_DIR
            / _safe_component(step, "step")
            / f"{_safe_component(candidate_sha, 'candidate_sha')}.json"
        )

    @staticmethod
    def _claim_lock_path(path: Path) -> Path:
        return path.parent / f".{path.name}.mutation.lock"

    def _open_claim_lock(self, path: Path, *, blocking: bool) -> int | None:
        """Lock all cooperative claim-path mutations; the kernel releases on crash."""
        lock_path = self._claim_lock_path(path)
        fd: int | None = None
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
            opened = os.fstat(fd)
            named = os.stat(lock_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != named.st_dev
                or opened.st_ino != named.st_ino
            ):
                os.close(fd)
                return None
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(fd, operation)
            return fd
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return None

    @staticmethod
    def _close_claim_lock(fd: int | None) -> None:
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _write_claim_candidate(path: Path, body: bytes) -> bool:
        try:
            fd = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except OSError:
            path.unlink(missing_ok=True)
            return False

    @staticmethod
    def _token_for(
        path: Path,
        *,
        expected: _ClaimSnapshot,
        holder_uuid: str,
        routine_id: str,
        run_id: str,
    ) -> ClaimToken | None:
        snapshot = _read_claim_snapshot(path)
        if (
            not _same_claim_object(expected, snapshot)
            or snapshot is None
            or snapshot.holder_uuid != holder_uuid
        ):
            return None
        return ClaimToken(
            claim_path=path,
            holder_uuid=holder_uuid,
            routine_id=routine_id,
            run_id=run_id,
            device=snapshot.device,
            inode=snapshot.inode,
        )

    @staticmethod
    def _restore_isolated_claim(isolated: Path, path: Path) -> bool:
        """Restore the same inode without replacing a newer path occupant."""
        try:
            os.link(isolated, path, follow_symlinks=False)
        except FileExistsError:
            return False
        except OSError:
            return False
        isolated.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return True

    @staticmethod
    def _cleanup_claim_artifacts(path: Path, *, now_ts: float) -> None:
        """Bound crash leftovers without touching a possibly live isolated claim."""
        patterns = (
            f".{path.name}.claim.tmp-*",
            f".{path.name}.reclaim-*",
        )
        for pattern in patterns:
            for candidate in path.parent.glob(pattern):
                try:
                    info = candidate.stat(follow_symlinks=False)
                except OSError:
                    continue
                age = max(0.0, now_ts - info.st_mtime)
                if age < _CLAIM_ARTIFACT_GRACE_SECONDS:
                    continue
                snapshot = _read_claim_snapshot(candidate)
                expires_at = snapshot.expires_at if snapshot is not None else None
                safely_expired = (
                    expires_at is not None and now_ts >= expires_at + _CLAIM_ARTIFACT_GRACE_SECONDS
                )
                if safely_expired or age >= _CLAIM_ARTIFACT_MAX_AGE_SECONDS:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass

    def sign(self, evidence: GateEvidence) -> GateEvidence:
        signature = hmac.new(self._key, _canonical_signing_bytes(evidence), "sha256").hexdigest()
        return replace(evidence, signature=signature)

    def verify(self, evidence: GateEvidence) -> bool:
        expected = hmac.new(self._key, _canonical_signing_bytes(evidence), "sha256").hexdigest()
        return hmac.compare_digest(expected, evidence.signature)

    def sign_step(self, receipt: GateStepReceipt) -> GateStepReceipt:
        signature = hmac.new(
            self._key, _canonical_step_signing_bytes(receipt), "sha256"
        ).hexdigest()
        return replace(receipt, signature=signature)

    def verify_step(self, receipt: GateStepReceipt) -> bool:
        expected = hmac.new(self._key, _canonical_step_signing_bytes(receipt), "sha256").hexdigest()
        return hmac.compare_digest(expected, receipt.signature)

    def claim(
        self, routine_id: str, run_id: str, *, ttl_seconds: float = 960.0
    ) -> ClaimToken | None:
        """Acquire an exact-object-bound execution lease for ``(routine_id, run_id)``.

        Publication is always no-replace. Expired takeover first isolates the
        currently named object, proves that its device/inode/bytes are exactly
        the stale object inspected, and only then links the new claim into an
        empty canonical path. A path replacement at any phase is preserved and
        makes this caller lose.
        """
        path = self._claim_path(routine_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        holder_uuid = secrets.token_hex(16)
        now_ts = datetime.now(UTC).timestamp()
        expires_at = now_ts + ttl_seconds
        body = (
            json.dumps(
                {
                    "holder_uuid": holder_uuid,
                    "pid": os.getpid(),
                    "hostname": os.uname().nodename if hasattr(os, "uname") else "localhost",
                    "created_at": now_ts,
                    "expires_at": expires_at,
                }
            )
            + "\n"
        ).encode("utf-8")

        lock_fd = self._open_claim_lock(path, blocking=False)
        if lock_fd is None:
            return None

        tmp_path = path.parent / (f".{path.name}.claim.tmp-{os.getpid()}-{secrets.token_hex(8)}")
        isolated_path: Path | None = None
        remove_isolated = False
        try:
            self._cleanup_claim_artifacts(path, now_ts=now_ts)
            if not self._write_claim_candidate(tmp_path, body):
                return None
            candidate = _read_claim_snapshot(tmp_path)
            if candidate is None or candidate.holder_uuid != holder_uuid:
                return None

            try:
                os.link(tmp_path, path, follow_symlinks=False)
            except FileExistsError:
                pass
            except OSError:
                return None
            else:
                _fsync_directory(path.parent)
                return self._token_for(
                    path,
                    expected=candidate,
                    holder_uuid=holder_uuid,
                    routine_id=routine_id,
                    run_id=run_id,
                )

            inspected = _read_claim_snapshot(path)
            if inspected is None:
                # Never interpret an empty, partial, linked, or unreadable
                # canonical claim as expired.
                return None
            old_holder_uuid = inspected.holder_uuid
            old_expires_at = inspected.expires_at
            if old_holder_uuid is None or old_expires_at is None:
                return None
            try:
                _safe_component(old_holder_uuid, "holder_uuid")
            except GateEvidenceError:
                return None
            if old_expires_at <= 0.0 or now_ts < old_expires_at:
                return None

            # Revalidate exact object identity immediately before isolation.
            if not _same_claim_object(inspected, _read_claim_snapshot(path)):
                return None

            isolated_path = path.parent / (
                f".{path.name}.reclaim-{holder_uuid}-{secrets.token_hex(8)}"
            )
            try:
                os.rename(path, isolated_path)
            except OSError:
                return None

            isolated = _read_claim_snapshot(isolated_path)
            if not _same_claim_object(inspected, isolated):
                # The path changed after revalidation. Put that exact displaced
                # inode back without overwriting anything published meanwhile.
                remove_isolated = self._restore_isolated_claim(isolated_path, path)
                return None

            remove_isolated = True
            try:
                # No-replace publication: a claimant that appears after
                # isolation wins and is never clobbered.
                os.link(tmp_path, path, follow_symlinks=False)
            except FileExistsError:
                return None
            except OSError:
                return None

            _fsync_directory(path.parent)
            return self._token_for(
                path,
                expected=candidate,
                holder_uuid=holder_uuid,
                routine_id=routine_id,
                run_id=run_id,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
            if isolated_path is not None and remove_isolated:
                isolated_path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
            self._close_claim_lock(lock_fd)

    def release_claim(self, token: ClaimToken) -> None:
        """Release only the exact inode originally granted to *token*."""
        lock_fd = self._open_claim_lock(token.claim_path, blocking=True)
        if lock_fd is None:
            return
        try:
            snapshot = _read_claim_snapshot(token.claim_path)
            identity_matches = (
                snapshot is not None
                and snapshot.holder_uuid == token.holder_uuid
                and snapshot.device == token.device
                and snapshot.inode == token.inode
            )
            if identity_matches:
                token.claim_path.unlink(missing_ok=True)
                _fsync_directory(token.claim_path.parent)
        except OSError:
            pass
        finally:
            self._close_claim_lock(lock_fd)

    def record(self, evidence: GateEvidence) -> GateEvidence:
        """Sign and durably store *evidence* using atomic write-tmp-link."""
        signed = self.sign(evidence)
        path = self._record_path(signed.routine_id, signed.run_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n"

        tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise GateExecutionInfraError(f"failed to write temporary evidence: {exc}") from exc

        try:
            os.link(tmp_path, path)
        except FileExistsError:
            tmp_path.unlink(missing_ok=True)
            raise GateEvidenceExists(
                f"gate evidence already recorded for run {signed.run_id}"
            ) from None
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise GateExecutionInfraError(f"failed to link evidence file: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)

        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

        return signed

    def _load_path(
        self,
        path: Path,
        *,
        quarantine_legacy: bool,
    ) -> GateEvidence | None:
        """Load one exact evidence file and authenticate it before returning."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GateExecutionInfraError(f"cannot inspect evidence file at {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise GateExecutionInfraError(f"evidence path is not a regular file at {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise GateExecutionInfraError(
                f"tampered or unparsable evidence file at {path}"
            ) from None

        if not isinstance(payload, dict):
            raise GateExecutionInfraError(f"tampered evidence payload at {path}")

        fields = set(GateEvidence.__dataclass_fields__)
        if set(payload) != fields:
            legacy_fields = {
                SCHEMA_V1: {
                    "schema",
                    "routine_id",
                    "run_id",
                    "iteration",
                    "gate_type",
                    "command",
                    "targets",
                    "workspace_digest",
                    "binding_digest",
                    "tool",
                    "tool_version",
                    "exit_code",
                    "checks_collected",
                    "checks_passed",
                    "checks_skipped",
                    "checks_failed",
                    "started_at",
                    "finished_at",
                    "nonce",
                    "signature",
                },
                SCHEMA_V2: fields - {"candidate_sha", "merge_base_sha"},
            }
            schema = payload.get("schema")
            expected_legacy_fields = legacy_fields.get(schema) if isinstance(schema, str) else None
            if expected_legacy_fields is not None and set(payload) == expected_legacy_fields:
                legacy_no_sig = {k: v for k, v in payload.items() if k != "signature"}
                legacy_bytes = json.dumps(
                    legacy_no_sig,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                expected_legacy_sig = hmac.new(self._key, legacy_bytes, "sha256").hexdigest()
                if not hmac.compare_digest(
                    expected_legacy_sig,
                    str(payload.get("signature") or ""),
                ):
                    version = str(schema).rsplit(".", 1)[-1]
                    raise GateExecutionInfraError(f"invalid {version} signature at {path}")

                if quarantine_legacy:
                    version = str(schema).rsplit(".", 1)[-1]
                    superseded_path = path.with_name(f"{path.name}.superseded-{version}")
                    try:
                        path.rename(superseded_path)
                    except OSError as exc:
                        raise GateExecutionInfraError(
                            f"cannot quarantine legacy evidence at {path}: {exc}"
                        ) from exc
                    return None
                raise GateExecutionInfraError(
                    f"legacy evidence at {path} has no signed candidate binding"
                )

            raise GateExecutionInfraError(f"invalid evidence fields at {path}")

        try:
            evidence = GateEvidence(
                **{
                    **payload,
                    "targets": tuple(payload.get("targets") or ()),
                }
            )
        except TypeError:
            raise GateExecutionInfraError(f"invalid evidence structure at {path}") from None

        if not self.verify(evidence):
            raise GateExecutionInfraError(f"invalid evidence signature at {path}")

        return evidence

    def load(self, routine_id: str, run_id: str) -> GateEvidence | None:
        """Return stored evidence for a run, or raise on tampered/corrupt records."""
        try:
            path = self._record_path(routine_id, run_id)
        except GateEvidenceError:
            return None
        evidence = self._load_path(path, quarantine_legacy=True)
        if evidence is None:
            return None
        if evidence.routine_id != routine_id or evidence.run_id != run_id:
            raise GateExecutionInfraError(f"evidence identity mismatch at {path}")
        return evidence

    def load_receipt(self, path: Path | str) -> GateEvidence | None:
        """Authenticate a standalone view of a record without trusting its filename."""
        return self._load_path(Path(path).expanduser(), quarantine_legacy=False)

    def record_step(self, receipt: GateStepReceipt) -> GateStepReceipt:
        """Sign and durably store *receipt*, replacing any prior one for its slot.

        Run evidence is exclusive-create because it is history.  A step receipt
        is a re-run skip cache for one ``(step, candidate)`` slot, so a newer
        green receipt (fresher window, changed command, moved trial-merge tree)
        must be able to supersede a stale one.  Integrity is carried by the
        signature and the verify-time binding, not by write exclusivity: a
        reader that loses a race sees either a valid receipt for exactly its
        binding or a mismatch — and a mismatch always re-runs the step.
        """
        signed = self.sign_step(receipt)
        path = self._step_receipt_path(signed.step, signed.candidate_sha)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n"

        tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise GateExecutionInfraError(f"failed to write step receipt: {exc}") from exc

        _fsync_directory(path.parent)
        return signed

    def load_step(self, step: str, candidate_sha: str) -> GateStepReceipt | None:
        """Return the authenticated step receipt for one slot, or None if absent.

        Raises :class:`GateExecutionInfraError` on a tampered, unparsable, or
        relocated receipt — the caller treats that exactly like a missing
        receipt (run the step for real), but loudly rather than silently.
        """
        try:
            path = self._step_receipt_path(step, candidate_sha)
        except GateEvidenceError:
            return None
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GateExecutionInfraError(f"cannot inspect step receipt at {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise GateExecutionInfraError(f"step receipt path is not a regular file at {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise GateExecutionInfraError(
                f"tampered or unparsable step receipt at {path}"
            ) from None
        if not isinstance(payload, dict) or set(payload) != set(
            GateStepReceipt.__dataclass_fields__
        ):
            raise GateExecutionInfraError(f"invalid step receipt fields at {path}")

        try:
            receipt = GateStepReceipt(**payload)
        except TypeError:
            raise GateExecutionInfraError(f"invalid step receipt structure at {path}") from None

        if not self.verify_step(receipt):
            raise GateExecutionInfraError(f"invalid step receipt signature at {path}")
        if receipt.step != step or receipt.candidate_sha != candidate_sha:
            raise GateExecutionInfraError(f"step receipt identity mismatch at {path}")
        return receipt


def _parse_timestamp(value: str) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def load_skip_allowlist() -> dict[str, Any] | None:
    """Load the verdict-time skip budget declared at the workspace root.

    The allowlist deliberately remains outside :class:`GateEvidence`: it is a
    current workspace policy decision, not candidate-supplied receipt content.
    An empty file is equivalent to no allowlist; malformed or incomplete
    declarations fail closed when a gate has skipped checks.
    """
    path = Path(_repo_root()) / _SKIP_ALLOWLIST_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GateEvidenceRefusal(f"cannot read skip allowlist at {path}: {exc}") from None

    if not raw.strip():
        return {}
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise GateEvidenceRefusal(
            f"skip allowlist at {_SKIP_ALLOWLIST_FILENAME} is invalid YAML: {exc}"
        ) from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise GateEvidenceRefusal(f"skip allowlist at {_SKIP_ALLOWLIST_FILENAME} must be a mapping")

    budget = loaded.get("budget")
    if type(budget) is not int or budget <= 0:
        raise GateEvidenceRefusal(
            f"skip allowlist at {_SKIP_ALLOWLIST_FILENAME} must declare a positive integer budget"
        )

    declared = loaded.get("declared", [])
    if not isinstance(declared, list):
        raise GateEvidenceRefusal(
            f"skip allowlist at {_SKIP_ALLOWLIST_FILENAME} declared entries must be a list"
        )
    normalized_declared: list[dict[str, str]] = []
    for entry in declared:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("test"), str)
            or not entry["test"].strip()
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
        ):
            raise GateEvidenceRefusal(
                f"skip allowlist at {_SKIP_ALLOWLIST_FILENAME} entries require test and reason strings"
            )
        normalized_declared.append({"test": entry["test"], "reason": entry["reason"]})
    return {"budget": budget, "declared": normalized_declared}


def _skipped_checks_rejection(checks_skipped: int, *, subject: str = "gate") -> str | None:
    """Return a refusal for unbudgeted skips, or ``None`` when policy allows them."""
    if checks_skipped == 0:
        return None
    try:
        allowlist = load_skip_allowlist()
    except GateEvidenceRefusal as exc:
        return f"{subject} had {checks_skipped} skipped checks; {exc}"
    if not allowlist:
        return (
            f"{subject} had {checks_skipped} skipped checks; "
            f"skipped checks require a declared allowlist at {_SKIP_ALLOWLIST_FILENAME}"
        )
    budget = allowlist["budget"]
    if checks_skipped > budget:
        return f"{subject} had {checks_skipped} skipped checks (exceeds budget of {budget})"
    return None


def evidence_rejections(
    evidence: object,
    *,
    routine_id: str,
    run_id: str,
    iteration: int,
    gate_type: str,
    gate_config: dict[str, Any],
    workspace_digest: str,
    now: datetime,
    verifier: Callable[[GateEvidence], bool] | None = None,
) -> list[str]:
    """Every reason *evidence* must not be accepted; empty means it passes."""
    if verifier is not None:
        if not isinstance(evidence, GateEvidence) or not verifier(evidence):
            return ["gate evidence signature is not verifiable at the decision point"]

    if not isinstance(evidence, GateEvidence):
        return ["gate evidence is not a trusted GateEvidence record"]

    errors: list[str] = []
    if evidence.schema != SCHEMA:
        errors.append("gate evidence schema is invalid")
    if evidence.routine_id != routine_id:
        errors.append("gate evidence belongs to a different routine")
    if evidence.run_id != run_id:
        errors.append("gate evidence belongs to a different run")
    if evidence.iteration != iteration:
        errors.append("gate evidence belongs to a different iteration")
    if evidence.gate_type != gate_type:
        errors.append("gate evidence was produced for a different gate type")

    try:
        expected_command = normalize_gate_command(str(gate_config.get("command") or ""))
    except GateEvidenceError:
        return [*errors, "gate config has no usable command"]
    if evidence.command != expected_command:
        errors.append("gate evidence command does not match the configured gate")
    if not evidence.targets:
        errors.append("gate evidence records no execution targets")
    if evidence.workspace_digest != workspace_digest:
        errors.append("gate evidence was produced in a different workspace")

    if not _GIT_SHA_RE.match(evidence.workspace_sha):
        errors.append("gate evidence workspace SHA is invalid")
    if not evidence.workspace_tree_clean:
        errors.append("gate workspace was modified during or after execution")
    if evidence.candidate_sha:
        if not _GIT_SHA_RE.match(evidence.candidate_sha):
            errors.append("gate evidence candidate SHA is invalid")
        elif evidence.candidate_sha != evidence.workspace_sha:
            errors.append("gate evidence candidate SHA does not match its workspace tip")
    if evidence.merge_base_sha:
        if not _GIT_SHA_RE.match(evidence.merge_base_sha):
            errors.append("gate evidence merge-base SHA is invalid")
        if not evidence.candidate_sha:
            errors.append("gate evidence merge-base has no candidate SHA")
    if evidence.deselected_count > 0:
        errors.append(f"gate had {evidence.deselected_count} deselected checks")
    if not evidence.interpreter or not Path(evidence.interpreter).is_absolute():
        errors.append("gate evidence interpreter path is invalid")
    if not re.match(r"\A[0-9a-f]{64}\Z", evidence.node_inventory_digest):
        errors.append("gate evidence node inventory digest is invalid")

    try:
        expected_digest = binding_digest(
            routine_id=routine_id,
            run_id=run_id,
            iteration=iteration,
            gate_type=gate_type,
            command=expected_command,
            targets=evidence.targets,
            workspace_digest=workspace_digest,
            candidate_sha=evidence.candidate_sha,
            merge_base_sha=evidence.merge_base_sha,
        )
    except GateEvidenceError:
        expected_digest = ""
    if not expected_digest or not hmac.compare_digest(evidence.binding_digest, expected_digest):
        errors.append("gate evidence binding digest does not match this routine run")

    expected_exit = gate_config.get("expected_exit_code", 0)
    if type(expected_exit) is not int or type(evidence.exit_code) is not int:
        errors.append("gate exit codes must be integers")
    elif evidence.exit_code != expected_exit:
        errors.append(f"gate exited {evidence.exit_code}, expected {expected_exit}")

    counts = (
        evidence.checks_collected,
        evidence.checks_passed,
        evidence.checks_skipped,
        evidence.checks_failed,
    )
    if any(type(count) is not int or count < 0 for count in counts):
        errors.append("gate evidence check counts are invalid")
    else:
        if evidence.checks_collected == 0:
            errors.append("gate collected zero checks (vacuous pass)")
        if evidence.checks_failed:
            errors.append(f"gate had {evidence.checks_failed} failed checks")
        skip_rejection = _skipped_checks_rejection(evidence.checks_skipped)
        if skip_rejection:
            errors.append(skip_rejection)
        if evidence.checks_passed + evidence.checks_skipped != evidence.checks_collected:
            errors.append("gate did not pass every collected check (partial execution)")

    if not evidence.tool or not evidence.tool_version:
        errors.append("gate evidence does not identify its executing tool")
    else:
        # M2: bind the adjudicated config's declared ecosystem to the executor
        # that actually produced the counts. Without this, `tool` is only
        # required to be non-empty, so evidence from one ecosystem's executor
        # could be adjudicated against a config that selected another — and an
        # unknown ecosystem string would be a silent no-op here instead of a
        # refusal. Imported lazily: gate_ecosystems imports this module.
        from omniagentos.scheduler.gate_ecosystems import expected_tool_for_gate_config

        try:
            expected_tool = expected_tool_for_gate_config(gate_config)
        except GateEvidenceRefusal as exc:
            errors.append(f"gate config does not select a usable ecosystem: {exc}")
        else:
            if evidence.tool != expected_tool:
                errors.append(
                    f"gate evidence was produced by {evidence.tool!r}, but this gate "
                    f"declares the {expected_tool!r} verifier"
                )
    if len(evidence.nonce) < 16:
        errors.append("gate evidence has no usable nonce")

    started = _parse_timestamp(evidence.started_at)
    finished = _parse_timestamp(evidence.finished_at)
    if started is None or finished is None:
        errors.append("gate evidence timestamps are invalid")
    elif finished < started:
        errors.append("gate evidence finished before it started")
    else:
        age = (now.astimezone(UTC) - finished).total_seconds()
        if age > MAX_EVIDENCE_AGE_SECONDS:
            errors.append("gate evidence is stale")
        elif age < -60:
            errors.append("gate evidence is dated in the future")

    return errors


def candidate_receipt_rejections(
    evidence: GateEvidence,
    *,
    candidate_sha: str,
    merge_base_sha: str,
    now: datetime | None = None,
) -> list[str]:
    """Return every semantic reason an authenticated merge receipt is unusable."""
    errors: list[str] = []
    if not _GIT_SHA_RE.match(candidate_sha):
        errors.append("expected candidate SHA is invalid")
    if not _GIT_SHA_RE.match(merge_base_sha):
        errors.append("expected merge-base SHA is invalid")
    if evidence.schema != SCHEMA:
        errors.append("signed receipt schema is invalid")
    if evidence.routine_id != MERGE_GATE_ROUTINE_ID:
        errors.append("signed receipt belongs to a different routine")
    if evidence.run_id != candidate_sha:
        errors.append("signed receipt belongs to a different candidate run")
    if evidence.gate_type != MERGE_GATE_TYPE:
        errors.append("signed receipt was produced for a different gate type")
    if not hmac.compare_digest(evidence.candidate_sha, candidate_sha):
        errors.append("signed receipt is bound to a different candidate SHA")
    if not hmac.compare_digest(evidence.merge_base_sha, merge_base_sha):
        errors.append("signed receipt is bound to a different merge-base SHA")
    if not hmac.compare_digest(evidence.workspace_sha, candidate_sha):
        errors.append("signed receipt workspace tip is not the candidate SHA")
    if not evidence.workspace_tree_clean:
        errors.append("signed receipt says the verified workspace tree was dirty")

    try:
        expected_binding = binding_digest(
            routine_id=MERGE_GATE_ROUTINE_ID,
            run_id=candidate_sha,
            iteration=evidence.iteration,
            gate_type=MERGE_GATE_TYPE,
            command=evidence.command,
            targets=evidence.targets,
            workspace_digest=evidence.workspace_digest,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
        )
    except GateEvidenceError:
        expected_binding = ""
    if not expected_binding or not hmac.compare_digest(
        evidence.binding_digest,
        expected_binding,
    ):
        errors.append("signed receipt binding digest does not match this candidate")

    if not evidence.targets:
        errors.append("signed receipt records no verification targets")
    if type(evidence.exit_code) is not int or evidence.exit_code != 0:
        errors.append("signed receipt does not record a successful gate exit")
    counts = (
        evidence.checks_collected,
        evidence.checks_passed,
        evidence.checks_skipped,
        evidence.checks_failed,
        evidence.deselected_count,
    )
    if any(type(count) is not int or count < 0 for count in counts):
        errors.append("signed receipt check counts are invalid")
    else:
        if evidence.checks_collected == 0:
            errors.append("signed receipt collected zero checks")
        if evidence.checks_failed:
            errors.append(f"signed receipt records {evidence.checks_failed} failed checks")
        skip_rejection = _skipped_checks_rejection(
            evidence.checks_skipped,
            subject="signed receipt",
        )
        if skip_rejection:
            errors.append(skip_rejection)
        if evidence.deselected_count:
            errors.append(f"signed receipt records {evidence.deselected_count} deselected checks")
        if evidence.checks_passed + evidence.checks_skipped != evidence.checks_collected:
            errors.append("signed receipt did not pass every collected check")
    if not evidence.tool or not evidence.tool_version:
        errors.append("signed receipt does not identify its verifier")
    elif evidence.tool != MERGE_GATE_TOOL:
        errors.append(
            f"signed receipt was produced by {evidence.tool!r}; the merge gate accepts "
            f"only {MERGE_GATE_TOOL!r} evidence"
        )
    if len(evidence.nonce) < 16:
        errors.append("signed receipt has no usable nonce")
    if not evidence.interpreter or not Path(evidence.interpreter).is_absolute():
        errors.append("signed receipt interpreter path is invalid")
    if not re.match(r"\A[0-9a-f]{64}\Z", evidence.node_inventory_digest):
        errors.append("signed receipt node inventory digest is invalid")

    clock = now if now is not None else datetime.now(UTC)
    started = _parse_timestamp(evidence.started_at)
    finished = _parse_timestamp(evidence.finished_at)
    if started is None or finished is None:
        errors.append("signed receipt timestamps are invalid")
    elif finished < started:
        errors.append("signed receipt finished before it started")
    else:
        # Same freshness rules as ordinary gate-evidence validation: a receipt
        # dated in the future or older than the evidence window is not evidence.
        age = (clock.astimezone(UTC) - finished).total_seconds()
        if age > MAX_EVIDENCE_AGE_SECONDS:
            errors.append("signed receipt is stale")
        elif age < -60:
            errors.append("signed receipt is dated in the future")

    return errors


def verify_candidate_receipt(
    receipt_path: Path | str,
    *,
    evidence_root: Path | str,
    candidate_sha: str,
    merge_base_sha: str,
    now: datetime | None = None,
) -> GateEvidence:
    """Load, authenticate, and bind one receipt to the exact merge candidate."""
    store = GateEvidenceStore(evidence_root, create_key=False)
    evidence = store.load_receipt(receipt_path)
    if evidence is None:
        raise GateEvidenceRefusal(f"no signed receipt at {receipt_path}")
    rejections = candidate_receipt_rejections(
        evidence,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        now=now,
    )
    if rejections:
        raise GateEvidenceRefusal("; ".join(rejections))
    return evidence


def workspace_digest_for(path: Path | str) -> str:
    """Stable digest of the absolute workspace a gate executed in."""
    return digest(str(Path(path).expanduser().resolve()))


def mint_candidate_receipt(
    *,
    candidate_sha: str,
    merge_base_sha: str,
    evidence_root: Path | str,
    workspace: Path | str | None = None,
    command: str = (
        "pytest tests/scheduler/test_gate_evidence.py"
        "::test_normalize_gate_command_is_stable_across_formatting"
    ),
) -> Path:
    """Produce a signed merge-candidate receipt via the trusted runner.

    Does NOT accept a pre-baked passed=true verdict. Runs a real allowlisted
    verifier in ``workspace`` (must be clean and tip-bound to ``candidate_sha``),
    signs the evidence with the real store key, and publishes the record under
    ``records/merge-gate/<candidate-sha>.json`` for ``merge-gate.sh``.
    """
    from omniagentos.scheduler.gate_runner import (  # local import avoids cycle
        GateRunRequest,
        PytestGateRunner,
        default_gate_workspace,
    )

    if not _GIT_SHA_RE.match(candidate_sha or ""):
        raise GateEvidenceRefusal("candidate SHA is invalid")
    if not _GIT_SHA_RE.match(merge_base_sha or ""):
        raise GateEvidenceRefusal("merge-base SHA is invalid")

    root = Path(evidence_root).expanduser().resolve()
    store = GateEvidenceStore(root, create_key=True)
    if workspace is not None:
        ws = Path(workspace).expanduser().resolve()
    else:
        resolved = default_gate_workspace()
        if resolved is None:
            raise GateEvidenceRefusal(
                "no gate workspace: set OMNIAGENTOS_GATE_WORKSPACE or run inside the repo"
            )
        ws = resolved
    if not ws.is_dir():
        raise GateEvidenceRefusal(f"gate workspace is not a directory: {ws}")

    request = GateRunRequest(
        routine_id=MERGE_GATE_ROUTINE_ID,
        run_id=candidate_sha,
        iteration=1,
        gate_type=MERGE_GATE_TYPE,
        gate_config={"command": command, "expected_exit_code": 0},
        workspace=ws,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
    )
    PytestGateRunner(store).run(request)
    record_path = store._record_path(MERGE_GATE_ROUTINE_ID, candidate_sha)
    published = root / "records" / "merge-gate" / f"{candidate_sha}.json"
    published.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    published.write_text(record_path.read_text(encoding="utf-8"), encoding="utf-8")
    return published


def _counterfeit_report_match(text: str | None) -> re.Match[str] | None:
    """The corpus's authoritative verdict line in ``text`` — the LAST one.

    Everything a candidate controls in the harness's output (entry ids,
    rationales, pytest excerpts) is printed by ``format_report`` BEFORE the
    summary line, so the last match is the one a candidate cannot get in front
    of. ``grep -oE ... | tail -1`` in ``merge-gate.sh`` meant exactly this;
    this is now the only implementation of it.
    """
    if not isinstance(text, str):
        return None
    last: re.Match[str] | None = None
    for match in _COUNTERFEIT_REPORT_RE.finditer(text):
        last = match
    return last


def _counterfeit_verdict_rejections(*, exit_code: object, summary: str | None) -> list[str]:
    """THE definition of "the counterfeit corpus ran green". ONE copy, three callers.

    Callers: the live gate (``merge-gate.sh`` via the ``judge-counterfeit``
    CLI), the receipt MINTER (:func:`record_step_receipt`) and the receipt
    VERIFIER (:func:`_step_receipt_rejections`). They used to hold two
    definitions — the shell refused ``other>0`` while the receipt path looked
    only at ``survived`` — so a receipt minted under the weaker rule replayed a
    refusable run as a pass for its whole 24h freshness window. A second
    definition that can drift IS the defect; this repo has already been
    auto-paused four times by settled-definition divergence
    (``routines.py`` "ADVERSE-collapse").

    Two independent signals, and BOTH must say green:

    * the harness's EXIT CODE — primary, produced by the instrument itself
      (``tests/counterfeits/harness.py``: 1 on any entry not caught, 2 on a
      corpus that would not load);
    * the report LINE — a human-readable cross-check, and text emitted by the
      tree under judgement.

    A disagreement is itself a refusal: neither signal may silently overrule
    the other, because whichever one you let win is the one an attacker (or a
    format change) only has to defeat once.
    """
    errors: list[str] = []
    code_green = type(exit_code) is int and exit_code == 0
    if not code_green:
        errors.append(f"counterfeit harness exited {exit_code!r}, not 0")

    summary_errors: list[str] = []
    match = _counterfeit_report_match(summary)
    if match is None:
        # An instrument that did not run is NOT a pass — a voter once reported
        # 558 clean results while every call was failing. This also covers a
        # crash before the summary, an empty capture, and any change to the
        # harness's report format (the regex requires all four fields).
        summary_errors.append("counterfeit corpus produced NO verdict line (did not run)")
    else:
        total = int(match.group("total"))
        caught = int(match.group("caught"))
        survived = int(match.group("survived"))
        # Absent field == the four-field line == no platform skips. Read through
        # `or 0` rather than defaulted in the pattern so a receipt minted before
        # the field existed keeps arithmetic that adds up.
        #
        # A platform skip is GREEN, unlike ``other``, and that is the harness's
        # ruling, not a new one taken here: ``EntryResult.ok`` (harness.py:441)
        # counts "caught" and "skipped_platform" alike because the pin is
        # explicit and the entry is verified on a pinned-platform host. Its exit
        # code says 0 for exactly that reason, so refusing here would be the
        # two-definitions defect this function exists to prevent. What skips may
        # NOT do is vanish: they are added into the total below, so a corpus
        # cannot quietly skip its way to a shorter run than it claims.
        skipped_platform = int(match.group("skipped_platform") or 0)
        other = int(match.group("other"))
        if survived:
            summary_errors.append(f"{survived} counterfeit(s) SURVIVED")
        if other:
            # A counterfeit that ERRORED never ran: its patch no longer
            # applies, or its suite failed to collect. survived=0 says nothing
            # about it, so coverage that silently went dead must refuse the
            # merge — or the corpus degrades one rename at a time and still
            # reports PASS, which is exactly what happened on 2026-08-01 when a
            # reachability fix renamed judged_runs -> _judged_runs and orphaned
            # an entry's anchor.
            summary_errors.append(f"{other} counterfeit(s) errored — dead coverage, not a pass")
        if total <= 0:
            summary_errors.append("counterfeit corpus is empty (total=0)")
        elif caught + survived + skipped_platform + other != total:
            summary_errors.append(f"counterfeit verdict line does not add up: {match.group(0)}")
    errors.extend(summary_errors)

    if code_green != (not summary_errors):
        observed = match.group(0) if match is not None else "no verdict line"
        errors.append(
            f"harness exit code and report line DISAGREE (exit {exit_code!r} vs {observed!r}) "
            "— refusing; neither signal may silently overrule the other"
        )
    return errors


def _first_diagnostic_line(text: str) -> str:
    """One line of WHY, for the operator reading a one-line gate report.

    A HEADER IS NOT A DIAGNOSTIC. The harness's control refusal prints
    ``COUNTERFEIT GATE CONTROL FAILED:`` and puts the reason — and the pytest
    excerpt naming the offending nodes — on the lines BELOW it, so matching one
    line handed the operator a colon and nothing else. The gate's ``$SCRATCH``
    (holding ``counterfeit.out``) is destroyed by ``merge-gate.sh``'s EXIT trap,
    which made that bare header the ONLY surviving record of ~8 consecutive
    in-gate refusals on 2026-08-04/05 and cost a full diagnostic cycle to
    recover by hand.

    So: a matched line that ends in a colon carries the first ``FAILED <node
    id>`` the excerpt names, then the first non-empty line that follows the
    header. Lines that already state their reason are returned unchanged.

    ORDER IS LOAD-BEARING, because the result is capped. The node id goes
    BEFORE the free-text body: the reason line is bounded only by whatever the
    harness printed, so appending the node id last let a long reason push the
    single actionable token past ``_DIAGNOSTIC_MAX_CHARS`` and truncate away the
    one thing an operator can act on — the same failure mode, one layer in.
    Truncation must always eat prose first.
    """
    match = _HARNESS_DIAGNOSTIC_RE.search(text)
    if match is None:
        return ""
    parts = [match.group(0).strip()]
    if parts[0].endswith(":"):
        node = _HARNESS_FAILED_NODE_RE.search(text)
        if node is not None:
            parts.append(f"FAILED {node.group(1)}")
        for line in text[match.end() :].splitlines():
            line = line.strip()
            if line:
                parts.append(line)
                break
    return " ".join(parts)[:_DIAGNOSTIC_MAX_CHARS]


def _step_verdict_rejections(step: str, *, exit_code: object, summary: str | None) -> list[str]:
    """Verdict rejections for one merge-gate step's recorded result.

    ``step`` is the caller's EXPECTED step id, never a value read back out of
    the artefact being judged. The counterfeit rule also applies to any other
    step whose summary carries — or merely resembles — a corpus report line, so
    the rule cannot be dodged by filing the corpus result under a different step
    name, nor by a report format that no longer states what it is being asked.
    """
    if step == COUNTERFEIT_STEP or (
        isinstance(summary, str) and _COUNTERFEIT_HINT_RE.search(summary) is not None
    ):
        return _counterfeit_verdict_rejections(exit_code=exit_code, summary=summary)
    return []


def _step_receipt_rejections(
    receipt: GateStepReceipt,
    *,
    step: str,
    candidate_sha: str,
    merge_base_sha: str,
    merge_tree_sha: str,
    command: str,
    now: datetime | None = None,
) -> list[str]:
    """Every reason an authenticated step receipt must not skip a re-run.

    The caller's expected identities are the authority — never the values
    embedded in the receipt itself (same caller-binding rule as
    :func:`candidate_receipt_rejections`).
    """
    errors: list[str] = []
    if step not in MERGE_GATE_STEP_NAMES:
        errors.append(f"expected step is not a known merge-gate step: {step!r}")
    if not _GIT_SHA_RE.match(candidate_sha or ""):
        errors.append("expected candidate SHA is invalid")
    if not _GIT_SHA_RE.match(merge_base_sha or ""):
        errors.append("expected merge-base SHA is invalid")
    if not _GIT_SHA_RE.match(merge_tree_sha or ""):
        errors.append("expected merge-tree SHA is invalid")
    try:
        expected_command = normalize_gate_command(command)
    except GateEvidenceError:
        return [*errors, "expected step command is unusable"]

    if receipt.schema != STEP_SCHEMA:
        errors.append("step receipt schema is invalid")
    if receipt.step != step:
        errors.append("step receipt was recorded for a different step")
    if not hmac.compare_digest(receipt.candidate_sha, candidate_sha):
        errors.append("step receipt is bound to a different candidate SHA")
    if not hmac.compare_digest(receipt.merge_base_sha, merge_base_sha):
        errors.append("step receipt is bound to a different merge-base SHA")
    if not hmac.compare_digest(receipt.merge_tree_sha, merge_tree_sha):
        errors.append("step receipt is bound to a different trial-merge tree")
    if receipt.command != expected_command:
        errors.append("step receipt command does not match this gate step")
    if not _HEX_DIGEST_RE.match(receipt.workspace_digest):
        errors.append("step receipt workspace digest is invalid")
    if not _HEX_DIGEST_RE.match(receipt.output_digest):
        errors.append("step receipt output digest is invalid")
    if type(receipt.exit_code) is not int or receipt.exit_code != 0:
        errors.append("step receipt does not record a green step exit")
    if not isinstance(receipt.summary, str) or not receipt.summary.strip():
        errors.append("step receipt has no verdict summary")
    else:
        # The SAME rule the live gate applies, from the SAME function: a
        # receipt must never certify a run the live gate would refuse.
        errors.extend(
            _step_verdict_rejections(step, exit_code=receipt.exit_code, summary=receipt.summary)
        )
    if len(receipt.nonce) < 16:
        errors.append("step receipt has no usable nonce")

    clock = now if now is not None else datetime.now(UTC)
    started = _parse_timestamp(receipt.started_at)
    finished = _parse_timestamp(receipt.finished_at)
    if started is None or finished is None:
        errors.append("step receipt timestamps are invalid")
    elif finished < started:
        errors.append("step receipt finished before it started")
    else:
        age = (clock.astimezone(UTC) - finished).total_seconds()
        if age > MAX_EVIDENCE_AGE_SECONDS:
            errors.append("step receipt is stale")
        elif age < -60:
            errors.append("step receipt is dated in the future")

    return errors


def verify_step_receipt(
    *,
    step: str,
    candidate_sha: str,
    merge_base_sha: str,
    merge_tree_sha: str,
    command: str,
    evidence_root: Path | str,
    now: datetime | None = None,
) -> GateStepReceipt:
    """Load and bind one step receipt to the exact step the gate is about to run.

    Success means the SAME candidate SHA already ran this step green on the
    SAME trial-merge tree with the SAME command, recently enough to trust —
    so the gate may skip re-running it.  Every other outcome raises, and the
    gate runs the step for real.
    """
    store = GateEvidenceStore(evidence_root, create_key=False)
    receipt = store.load_step(step, candidate_sha)
    if receipt is None:
        raise GateEvidenceRefusal(
            f"no step receipt for {step} at candidate {(candidate_sha or '?')[:12]}"
        )
    rejections = _step_receipt_rejections(
        receipt,
        step=step,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        merge_tree_sha=merge_tree_sha,
        command=command,
        now=now,
    )
    if rejections:
        raise GateEvidenceRefusal("; ".join(rejections))
    return receipt


def record_step_receipt(
    *,
    step: str,
    candidate_sha: str,
    merge_base_sha: str,
    merge_tree_sha: str,
    command: str,
    workspace: Path | str,
    output_path: Path | str,
    exit_code: int,
    summary: str,
    evidence_root: Path | str,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> Path:
    """Sign and publish a per-step receipt for one green merge-gate step.

    Does NOT accept a pre-baked skip decision: the caller is the merge gate
    that just ran the step, and this function refuses anything that is not a
    green run with its real output artifact in hand — non-zero exit, an empty
    or missing artifact, a summary line that does not appear in the artifact,
    or a counterfeit report that :func:`_counterfeit_verdict_rejections`
    refuses (survivors, errored entries, no verdict line at all, or a report
    line that disagrees with the exit code).  The output digest is always
    computed here from the artifact bytes, never accepted as a string, so the
    receipt is content-addressed to what actually happened (§2d-2).
    """
    if step not in MERGE_GATE_STEP_NAMES:
        raise GateEvidenceRefusal(f"unknown merge-gate step: {step!r}")
    for label, value in (
        ("candidate", candidate_sha),
        ("merge-base", merge_base_sha),
        ("merge-tree", merge_tree_sha),
    ):
        if not _GIT_SHA_RE.match(value or ""):
            raise GateEvidenceRefusal(f"{label} SHA is invalid")
    if type(exit_code) is not int or exit_code != 0:
        raise GateEvidenceRefusal(
            f"refusing to record a step receipt for a non-green run (exit {exit_code!r})"
        )
    if not isinstance(summary, str) or not summary.strip():
        raise GateEvidenceRefusal("step receipt requires the step's verdict summary line")
    verdict_errors = _step_verdict_rejections(step, exit_code=exit_code, summary=summary)
    if verdict_errors:
        raise GateEvidenceRefusal(
            "refusing to record a step receipt for a step that did not run green: "
            + "; ".join(verdict_errors)
        )

    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise GateEvidenceRefusal(f"step workspace is not a directory: {ws}")

    artifact = Path(output_path).expanduser()
    try:
        output_bytes = artifact.read_bytes()
    except OSError as exc:
        raise GateEvidenceRefusal(f"step output artifact is unreadable: {exc}") from exc
    if not output_bytes.strip():
        raise GateEvidenceRefusal(f"step output artifact is empty: {artifact}")
    if summary.strip() not in output_bytes.decode("utf-8", errors="replace"):
        raise GateEvidenceRefusal("step summary does not appear in the step output artifact")

    normalized = normalize_gate_command(command)
    finished = finished_at if finished_at is not None else utc_now_iso()
    started = started_at if started_at is not None else finished
    for label, value in (("started-at", started), ("finished-at", finished)):
        if _parse_timestamp(value) is None:
            raise GateEvidenceRefusal(f"step receipt {label} timestamp is invalid: {value!r}")

    store = GateEvidenceStore(Path(evidence_root).expanduser().resolve(), create_key=True)
    signed = store.record_step(
        GateStepReceipt(
            schema=STEP_SCHEMA,
            step=step,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
            merge_tree_sha=merge_tree_sha,
            command=normalized,
            workspace_digest=workspace_digest_for(ws),
            output_digest=digest(output_bytes),
            exit_code=exit_code,
            summary=summary.strip(),
            started_at=started,
            finished_at=finished,
            nonce=secrets.token_hex(16),
        )
    )
    return store._step_receipt_path(signed.step, signed.candidate_sha)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="OmniAgentOS gate evidence producer/verifier")
    subparsers = parser.add_subparsers(dest="action", required=True)
    candidate = subparsers.add_parser(
        "verify-candidate",
        help="verify a merge-candidate receipt against exact git identities",
    )
    candidate.add_argument("--receipt", required=True)
    candidate.add_argument("--evidence-root", required=True)
    candidate.add_argument("--candidate-sha", required=True)
    candidate.add_argument("--merge-base-sha", required=True)

    mint = subparsers.add_parser(
        "mint-candidate",
        help="run a trusted verifier and sign a merge-candidate receipt (no pre-baked pass)",
    )
    mint.add_argument("--candidate-sha", required=True)
    mint.add_argument("--merge-base-sha", required=True)
    mint.add_argument("--evidence-root", required=True)
    mint.add_argument(
        "--workspace",
        default=None,
        help="git workspace tip-bound to candidate-sha (default: gate workspace / repo root)",
    )
    mint.add_argument(
        "--command",
        default="pytest tests/scheduler/test_gate_evidence.py::test_normalize_gate_command_is_stable_across_formatting",
        help="allowlisted verifier command; must collect>0 and pass",
    )

    verify_step = subparsers.add_parser(
        "verify-step",
        help="verify a per-step merge-gate receipt; exit 0 means the step may be skipped",
    )
    verify_step.add_argument("--evidence-root", required=True)
    verify_step.add_argument("--step", required=True)
    verify_step.add_argument("--candidate-sha", required=True)
    verify_step.add_argument("--merge-base-sha", required=True)
    verify_step.add_argument("--merge-tree-sha", required=True)
    verify_step.add_argument("--command", required=True)

    record_step = subparsers.add_parser(
        "record-step",
        help="sign a per-step merge-gate receipt for one green step (requires the real output artifact)",
    )
    record_step.add_argument("--evidence-root", required=True)
    record_step.add_argument("--step", required=True)
    record_step.add_argument("--candidate-sha", required=True)
    record_step.add_argument("--merge-base-sha", required=True)
    record_step.add_argument("--merge-tree-sha", required=True)
    record_step.add_argument("--command", required=True)
    record_step.add_argument("--workspace", required=True)
    record_step.add_argument("--output", required=True, help="file capturing the step's output")
    record_step.add_argument("--exit-code", required=True, type=int)
    record_step.add_argument("--summary", required=True)
    record_step.add_argument(
        "--started-at",
        default=None,
        help="UTC start of the step run (YYYY-MM-DDTHH:MM:SSZ); default: record time",
    )

    judge = subparsers.add_parser(
        "judge-counterfeit",
        help=(
            "judge one counterfeit-corpus run from its measured exit code AND its "
            "report line; exit 0 prints the verdict line, exit 1 prints the refusal"
        ),
    )
    judge.add_argument(
        "--exit-code",
        required=True,
        type=int,
        help="the exit status the gate MEASURED for the harness process",
    )
    judge.add_argument("--output", required=True, help="file capturing the harness's output")
    args = parser.parse_args(argv)

    try:
        if args.action == "verify-candidate":
            evidence = verify_candidate_receipt(
                args.receipt,
                evidence_root=args.evidence_root,
                candidate_sha=args.candidate_sha,
                merge_base_sha=args.merge_base_sha,
            )
            print(
                f"verified {evidence.tool} receipt for candidate {evidence.candidate_sha[:12]} "
                f"at merge-base {evidence.merge_base_sha[:12]}"
            )
            return 0
        if args.action == "mint-candidate":
            path = mint_candidate_receipt(
                candidate_sha=args.candidate_sha,
                merge_base_sha=args.merge_base_sha,
                evidence_root=args.evidence_root,
                workspace=args.workspace,
                command=args.command,
            )
            print(f"minted {path}")
            return 0
        if args.action == "verify-step":
            receipt = verify_step_receipt(
                step=args.step,
                candidate_sha=args.candidate_sha,
                merge_base_sha=args.merge_base_sha,
                merge_tree_sha=args.merge_tree_sha,
                command=args.command,
                evidence_root=args.evidence_root,
            )
            print(f"receipt {receipt.signature[:12]} reused — {receipt.summary}")
            return 0
        if args.action == "record-step":
            path = record_step_receipt(
                step=args.step,
                candidate_sha=args.candidate_sha,
                merge_base_sha=args.merge_base_sha,
                merge_tree_sha=args.merge_tree_sha,
                command=args.command,
                workspace=args.workspace,
                output_path=args.output,
                exit_code=args.exit_code,
                summary=args.summary,
                evidence_root=args.evidence_root,
                started_at=args.started_at,
            )
            print(f"recorded {path}")
            return 0
        if args.action == "judge-counterfeit":
            try:
                text = Path(args.output).expanduser().read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                # A capture the gate cannot read is an instrument that did not
                # report. Never a pass.
                print(f"REFUSED: counterfeit output is unreadable: {exc}", file=sys.stderr)
                return 1
            rejections = _counterfeit_verdict_rejections(exit_code=args.exit_code, summary=text)
            verdict = _counterfeit_report_match(text)
            if rejections or verdict is None:
                reason = "; ".join(rejections) or "counterfeit corpus produced no verdict line"
                diagnostic = _first_diagnostic_line(text)
                if diagnostic:
                    reason = f"{reason} — first diagnostic: {diagnostic}"
                print(f"REFUSED: {reason}", file=sys.stderr)
                return 1
            # stdout is the verdict line VERBATIM: the gate prints it in the
            # human-readable report and stores it as the step receipt summary,
            # which record_step_receipt re-checks against the artifact bytes.
            print(verdict.group(0))
            return 0
    except GateEvidenceError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"unknown action {args.action}", file=sys.stderr)
    return 2


__all__ = [
    "COUNTERFEIT_STEP",
    "MAX_EVIDENCE_AGE_SECONDS",
    "MERGE_GATE_ROUTINE_ID",
    "MERGE_GATE_STEP_NAMES",
    "MERGE_GATE_STEPS_DIR",
    "MERGE_GATE_TYPE",
    "SCHEMA",
    "SCHEMA_V1",
    "SCHEMA_V2",
    "STEP_SCHEMA",
    "ClaimToken",
    "GateEvidence",
    "GateEvidenceError",
    "GateEvidenceExists",
    "GateEvidenceRefusal",
    "GateExecutionInfraError",
    "GateEvidenceStore",
    "GateStepReceipt",
    "GateWorkspaceUnusable",
    "binding_digest",
    "candidate_receipt_rejections",
    "evidence_rejections",
    "gate_evidence_dir",
    "load_skip_allowlist",
    "normalize_gate_command",
    "mint_candidate_receipt",
    "record_step_receipt",
    "verify_candidate_receipt",
    "verify_step_receipt",
    "workspace_digest_for",
]


if __name__ == "__main__":
    raise SystemExit(_main())
