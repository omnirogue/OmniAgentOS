"""Coordinator-only, exact-SHA promotion finalizer.

Report mode is the default and performs no filesystem or Git writes.  Enforce
mode is deliberately difficult to enter: it requires raw YAML ``enforce``
(environment overrides are refused), two signed clean report cycles separated
by the configured pause, an Ed25519 operator authorization, complete signed
authorship provenance, and two structured outside-family approvals.

All candidate, code-merge, and architecture commits are built and proved
off-ref.  The authoritative ``main`` ref changes once, with an expected-parent
``update-ref`` CAS, only after every fallible gate and durable publication
prerequisite has completed.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from omniagentos.archdocs.staleness import is_stale
from omniagentos.formation.lineage import UnknownModelLineageError, lineage_for_model
from omniagentos.integration.config import IntegrationConfig, load_integration_config
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.scheduler.gate_evidence import (
    SCHEMA,
    GateEvidence,
    GateEvidenceError,
    GateEvidenceStore,
    binding_digest,
    normalize_gate_command,
    verify_candidate_receipt,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_runner import (
    GateRunRequest,
    PytestGateRunner,
    parse_gate_command,
    produce_gate_evidence,
)

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_REF_RE = re.compile(r"\Arefs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_TRAILER_RE = re.compile(r"(?im)^\s*([A-Za-z][A-Za-z-]*)\s*:\s*(.*?)\s*$")
_ZERO_SHA = "0" * 40
_REPORT_MAX_AGE_SECONDS = 24 * 60 * 60
_ORACLE_PATHS = (
    "ARCHI.md",
    "ARCHI.json",
    "docs/architecture/system-map.md",
    "docs/architecture/system-map.mmd",
)
_AUTHORSHIP_ROLES = frozenset({"coder", "coder_rework", "coder_recovery", "escalation"})
_REPORT_SCHEMA = "omniagentos.promotion-report-cycle.v1"
_AUTHORSHIP_SCHEMA = "omniagentos.promotion-authorship.v1"
_OPERATOR_SCHEMA = "omniagentos.promotion-operator-authorization.v1"
_FINALIZER_SCHEMA = "omniagentos.promotion-finalizer.v2"
_REVIEW_DECISIONS = frozenset({"APPROVE", "APPROVE WITH NOTES", "REJECT"})


class PromotionRefusal(RuntimeError):
    """The requested operation cannot be proved safe before publication."""


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    repo: Path
    expected_main_sha: str
    integration_sha: str
    authorship_manifest: Path
    aggregate_reviews: tuple[Path, ...]
    config_path: Path
    evidence_root: Path
    lock_path: Path
    candidate_ref: str
    report_receipts: tuple[Path, ...] = ()
    operator_authorization: Path | None = None
    enforce: bool = False


@dataclass(frozen=True, slots=True)
class Authorization:
    authorship_families: tuple[str, ...]
    reviewer_families: tuple[str, ...]
    authorship_sha256: str
    review_sha256: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Subject:
    digest: str
    config_sha256: str
    gate_targets: tuple[str, ...]
    authorization: Authorization


@dataclass(frozen=True, slots=True)
class ProducedReceipt:
    evidence: GateEvidence
    request_contract_digest: str
    produced_new: bool


@dataclass(frozen=True, slots=True)
class PromotionResult:
    schema: str
    status: Literal[
        "report",
        "prepared",
        "published",
        "finalized",
        "published_recovery_required",
    ]
    main_sha: str
    integration_sha: str
    merge_base_sha: str
    candidate_ref: str
    subject_digest: str
    config_sha256: str
    authorship_sha256: str
    review_sha256: tuple[str, ...]
    authorship_families: tuple[str, ...]
    reviewer_families: tuple[str, ...]
    gate_targets: tuple[str, ...]
    report_receipt: dict[str, Any] | None = None
    report_cycle_ids: tuple[str, ...] = ()
    operator_authorization_sha256: str = ""
    promotion_sha: str = ""
    receipt_path: str = ""
    receipt_sha256: str = ""
    merge_gate_output_path: str = ""
    merge_gate_output_sha256: str = ""
    code_main_sha: str = ""
    final_main_sha: str = ""
    observed_main_sha: str = ""
    terminal_parents: tuple[str, ...] = ()
    terminal_changed_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)


class ReceiptProducer(Protocol):
    def __call__(
        self,
        request: GateRunRequest,
        store: GateEvidenceStore,
        request_contract_digest: str,
    ) -> ProducedReceipt: ...


class MergeGateRunner(Protocol):
    def __call__(self, repo: Path, candidate_ref: str, receipt_path: Path) -> str: ...


class ArchdocsRunner(Protocol):
    def __call__(self, worktree: Path) -> str: ...


Checkpoint = Callable[[str, PromotionRequest, str, str], None]
Clock = Callable[[], datetime]


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise PromotionRefusal(f"command failed ({shlex.join(argv)}): {detail}")
    return proc


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return _run(["git", *args], cwd=repo, check=check).stdout.strip()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise PromotionRefusal(f"{label} timestamp is missing")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise PromotionRefusal(f"{label} timestamp is invalid") from None


def _require_sha(value: str, label: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise PromotionRefusal(f"{label} must be an exact lowercase 40-character commit SHA")
    return value


def _require_candidate_ref(value: str, protected: tuple[str, ...]) -> str:
    if not _REF_RE.fullmatch(value) or ".." in value or value.endswith(("/", ".")):
        raise PromotionRefusal("candidate_ref must be a safe full refs/heads/* name")
    protected_refs = {f"refs/heads/{branch}" for branch in protected}
    if value in protected_refs:
        raise PromotionRefusal(f"candidate_ref is protected: {value}")
    return value


def _resolve_commit(repo: Path, rev: str, label: str) -> str:
    resolved = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}", check=False)
    if not _SHA_RE.fullmatch(resolved):
        raise PromotionRefusal(f"{label} does not resolve to a commit: {rev}")
    return resolved


def _ref_value(repo: Path, ref: str) -> str | None:
    value = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    return value if _SHA_RE.fullmatch(value) else None


def _tree_clean(repo: Path) -> bool:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise PromotionRefusal("cannot inspect worktree cleanliness")
    return not proc.stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        _run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


def _exact_file(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PromotionRefusal(f"{label} is unavailable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PromotionRefusal(f"{label} must be a regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PromotionRefusal(f"{label} is unreadable: {exc}") from exc


def _load_trust_key(evidence_root: Path) -> bytes:
    key_path = evidence_root / "signing.key"
    raw = _exact_file(key_path, "pre-existing gate-evidence signing key")
    info = key_path.stat(follow_symlinks=False)
    if len(raw) < 32 or stat.S_IMODE(info.st_mode) & 0o077:
        raise PromotionRefusal(
            "pre-existing gate-evidence signing key is invalid or too permissive"
        )
    return raw


def _raw_promotion_mode(config_path: Path) -> str:
    raw = yaml.safe_load(_exact_file(config_path, "integration config")) or {}
    if not isinstance(raw, dict):
        raise PromotionRefusal("integration config is not a mapping")
    body = raw.get("integration", raw)
    if not isinstance(body, dict):
        raise PromotionRefusal("integration config body is not a mapping")
    promotion = body.get("promotion", {})
    if not isinstance(promotion, dict):
        raise PromotionRefusal("integration promotion config is not a mapping")
    return str(promotion.get("mode") or "report").strip().lower()


def _validate_authorship(
    path: Path,
    *,
    candidate_sha: str,
    trust_key: bytes,
) -> tuple[tuple[str, ...], str]:
    raw = _exact_file(path, "authorship manifest")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise PromotionRefusal("authorship manifest is not valid JSON") from None
    if not isinstance(payload, dict):
        raise PromotionRefusal("authorship manifest is not an object")
    signature = payload.pop("signature", None)
    if not isinstance(signature, str) or not hmac.compare_digest(
        hmac.new(trust_key, _canonical_json(payload), "sha256").hexdigest(),
        signature,
    ):
        raise PromotionRefusal("authorship manifest signature is invalid")
    if payload.get("schema") != _AUTHORSHIP_SCHEMA or payload.get("candidate_sha") != candidate_sha:
        raise PromotionRefusal("authorship manifest is bound to a different subject")
    records = payload.get("authorships")
    if not isinstance(records, list) or not records:
        raise PromotionRefusal("authorship manifest has no authorship records")
    families: set[str] = set()
    seen_records: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise PromotionRefusal("authorship record is not an object")
        role = str(record.get("role") or "").strip()
        model = str(record.get("model") or "").strip()
        family = str(record.get("family") or "").strip().lower()
        if role not in _AUTHORSHIP_ROLES or (role, model) in seen_records:
            raise PromotionRefusal(
                f"authorship record is invalid or duplicated: {role!r}/{model!r}"
            )
        try:
            resolved = lineage_for_model(model)
        except UnknownModelLineageError as exc:
            raise PromotionRefusal(str(exc)) from exc
        if family != resolved:
            raise PromotionRefusal(f"authorship family does not match model {model!r}")
        seen_records.add((role, model))
        families.add(family)
    return tuple(sorted(families)), _sha256_bytes(raw)


def _review_trailers(path: Path) -> tuple[dict[str, str], bytes]:
    raw = _exact_file(path, "review artifact")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PromotionRefusal("review artifact is not UTF-8") from None
    trailers: dict[str, str] = {}
    for key, value in _TRAILER_RE.findall(text):
        normalized = key.strip().lower()
        if normalized in trailers:
            raise PromotionRefusal(f"review artifact duplicates trailer {key}")
        trailers[normalized] = value.strip()
    return trailers, raw


def _validate_reviews(
    paths: tuple[Path, ...],
    *,
    candidate_sha: str,
    authorship_families: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if len(paths) < 2:
        raise PromotionRefusal("sensitive promotion requires two structured review artifacts")
    approvals: dict[str, str] = {}
    hashes: list[str] = []
    authorship_set = set(authorship_families)
    for path in paths:
        trailers, raw = _review_trailers(path)
        hashes.append(_sha256_bytes(raw))
        required = {
            "verdict",
            "reviewer-model",
            "reviewer-family",
            "candidate-sha",
            "authorship-families",
        }
        if not required.issubset(trailers):
            raise PromotionRefusal("review artifact lacks required structured trailers")
        decision = trailers["verdict"].upper()
        if decision not in _REVIEW_DECISIONS:
            raise PromotionRefusal(f"review decision is invalid: {decision!r}")
        if trailers["candidate-sha"] != candidate_sha:
            raise PromotionRefusal("review artifact is bound to a different candidate SHA")
        declared_authors = tuple(
            sorted(
                item.strip().lower()
                for item in trailers["authorship-families"].split(",")
                if item.strip()
            )
        )
        if declared_authors != authorship_families:
            raise PromotionRefusal(
                "review artifact does not bind the complete authorship-family set"
            )
        model = trailers["reviewer-model"]
        family = trailers["reviewer-family"].lower()
        try:
            resolved = lineage_for_model(model)
        except UnknownModelLineageError as exc:
            raise PromotionRefusal(str(exc)) from exc
        if family != resolved:
            raise PromotionRefusal("reviewer-family trailer does not match reviewer model")
        if decision == "REJECT":
            raise PromotionRefusal(f"binding review rejects candidate ({model})")
        if family in authorship_set:
            raise PromotionRefusal(f"same-family approval is non-authorizing: {family}")
        approvals.setdefault(family, model)
    if len(approvals) < 2:
        raise PromotionRefusal(
            "sensitive promotion requires two distinct outside reviewer families"
        )
    return tuple(sorted(approvals)), tuple(hashes)


def _subject(
    request: PromotionRequest,
    config: IntegrationConfig,
    trust_key: bytes,
) -> Subject:
    authorship_families, authorship_hash = _validate_authorship(
        request.authorship_manifest,
        candidate_sha=request.integration_sha,
        trust_key=trust_key,
    )
    reviewer_families, review_hashes = _validate_reviews(
        request.aggregate_reviews,
        candidate_sha=request.integration_sha,
        authorship_families=authorship_families,
    )
    config_hash = _sha256_file(request.config_path)
    body = {
        "main_sha": request.expected_main_sha,
        "integration_sha": request.integration_sha,
        "config_sha256": config_hash,
        "gate_targets": list(config.gate_targets),
        "authorship_sha256": authorship_hash,
        "review_sha256": list(review_hashes),
        "authorship_families": list(authorship_families),
        "reviewer_families": list(reviewer_families),
    }
    return Subject(
        digest=_sha256_bytes(_canonical_json(body)),
        config_sha256=config_hash,
        gate_targets=config.gate_targets,
        authorization=Authorization(
            authorship_families=authorship_families,
            reviewer_families=reviewer_families,
            authorship_sha256=authorship_hash,
            review_sha256=review_hashes,
        ),
    )


def _make_report_receipt(
    request: PromotionRequest,
    subject: Subject,
    trust_key: bytes,
    now: datetime,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": _REPORT_SCHEMA,
        "status": "report",
        "subject_digest": subject.digest,
        "main_sha": request.expected_main_sha,
        "integration_sha": request.integration_sha,
        "config_sha256": subject.config_sha256,
        "gate_targets": list(subject.gate_targets),
        "authorship_sha256": subject.authorization.authorship_sha256,
        "review_sha256": list(subject.authorization.review_sha256),
        "observed_at": _utc_text(now),
        "nonce": secrets.token_hex(16),
    }
    payload["signature"] = hmac.new(trust_key, _canonical_json(payload), "sha256").hexdigest()
    payload["report_id"] = _sha256_bytes(_canonical_json(payload))
    return payload


def _validate_report_receipts(
    paths: tuple[Path, ...],
    *,
    subject: Subject,
    expected_main_sha: str,
    integration_sha: str,
    trust_key: bytes,
    pause_seconds: int,
    now: datetime,
) -> tuple[str, ...]:
    if len(paths) != 2:
        raise PromotionRefusal("enforce requires exactly two clean report-cycle receipts")
    reports: list[tuple[datetime, str]] = []
    for path in paths:
        raw = _exact_file(path, "report-cycle receipt")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise PromotionRefusal("report-cycle receipt is not valid JSON") from None
        if not isinstance(payload, dict):
            raise PromotionRefusal("report-cycle receipt is not an object")
        report_id = payload.pop("report_id", None)
        if not isinstance(report_id, str) or not hmac.compare_digest(
            report_id,
            _sha256_bytes(_canonical_json(payload)),
        ):
            raise PromotionRefusal("report-cycle receipt ID is invalid")
        signature = payload.pop("signature", None)
        if not isinstance(signature, str) or not hmac.compare_digest(
            hmac.new(trust_key, _canonical_json(payload), "sha256").hexdigest(),
            signature,
        ):
            raise PromotionRefusal("report-cycle receipt signature is invalid")
        expected = {
            "schema": _REPORT_SCHEMA,
            "status": "report",
            "subject_digest": subject.digest,
            "main_sha": expected_main_sha,
            "integration_sha": integration_sha,
            "config_sha256": subject.config_sha256,
            "gate_targets": list(subject.gate_targets),
            "authorship_sha256": subject.authorization.authorship_sha256,
            "review_sha256": list(subject.authorization.review_sha256),
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise PromotionRefusal("report-cycle receipt is bound to a different subject")
        if (
            not _SHA_RE.fullmatch(str(payload.get("main_sha") or ""))
            or not _SHA_RE.fullmatch(str(payload.get("integration_sha") or ""))
            or len(str(payload.get("nonce") or "")) < 16
        ):
            raise PromotionRefusal("report-cycle receipt identity fields are invalid")
        observed = _parse_utc(payload.get("observed_at"), "report-cycle")
        age = (now - observed).total_seconds()
        if age < -60 or age > _REPORT_MAX_AGE_SECONDS:
            raise PromotionRefusal("report-cycle receipt is outside the freshness window")
        reports.append((observed, report_id))
    reports.sort()
    if reports[0][1] == reports[1][1]:
        raise PromotionRefusal("report-cycle receipts are duplicates")
    if (reports[1][0] - reports[0][0]).total_seconds() < pause_seconds:
        raise PromotionRefusal("report-cycle receipts do not satisfy the configured pause")
    return tuple(report_id for _, report_id in reports)


def _validate_operator_authorization(
    path: Path | None,
    *,
    subject_digest: str,
    report_ids: tuple[str, ...],
    evidence_root: Path,
    now: datetime,
) -> str:
    if path is None:
        raise PromotionRefusal("operator authorization artifact is required")
    raw = _exact_file(path, "operator authorization")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise PromotionRefusal("operator authorization is not valid JSON") from None
    if not isinstance(payload, dict):
        raise PromotionRefusal("operator authorization is not an object")
    encoded_signature = payload.pop("signature", None)
    if not isinstance(encoded_signature, str):
        raise PromotionRefusal("operator authorization signature is missing")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
        public_path = evidence_root / "operator-authorization.pub"
        public_raw = _exact_file(
            public_path,
            "operator authorization public key",
        )
        if stat.S_IMODE(public_path.stat(follow_symlinks=False).st_mode) & 0o022:
            raise PromotionRefusal("operator authorization public key is writable by others")
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, _canonical_json(payload))
    except (ValueError, InvalidSignature):
        raise PromotionRefusal("operator authorization signature is invalid") from None
    if (
        payload.get("schema") != _OPERATOR_SCHEMA
        or payload.get("authorized") is not True
        or payload.get("subject_digest") != subject_digest
        or payload.get("report_ids") != list(report_ids)
        or not str(payload.get("operator_id") or "").strip()
        or len(str(payload.get("nonce") or "")) < 16
    ):
        raise PromotionRefusal("operator authorization is not bound to this exact promotion")
    issued = _parse_utc(payload.get("issued_at"), "operator authorization")
    age = (now - issued).total_seconds()
    if age < -60 or age > _REPORT_MAX_AGE_SECONDS:
        raise PromotionRefusal("operator authorization is outside the freshness window")
    return _sha256_bytes(raw)


def _validate_request(request: PromotionRequest) -> tuple[Path, IntegrationConfig]:
    if request.repo.is_symlink():
        raise PromotionRefusal("repo cannot be a symlink")
    repo = request.repo.expanduser().resolve()
    if not repo.is_dir() or _git(repo, "rev-parse", "--show-toplevel", check=False) != str(repo):
        raise PromotionRefusal("repo must be an existing top-level worktree")
    _require_sha(request.expected_main_sha, "expected_main_sha")
    _require_sha(request.integration_sha, "integration_sha")
    config = load_integration_config(request.config_path)
    _require_candidate_ref(request.candidate_ref, config.protected_branches)
    return repo, config


def _capture(
    request: PromotionRequest,
    repo: Path,
    *,
    expected_candidate_sha: str | None,
) -> str:
    main_ref = _ref_value(repo, "refs/heads/main")
    head = _resolve_commit(repo, "HEAD", "HEAD")
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch != "main":
        raise PromotionRefusal(
            f"coordinator worktree must be on main, found {branch or 'detached'}"
        )
    if main_ref != request.expected_main_sha or head != request.expected_main_sha:
        raise PromotionRefusal(
            f"main moved: expected {request.expected_main_sha}, ref={main_ref}, HEAD={head}"
        )
    if not _tree_clean(repo):
        raise PromotionRefusal("coordinator main worktree is dirty")
    if _resolve_commit(repo, request.integration_sha, "integration_sha") != request.integration_sha:
        raise PromotionRefusal("integration SHA did not resolve to itself")
    candidate_now = _ref_value(repo, request.candidate_ref)
    if expected_candidate_sha is None and candidate_now is not None:
        raise PromotionRefusal(f"candidate ref already exists: {request.candidate_ref}")
    if expected_candidate_sha is not None and candidate_now != expected_candidate_sha:
        raise PromotionRefusal(
            f"candidate ref moved: expected {expected_candidate_sha}, found {candidate_now}"
        )
    merge_base = _git(
        repo,
        "merge-base",
        request.expected_main_sha,
        request.integration_sha,
        check=False,
    )
    if merge_base != request.expected_main_sha:
        raise PromotionRefusal("expected main is not the exact forward merge base")
    return merge_base


@dataclass(slots=True)
class _StableLock:
    repo: Path
    evidence_root: Path
    lock_path: Path
    descriptors: list[int] = field(default_factory=list)
    identities: list[tuple[Path, int, int]] = field(default_factory=list)
    index_lock: Path | None = None
    index_identity: tuple[int, int] | None = None

    @classmethod
    def acquire(cls, request: PromotionRequest, repo: Path) -> _StableLock:
        canonical_root = repo / "var" / "gate-evidence"
        canonical_lock = canonical_root / "locks" / "promotion.lock"
        if request.evidence_root != canonical_root or request.lock_path != canonical_lock:
            raise PromotionRefusal("enforce requires the canonical repository promotion lock/root")
        for existing in (canonical_root, canonical_root / "locks"):
            if existing.exists() and (existing.is_symlink() or not existing.is_dir()):
                raise PromotionRefusal(f"canonical lock parent is unsafe: {existing}")
        (canonical_root / "locks").mkdir(parents=True, exist_ok=True, mode=0o700)
        git_common_raw = _git(repo, "rev-parse", "--git-common-dir")
        git_common = (
            (repo / git_common_raw).resolve()
            if not Path(git_common_raw).is_absolute()
            else Path(git_common_raw).resolve()
        )
        lock = cls(repo=repo, evidence_root=canonical_root, lock_path=canonical_lock)
        try:
            for directory in (git_common, canonical_root, canonical_root / "locks"):
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_DIRECTORY", 0)
                fd = os.open(directory, flags)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                info = os.fstat(fd)
                named = os.stat(directory, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                    raise PromotionRefusal(f"canonical lock directory changed: {directory}")
                lock.descriptors.append(fd)
                lock.identities.append((directory, info.st_dev, info.st_ino))
            fd = os.open(
                canonical_lock,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            named = os.stat(canonical_lock, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                raise PromotionRefusal("promotion lock inode changed while opening")
            lock.descriptors.append(fd)
            lock.identities.append((canonical_lock, info.st_dev, info.st_ino))

            index_lock = git_common / "index.lock"
            index_fd = os.open(
                index_lock,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            index_info = os.fstat(index_fd)
            lock.descriptors.append(index_fd)
            lock.index_lock = index_lock
            lock.index_identity = (index_info.st_dev, index_info.st_ino)
            return lock
        except BlockingIOError:
            lock.release()
            raise PromotionRefusal("another promotion holds the canonical host lock") from None
        except Exception:
            lock.release()
            raise

    def check(self) -> None:
        for path, device, inode in self.identities:
            try:
                named = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise PromotionRefusal(
                    f"canonical lock identity disappeared: {path}: {exc}"
                ) from exc
            if (named.st_dev, named.st_ino) != (device, inode):
                raise PromotionRefusal(f"canonical lock inode changed: {path}")
        if self.index_lock is not None and self.index_identity is not None:
            named = os.stat(self.index_lock, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != self.index_identity:
                raise PromotionRefusal("archi-morning index interlock inode changed")

    def release_index_interlock(self) -> list[str]:
        warnings: list[str] = []
        if self.index_lock is None or self.index_identity is None:
            return warnings
        try:
            named = os.stat(self.index_lock, follow_symlinks=False)
            if (named.st_dev, named.st_ino) == self.index_identity:
                self.index_lock.unlink()
            else:
                warnings.append(
                    "archi-morning index interlock was replaced; foreign path preserved"
                )
        except OSError as exc:
            warnings.append(f"failed to release archi-morning index interlock: {exc}")
        self.index_lock = None
        self.index_identity = None
        return warnings

    def release(self) -> list[str]:
        warnings = self.release_index_interlock()
        for path, device, inode in self.identities:
            try:
                named = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                warnings.append(
                    f"canonical lock identity disappeared during cleanup: {path}: {exc}"
                )
                continue
            if (named.st_dev, named.st_ino) != (device, inode):
                warnings.append(
                    f"canonical lock identity was replaced; foreign path preserved: {path}"
                )
        for fd in reversed(self.descriptors):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                warnings.append(f"failed to unlock promotion descriptor: {exc}")
            try:
                os.close(fd)
            except OSError as exc:
                warnings.append(f"failed to close promotion descriptor: {exc}")
        self.descriptors.clear()
        return warnings


def _cas_create_ref(repo: Path, ref: str, sha: str) -> None:
    proc = _run(["git", "update-ref", ref, sha, _ZERO_SHA], cwd=repo, check=False)
    if proc.returncode != 0:
        raise PromotionRefusal(
            f"candidate ref CAS create failed: {(proc.stderr or proc.stdout).strip()}"
        )


def _cas_delete_ref_no_raise(repo: Path, ref: str, sha: str) -> str | None:
    try:
        if _ref_value(repo, ref) != sha:
            return "candidate ref is foreign or moved; preserved"
        proc = _run(["git", "update-ref", "-d", ref, sha], cwd=repo, check=False)
        if proc.returncode != 0:
            return f"candidate ref cleanup failed: {(proc.stderr or proc.stdout).strip()}"
        return None
    except Exception as exc:
        return f"candidate ref cleanup could not run: {type(exc).__name__}: {exc}"


def _restore_oracles(worktree: Path, main_sha: str) -> None:
    tracked = set(_git(worktree, "ls-tree", "-r", "--name-only", main_sha).splitlines())
    for path in _ORACLE_PATHS:
        if path in tracked:
            _git(worktree, "restore", f"--source={main_sha}", "--staged", "--worktree", "--", path)
        else:
            _git(worktree, "rm", "-f", "--ignore-unmatch", "--", path)


def _construct_candidate(request: PromotionRequest, repo: Path, worktree: Path) -> str:
    _git(repo, "worktree", "add", "--detach", str(worktree), request.expected_main_sha)
    proc = _run(
        [
            "git",
            "-c",
            "user.name=omniagentos-promotion",
            "-c",
            "user.email=4580856+omniagentos-bot[bot]@users.noreply.github.com",
            "merge",
            "--no-ff",
            "--no-commit",
            request.integration_sha,
        ],
        cwd=worktree,
        check=False,
    )
    if proc.returncode != 0:
        _git(worktree, "merge", "--abort", check=False)
        raise PromotionRefusal(
            f"integration candidate conflicts with main: {(proc.stderr or proc.stdout).strip()}"
        )
    _restore_oracles(worktree, request.expected_main_sha)
    if not _git(worktree, "diff", "--cached", "--name-only"):
        raise PromotionRefusal("oracle-free promotion candidate has no changes")
    _run(
        [
            "git",
            "-c",
            "user.name=omniagentos-promotion",
            "-c",
            "user.email=4580856+omniagentos-bot[bot]@users.noreply.github.com",
            "commit",
            "-m",
            "merge(p0): finalize reviewed integration without merge-owned architecture oracles",
        ],
        cwd=worktree,
    )
    promotion_sha = _resolve_commit(worktree, "HEAD", "promotion candidate")
    changed = set(
        _git(
            worktree, "diff", "--name-only", f"{request.expected_main_sha}...{promotion_sha}"
        ).splitlines()
    )
    if not changed or changed.intersection(_ORACLE_PATHS) or not _tree_clean(worktree):
        raise PromotionRefusal("promotion candidate failed oracle-free clean-tree proof")
    return promotion_sha


def _make_code_merge(repo: Path, main_sha: str, promotion_sha: str) -> str:
    tree = _git(repo, "rev-parse", f"{promotion_sha}^{{tree}}")
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "omniagentos-promotion",
            "GIT_AUTHOR_EMAIL": "4580856+omniagentos-bot[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "omniagentos-promotion",
            "GIT_COMMITTER_EMAIL": "4580856+omniagentos-bot[bot]@users.noreply.github.com",
        }
    )
    proc = _run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            main_sha,
            "-p",
            promotion_sha,
            "-m",
            f"merge(p0): promote exact gate-approved candidate {promotion_sha}",
        ],
        cwd=repo,
        env=env,
    )
    code_sha = proc.stdout.strip()
    _require_sha(code_sha, "code_main_sha")
    parents = tuple(_git(repo, "show", "-s", "--format=%P", code_sha).split())
    if parents != (main_sha, promotion_sha):
        raise PromotionRefusal("off-ref code merge has unexpected parents")
    return code_sha


def _default_archdocs_runner(worktree: Path) -> str:
    python = worktree / ".venv" / "bin" / "python"
    executable = str(python) if python.is_file() else sys.executable
    _run(
        [executable, "-m", "omniagentos.archdocs.generate", "--repo-root", str(worktree)],
        cwd=worktree,
    )
    _run(
        [
            executable,
            "-c",
            "import sys; from omniagentos.archdocs.staleness import stamp_archi; "
            "stamp_archi(sys.argv[1])",
            str(worktree),
        ],
        cwd=worktree,
    )
    _run(
        [executable, "-m", "omniagentos.archdocs.diagram", "--repo-root", str(worktree)],
        cwd=worktree,
    )
    _git(worktree, "add", "--", *_ORACLE_PATHS)
    if not _git(worktree, "diff", "--cached", "--name-only"):
        raise PromotionRefusal("architecture rehearsal produced no oracle refresh commit")
    _run(
        [
            "git",
            "-c",
            "user.name=archi-morning",
            "-c",
            "user.email=4580856+omniagentos-bot[bot]@users.noreply.github.com",
            "commit",
            "-m",
            "archi-morning: refresh map + diagram",
        ],
        cwd=worktree,
    )
    final_sha = _resolve_commit(worktree, "HEAD", "architecture rehearsal")
    _run(
        [
            executable,
            "-m",
            "omniagentos.archdocs.generate",
            "--repo-root",
            str(worktree),
            "--check",
        ],
        cwd=worktree,
    )
    _run(
        [executable, "-m", "omniagentos.archdocs.diagram", "--repo-root", str(worktree), "--check"],
        cwd=worktree,
    )
    if is_stale(worktree):
        raise PromotionRefusal("architecture freshness semantics are not promotion-safe")
    return final_sha


def _terminal_proof(
    repo: Path,
    *,
    main_sha: str,
    integration_sha: str,
    promotion_sha: str,
    code_sha: str,
    final_sha: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    code_parents = tuple(_git(repo, "show", "-s", "--format=%P", code_sha).split())
    final_parents = tuple(_git(repo, "show", "-s", "--format=%P", final_sha).split())
    if code_parents != (main_sha, promotion_sha):
        raise PromotionRefusal("code merge parent contract is invalid")
    if final_parents != (code_sha,):
        raise PromotionRefusal(
            "terminal architecture commit must have exactly code_main_sha as parent"
        )
    if not _is_ancestor(repo, promotion_sha, final_sha):
        raise PromotionRefusal("promotion SHA is not an ancestor of terminal main")
    if not _is_ancestor(repo, integration_sha, final_sha):
        raise PromotionRefusal("integration SHA is not an ancestor of terminal main")
    paths = tuple(
        sorted(
            _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", final_sha).splitlines()
        )
    )
    if not paths or not set(paths).issubset(_ORACLE_PATHS):
        raise PromotionRefusal("terminal commit changes paths outside architecture oracles")
    pathspec = [".", *[f":(exclude){path}" for path in _ORACLE_PATHS]]
    if (
        _run(
            ["git", "diff", "--quiet", code_sha, final_sha, "--", *pathspec], cwd=repo, check=False
        ).returncode
        != 0
    ):
        raise PromotionRefusal("terminal non-oracle tree differs from authenticated code merge")
    return final_parents, paths


def _request_contract_digest(request: GateRunRequest, config_sha256: str) -> str:
    _, targets = parse_gate_command(str(request.gate_config.get("command") or ""))
    body = {
        "routine_id": request.routine_id,
        "run_id": request.run_id,
        "iteration": request.iteration,
        "gate_type": request.gate_type,
        "command": normalize_gate_command(str(request.gate_config.get("command") or "")),
        "targets": list(targets),
        "workspace_digest": workspace_digest_for(request.workspace),
        "candidate_sha": request.candidate_sha,
        "merge_base_sha": request.merge_base_sha,
        "config_sha256": config_sha256,
        "expected_exit_code": request.gate_config.get("expected_exit_code"),
        "tool": "pytest",
    }
    return _sha256_bytes(_canonical_json(body))


def _default_receipt_producer(
    request: GateRunRequest,
    store: GateEvidenceStore,
    request_contract_digest: str,
) -> ProducedReceipt:
    if store.load(request.routine_id, request.run_id) is not None:
        raise PromotionRefusal("pre-existing receipt replay is not accepted for promotion")
    python = request.workspace / ".venv" / "bin" / "python"
    executable = str(python) if python.is_file() else sys.executable
    outcome = produce_gate_evidence(
        PytestGateRunner(store, python_executable=executable),
        store,
        request,
    )
    if (
        outcome.status != "evidence"
        or outcome.evidence is None
        or outcome.detail != "recorded new evidence"
    ):
        raise PromotionRefusal(
            f"fresh signed receipt was not produced: {outcome.status}: {outcome.detail}"
        )
    return ProducedReceipt(outcome.evidence, request_contract_digest, True)


def _validate_receipt_subject(
    produced: ProducedReceipt,
    request: GateRunRequest,
    *,
    expected_contract_digest: str,
    not_before: datetime,
) -> None:
    evidence = produced.evidence
    expected_command = normalize_gate_command(str(request.gate_config["command"]))
    _, expected_targets = parse_gate_command(expected_command)
    expected_workspace = workspace_digest_for(request.workspace)
    if not produced.produced_new:
        raise PromotionRefusal("receipt was not freshly produced")
    if produced.request_contract_digest != expected_contract_digest:
        raise PromotionRefusal("receipt config/request contract digest mismatch")
    exact = {
        "schema": (evidence.schema, SCHEMA),
        "routine": (evidence.routine_id, request.routine_id),
        "run": (evidence.run_id, request.run_id),
        "iteration": (evidence.iteration, request.iteration),
        "gate type": (evidence.gate_type, request.gate_type),
        "command": (evidence.command, expected_command),
        "targets": (evidence.targets, expected_targets),
        "workspace": (evidence.workspace_digest, expected_workspace),
        "tool": (evidence.tool, "pytest"),
        "candidate": (evidence.candidate_sha, request.candidate_sha),
        "merge base": (evidence.merge_base_sha, request.merge_base_sha),
        "workspace SHA": (evidence.workspace_sha, request.candidate_sha),
    }
    for label, (actual, expected) in exact.items():
        if actual != expected:
            raise PromotionRefusal(f"receipt {label} does not match requested gate")
    expected_binding = binding_digest(
        routine_id=request.routine_id,
        run_id=request.run_id,
        iteration=request.iteration,
        gate_type=request.gate_type,
        command=expected_command,
        targets=expected_targets,
        workspace_digest=expected_workspace,
        candidate_sha=request.candidate_sha,
        merge_base_sha=request.merge_base_sha,
    )
    if not hmac.compare_digest(evidence.binding_digest, expected_binding):
        raise PromotionRefusal("receipt binding digest does not match requested gate")
    finished = _parse_utc(evidence.finished_at, "receipt")
    if finished < not_before - _ONE_SECOND:
        raise PromotionRefusal("receipt predates this promotion transaction")


_ONE_SECOND = timedelta(seconds=1)


def _assert_candidate_import(worktree: Path) -> None:
    python = worktree / ".venv" / "bin" / "python"
    executable = str(python) if python.is_file() else sys.executable
    env = dict(os.environ)
    # Mirror gate_runner._sanitized_env: it names the coordinator source root.
    # The verifier's cwd must still make the candidate tree win import order.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    proc = _run(
        [executable, "-c", "import omniagentos; print(omniagentos.__file__)"],
        cwd=worktree,
        env=env,
    )
    imported = Path(proc.stdout.strip()).resolve()
    if inode_relative_parts_anchored(imported, worktree) is None:
        raise PromotionRefusal(f"receipt verifier imports outside candidate tree: {imported}")


def _default_merge_gate_runner(repo: Path, candidate_ref: str, receipt_path: Path) -> str:
    env = dict(os.environ)
    env["REPO"] = str(repo)
    env["MERGE_GATE_EVIDENCE_ROOT"] = str(receipt_path.parents[2])
    # Resolve the judge from the tree it grades, never from the ambient cwd
    # (2026-08-07). A relative "scripts/merge-gate.sh" is whatever the process's
    # working directory happens to hold, which is one input taken from here and
    # the graded tree taken from there — the split that let a stale gate grade a
    # correctly-pinned workspace. cwd is already `repo`, so this is
    # behaviour-identical today and stays correct if a caller ever changes it.
    proc = _run(
        ["bash", str(repo / "scripts" / "merge-gate.sh"), candidate_ref, str(receipt_path)],
        cwd=repo,
        env=env,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise PromotionRefusal(f"merge gate refused candidate:\n{output.strip()}")
    if "MERGE GATE: PASS" not in output:
        raise PromotionRefusal("merge gate exited zero without an explicit PASS marker")
    return output


def _atomic_idempotent(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if _exact_file(path, "existing promotion artifact") == body:
            return
        raise PromotionRefusal(f"promotion artifact collision with different content: {path}")
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temporary.unlink(missing_ok=True)


class PromotionFinalizer:
    def __init__(
        self,
        *,
        receipt_producer: ReceiptProducer | None = None,
        merge_gate_runner: MergeGateRunner | None = None,
        archdocs_runner: ArchdocsRunner | None = None,
        checkpoint: Checkpoint | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.receipt_producer = receipt_producer or _default_receipt_producer
        self.merge_gate_runner = merge_gate_runner or _default_merge_gate_runner
        self.archdocs_runner = archdocs_runner or _default_archdocs_runner
        self.checkpoint = checkpoint or (lambda _name, _request, _promotion, _final: None)
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(self, request: PromotionRequest) -> PromotionResult:
        repo, config = _validate_request(request)
        if request.enforce:
            canonical_root = repo / "var" / "gate-evidence"
            canonical_lock = canonical_root / "locks" / "promotion.lock"
            if request.evidence_root != canonical_root or request.lock_path != canonical_lock:
                raise PromotionRefusal(
                    "enforce requires the canonical repository promotion lock/root"
                )
        trust_key = _load_trust_key(request.evidence_root)
        subject = _subject(request, config, trust_key)
        merge_base = _capture(request, repo, expected_candidate_sha=None)
        if not request.enforce:
            receipt = _make_report_receipt(request, subject, trust_key, self.clock())
            _capture(request, repo, expected_candidate_sha=None)
            return self._result(
                request,
                subject,
                merge_base,
                status="report",
                report_receipt=receipt,
            )

        if os.environ.get("OMNIAGENTOS_AUTO_PROMOTE"):
            raise PromotionRefusal("environment promotion override is forbidden at this boundary")
        if _raw_promotion_mode(request.config_path) != "enforce":
            raise PromotionRefusal("raw integration YAML does not independently authorize enforce")
        now = self.clock()
        report_ids = _validate_report_receipts(
            request.report_receipts,
            subject=subject,
            expected_main_sha=request.expected_main_sha,
            integration_sha=request.integration_sha,
            trust_key=trust_key,
            pause_seconds=config.promotion_pause_s,
            now=now,
        )
        operator_hash = _validate_operator_authorization(
            request.operator_authorization,
            subject_digest=subject.digest,
            report_ids=report_ids,
            evidence_root=request.evidence_root,
            now=now,
        )
        lock = _StableLock.acquire(request, repo)
        warnings: list[str] = []
        try:
            lock.check()
            _capture(request, repo, expected_candidate_sha=None)
            result = self._enforce(
                request,
                repo,
                config,
                subject,
                merge_base,
                report_ids,
                operator_hash,
                lock,
                warnings,
            )
        except BaseException as exc:
            cleanup_warnings = lock.release()
            for warning in cleanup_warnings:
                exc.add_note(f"promotion lock cleanup warning: {warning}")
            raise
        cleanup_warnings = lock.release()
        if cleanup_warnings:
            result = dataclass_replace(
                result,
                warnings=tuple([*result.warnings, *cleanup_warnings]),
            )
        return result

    def _enforce(
        self,
        request: PromotionRequest,
        repo: Path,
        config: IntegrationConfig,
        subject: Subject,
        merge_base: str,
        report_ids: tuple[str, ...],
        operator_hash: str,
        lock: _StableLock,
        warnings: list[str],
    ) -> PromotionResult:
        promotion_sha = ""
        candidate_owned = False
        preserve_candidate = False
        with tempfile.TemporaryDirectory(prefix="omni-promotion-") as scratch:
            scratch_path = Path(scratch)
            candidate_worktree = scratch_path / "candidate"
            terminal_worktree = scratch_path / "terminal"
            try:
                lock.check()
                promotion_sha = _construct_candidate(request, repo, candidate_worktree)
                lock.check()
                _capture(request, repo, expected_candidate_sha=None)
                _cas_create_ref(repo, request.candidate_ref, promotion_sha)
                candidate_owned = True
                self.checkpoint("candidate_published", request, promotion_sha, "")
                lock.check()
                _capture(request, repo, expected_candidate_sha=promotion_sha)

                code_sha = _make_code_merge(repo, request.expected_main_sha, promotion_sha)
                _git(repo, "worktree", "add", "--detach", str(terminal_worktree), code_sha)
                final_sha = self.archdocs_runner(terminal_worktree)
                if _resolve_commit(terminal_worktree, "HEAD", "terminal worktree") != final_sha:
                    raise PromotionRefusal(
                        "archdocs runner returned a SHA other than its worktree HEAD"
                    )
                terminal_parents, terminal_paths = _terminal_proof(
                    repo,
                    main_sha=request.expected_main_sha,
                    integration_sha=request.integration_sha,
                    promotion_sha=promotion_sha,
                    code_sha=code_sha,
                    final_sha=final_sha,
                )
                lock.check()
                _capture(request, repo, expected_candidate_sha=promotion_sha)

                _assert_candidate_import(candidate_worktree)
                command = "pytest " + shlex.join(list(config.gate_targets))
                gate_request = GateRunRequest(
                    routine_id="merge-gate",
                    run_id=promotion_sha,
                    iteration=1,
                    gate_type="merge_candidate",
                    gate_config={
                        "command": command,
                        "expected_exit_code": 0,
                        "candidate_sha": promotion_sha,
                        "merge_base_sha": request.expected_main_sha,
                        "config_sha256": subject.config_sha256,
                    },
                    workspace=candidate_worktree,
                    candidate_sha=promotion_sha,
                    merge_base_sha=request.expected_main_sha,
                )
                contract_digest = _request_contract_digest(gate_request, subject.config_sha256)
                receipt_not_before = self.clock()
                store = GateEvidenceStore(request.evidence_root, create_key=False)
                produced = self.receipt_producer(gate_request, store, contract_digest)
                _validate_receipt_subject(
                    produced,
                    gate_request,
                    expected_contract_digest=contract_digest,
                    not_before=receipt_not_before,
                )
                receipt_path = (
                    request.evidence_root / "records" / "merge-gate" / f"{promotion_sha}.json"
                )
                verified = verify_candidate_receipt(
                    receipt_path,
                    evidence_root=request.evidence_root,
                    candidate_sha=promotion_sha,
                    merge_base_sha=request.expected_main_sha,
                )
                if verified != produced.evidence:
                    raise PromotionRefusal("authenticated receipt differs from producer result")
                self.checkpoint("receipt_verified", request, promotion_sha, final_sha)
                lock.check()
                _capture(request, repo, expected_candidate_sha=promotion_sha)

                gate_output = self.merge_gate_runner(repo, request.candidate_ref, receipt_path)
                self.checkpoint("gate_passed", request, promotion_sha, final_sha)
                lock.check()
                _capture(request, repo, expected_candidate_sha=promotion_sha)

                artifact_dir = request.evidence_root / "promotion-reports" / promotion_sha
                gate_path = artifact_dir / "merge-gate.log"
                _atomic_idempotent(gate_path, gate_output.encode("utf-8"))
                receipt_hash = _sha256_file(receipt_path)
                gate_output_hash = _sha256_text(gate_output)
                prepared = self._result(
                    request,
                    subject,
                    merge_base,
                    status="prepared",
                    report_cycle_ids=report_ids,
                    operator_authorization_sha256=operator_hash,
                    promotion_sha=promotion_sha,
                    receipt_path=str(receipt_path),
                    receipt_sha256=receipt_hash,
                    merge_gate_output_path=str(gate_path),
                    merge_gate_output_sha256=gate_output_hash,
                    code_main_sha=code_sha,
                    final_main_sha=final_sha,
                    observed_main_sha=request.expected_main_sha,
                    terminal_parents=terminal_parents,
                    terminal_changed_paths=terminal_paths,
                )
                _atomic_idempotent(
                    artifact_dir / "prepared.json",
                    (prepared.to_json() + "\n").encode("utf-8"),
                )

                self.checkpoint("publication_boundary", request, promotion_sha, final_sha)
                lock.check()
                _capture(request, repo, expected_candidate_sha=promotion_sha)
                self.checkpoint("before_main_cas", request, promotion_sha, final_sha)
                cas = _run(
                    [
                        "git",
                        "update-ref",
                        "refs/heads/main",
                        final_sha,
                        request.expected_main_sha,
                    ],
                    cwd=repo,
                    check=False,
                )
                if cas.returncode != 0:
                    raise PromotionRefusal(
                        f"authoritative main CAS refused: {(cas.stderr or cas.stdout).strip()}"
                    )
                preserve_candidate = True
                published_result = self._result(
                    request,
                    subject,
                    merge_base,
                    status="published",
                    report_cycle_ids=report_ids,
                    operator_authorization_sha256=operator_hash,
                    promotion_sha=promotion_sha,
                    receipt_path=str(receipt_path),
                    receipt_sha256=receipt_hash,
                    merge_gate_output_path=str(gate_path),
                    merge_gate_output_sha256=gate_output_hash,
                    code_main_sha=code_sha,
                    final_main_sha=final_sha,
                    observed_main_sha=final_sha,
                    terminal_parents=terminal_parents,
                    terminal_changed_paths=terminal_paths,
                )
                published_artifact_ok = True
                try:
                    _atomic_idempotent(
                        artifact_dir / "published.json",
                        (published_result.to_json() + "\n").encode("utf-8"),
                    )
                except Exception as exc:
                    published_artifact_ok = False
                    warnings.append(f"published state artifact publication failed: {exc}")

                observed_main = ""
                finalized = False
                try:
                    warnings.extend(lock.release_index_interlock())
                    reset = _run(["git", "reset", "--hard", final_sha], cwd=repo, check=False)
                    observed_main = _ref_value(repo, "refs/heads/main") or ""
                    observed_head = _ref_value(repo, "HEAD") or ""
                    status_proc = _run(
                        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                        cwd=repo,
                        check=False,
                    )
                    finalized = (
                        reset.returncode == 0
                        and published_artifact_ok
                        and observed_main == final_sha
                        and observed_head == final_sha
                        and status_proc.returncode == 0
                        and not status_proc.stdout.strip()
                    )
                    reset_detail = (reset.stderr or reset.stdout).strip()
                except BaseException as exc:
                    reset_detail = f"{type(exc).__name__}: {exc}"
                    try:
                        observed_main = _ref_value(repo, "refs/heads/main") or ""
                    except Exception:
                        observed_main = ""
                if not finalized:
                    warnings.append(
                        "authoritative ref published but coordinator worktree needs recovery: "
                        + reset_detail
                    )
                status: Literal["finalized", "published_recovery_required"] = (
                    "finalized" if finalized else "published_recovery_required"
                )
                result = self._result(
                    request,
                    subject,
                    merge_base,
                    status=status,
                    report_cycle_ids=report_ids,
                    operator_authorization_sha256=operator_hash,
                    promotion_sha=promotion_sha,
                    receipt_path=str(receipt_path),
                    receipt_sha256=receipt_hash,
                    merge_gate_output_path=str(gate_path),
                    merge_gate_output_sha256=gate_output_hash,
                    code_main_sha=code_sha,
                    final_main_sha=final_sha,
                    observed_main_sha=observed_main,
                    terminal_parents=terminal_parents,
                    terminal_changed_paths=terminal_paths,
                    warnings=tuple(warnings),
                )
                try:
                    _atomic_idempotent(
                        artifact_dir / f"{status}.json",
                        (result.to_json() + "\n").encode("utf-8"),
                    )
                except Exception as exc:
                    warnings.append(f"terminal state artifact publication failed: {exc}")
                    result = self._result(
                        request,
                        subject,
                        merge_base,
                        status="published_recovery_required",
                        report_cycle_ids=report_ids,
                        operator_authorization_sha256=operator_hash,
                        promotion_sha=promotion_sha,
                        receipt_path=str(receipt_path),
                        receipt_sha256=receipt_hash,
                        merge_gate_output_path=str(gate_path),
                        merge_gate_output_sha256=gate_output_hash,
                        code_main_sha=code_sha,
                        final_main_sha=final_sha,
                        observed_main_sha=observed_main,
                        terminal_parents=terminal_parents,
                        terminal_changed_paths=terminal_paths,
                        warnings=tuple(warnings),
                    )
                if result.status == "finalized":
                    preserve_candidate = False
                return result
            finally:
                for worktree in (terminal_worktree, candidate_worktree):
                    try:
                        _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
                    except Exception as exc:
                        warnings.append(
                            f"worktree cleanup failed for {worktree}: {type(exc).__name__}: {exc}"
                        )
                if candidate_owned and not preserve_candidate:
                    warning = _cas_delete_ref_no_raise(repo, request.candidate_ref, promotion_sha)
                    if warning:
                        warnings.append(warning)

    @staticmethod
    def _result(
        request: PromotionRequest,
        subject: Subject,
        merge_base: str,
        *,
        status: Literal[
            "report",
            "prepared",
            "published",
            "finalized",
            "published_recovery_required",
        ],
        **kwargs: Any,
    ) -> PromotionResult:
        return PromotionResult(
            schema=_FINALIZER_SCHEMA,
            status=status,
            main_sha=request.expected_main_sha,
            integration_sha=request.integration_sha,
            merge_base_sha=merge_base,
            candidate_ref=request.candidate_ref,
            subject_digest=subject.digest,
            config_sha256=subject.config_sha256,
            authorship_sha256=subject.authorization.authorship_sha256,
            review_sha256=subject.authorization.review_sha256,
            authorship_families=subject.authorization.authorship_families,
            reviewer_families=subject.authorization.reviewer_families,
            gate_targets=subject.gate_targets,
            **kwargs,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report or finalize an exact-SHA promotion")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--expected-main-sha", required=True)
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--authorship-manifest", required=True, type=Path)
    parser.add_argument("--review", action="append", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--candidate-ref")
    parser.add_argument("--report-receipt", action="append", default=[], type=Path)
    parser.add_argument("--operator-authorization", type=Path)
    parser.add_argument("--enforce", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    evidence_root = (
        args.evidence_root.expanduser().resolve()
        if args.evidence_root
        else repo / "var" / "gate-evidence"
    )
    request = PromotionRequest(
        repo=repo,
        expected_main_sha=args.expected_main_sha,
        integration_sha=args.integration_sha,
        authorship_manifest=args.authorship_manifest.expanduser().resolve(),
        aggregate_reviews=tuple(path.expanduser().resolve() for path in args.review),
        config_path=(args.config or repo / "configs" / "integration.yaml").expanduser().resolve(),
        evidence_root=evidence_root,
        lock_path=(args.lock_file or evidence_root / "locks" / "promotion.lock")
        .expanduser()
        .resolve(),
        candidate_ref=args.candidate_ref or f"refs/heads/promotion/p0-{args.integration_sha[:12]}",
        report_receipts=tuple(path.expanduser().resolve() for path in args.report_receipt),
        operator_authorization=(
            args.operator_authorization.expanduser().resolve()
            if args.operator_authorization
            else None
        ),
        enforce=bool(args.enforce),
    )
    try:
        result = PromotionFinalizer().run(request)
    except (GateEvidenceError, OSError, ValueError, PromotionRefusal) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(result.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Authorization",
    "ProducedReceipt",
    "PromotionFinalizer",
    "PromotionRefusal",
    "PromotionRequest",
    "PromotionResult",
    "Subject",
    "main",
]
