"""Improvement pipeline — the full §5 + §5b lifecycle (safety-critical core).

Drives a proposal through ``proposed → testing → judging → awaiting_human|approved
→ applying → applied → monitoring → confirmed|rolled_back``. Every status change goes
through ``store.transition_improvement`` (CAS + atomic hash-chained log append, §5b.1).

Durability contracts implemented here:
  * Apply is journaled in ``reliability_state`` (``apply_journal:<id>``) with a git
    commit trailer ``Imp-Id: <id> Attempt: <n>``; restart reconciliation scans
    ``git log --grep`` for the trailer — commit found ⇒ advance, else restore the
    worktree from ``pre_sha`` and retreat (§5b.2, M2).
  * Single-flight apply/rollback via a fencing lease (``store.acquire_lease``); an
    auto-apply whose diff overlaps a still-monitoring improvement's paths is deferred
    (M2). Overlap is checked while the lease is held, so applies never interleave.
  * Rollback is journaled identically (``rolling_back`` status, ``Imp-Revert`` trailer);
    a revert conflict is aborted CLEANLY (no partial revert), the worktree restored,
    and the improvement escalated to a human (M2).
  * Monitoring compares KPIs before/after using BOTH ``reliability_events`` AND a
    detector-independent raw signal (``runs.error`` / failed steps straight from the
    DB); "raw failures up while events down" is itself a critical alert (M1).

Hard invariants (enforced + tested):
  * The pipeline NEVER writes ``autonomy_settings`` (it only reads via ``resolve_autonomy``).
  * ``apply`` performs ONLY declared file diffs, schema-validated ``config_edits``, and
    skill/agent DB ops — there is NO shell execution of proposal content; ``plan[]`` is
    narrative and is never run (B1).
  * Deterministic ``governance.classify_risk`` runs on the authoritative sandbox diff and
    only RAISES risk; auto-apply requires quorum AND sandbox green AND
    ``risk ≤ max_auto_risk`` AND no Tier-S/P touch.
  * A notification fires on every terminal / human-gating transition (§11 policy).
  * EVERY destructive mutation performed while the apply lease is held is preceded
    by its OWN fresh ``store.assert_lease`` call — one assertion authorises exactly
    one mutation and is consumed by it (M-01). See "Fence-ticket discipline" below.

Fence-ticket discipline (M-01)
------------------------------
A single ``assert_lease`` call that "covers" a group of mutations is not a fence:
the lease can be stolen between the second and third mutation of the group. This
module therefore makes group-covering assertions structurally unwritable.

  * ``_fenced_section(key, token)`` opens a leased region.
  * ``_fenced_mutation(name)`` *arms* exactly one authorisation: it calls
    ``assert_lease`` fresh and records a ``("fence", name)`` trace entry. Arming a
    second authorisation while one is unconsumed raises ``FenceDisciplineError``.
  * The low-level side-effecting primitive *spends* that authorisation via
    ``_spend_fence(name)``, recording ``("mutation", name)``. Spending with nothing
    armed, spending the wrong name, or leaving an authorisation unspent all raise
    ``FenceDisciplineError``.
  * Notification persistence uses the stronger cross-module equivalent:
    ``BEGIN IMMEDIATE`` → validate the exact lease owner/token/generation →
    dedupe/insert → commit. Its trace pair is recorded only after the insert
    commits; dedupe, persistence failure, and claim loss record no fictional
    mutation.

The resulting ``fence_trace`` is therefore an exact, ordered, one-to-one
``fence → mutation`` sequence. Inserting a new destructive mutation without
wrapping it in its own ``_fenced_mutation`` fails loudly rather than silently
riding on a neighbouring assertion.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.reliability.contracts import (
    Improvement,
    LeaseConflict,
    ReliabilityStore,
)
from omniagentos.reliability.governance import (
    DiffEntry,
    GovernanceConfig,
    RiskResult,
    classify_risk,
    load_governance_config,
)
from omniagentos.reliability.taxonomy import (
    AutonomyMode,
    ChangeRisk,
    ImprovementStatus,
    VerdictKind,
)

_APPLY_LEASE_KEY = "reliability:apply"
_MAX_APPLY_ATTEMPTS = 3
_BUSY_RETRY_ATTEMPTS = 8
_BUSY_RETRY_BASE_MS = 10
_BUSY_RETRY_MAX_MS = 400

# Verdicts that count as an affirmative vote.
_APPROVE_VERDICTS = frozenset(
    {VerdictKind.APPROVE.value, VerdictKind.APPROVE_WITH_CONDITIONS.value}
)

# git subcommands that mutate the worktree, the index, or refs. Any of these
# issued without an armed fence ticket is a discipline violation (M-01).
_DESTRUCTIVE_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "fetch",
        "merge",
        "mv",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
        "tag",
        "update-ref",
        "worktree",
    }
)


class FenceDisciplineError(RuntimeError):
    """A destructive mutation was attempted without its own fresh fence assertion.

    Raised when the arm/consume protocol is violated: a mutation ran with no
    armed authorisation, a second authorisation was armed while one was still
    unconsumed (a group-covering assertion), an armed authorisation was left
    unspent, or a mutation spent an authorisation armed for a different name.
    """


# --- Compatibility contracts for sibling lanes (fail-closed until integrated).
#
# Detection is by symbol AND keyword-parameter signature, not by a marker flag
# and not by bare ``callable()``. A pre-integration shim that happens to expose
# the right name with the wrong shape — or a permissive ``**kwargs`` wrapper —
# must NOT satisfy the gate, because activation of the production sandbox/judge
# path (C-03) depends on the real semantics being present.

#: ``omniagentos.csi.frozen`` as shipped by L09 (ce5e75e).
L09_CONTAINMENT_CONTRACT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("assert_writable", ("paths", "patterns", "repo_root")),
    ("assert_canonical_destination", ("path", "root", "allowed_prefix")),
)

#: The generation-fenced lease API shipped by L03 (3c896de). ``assert_lease`` is
#: the fence gate; without it no mutation can be individually authorised.
L03_FENCED_LEASE_CONTRACT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("acquire_lease", ("key", "owner", "duration_seconds")),
    ("assert_lease", ("key", "owner", "token")),
    ("renew_lease", ("key", "owner", "token", "duration_seconds")),
    ("release_lease", ("key", "owner", "token")),
)


def _contract_gap(subject: Any, contract: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    """Return the first unmet clause of ``contract`` on ``subject``, else ``None``.

    A clause is met only when the named attribute is callable AND its signature
    declares every named parameter explicitly. ``**kwargs`` deliberately does not
    satisfy a clause: a catch-all wrapper proves nothing about the semantics.
    """
    for name, params in contract:
        fn = getattr(subject, name, None)
        if not callable(fn):
            return f"missing:{name}"
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return f"unintrospectable:{name}"
        missing = [p for p in params if p not in sig.parameters]
        if missing:
            return f"signature:{name}(-{',-'.join(missing)})"
    return None


# --- Injected collaborators (W3 sandbox + judges). Defined structurally so the
# pipeline is testable in isolation and does not hard-depend on W3 at import time.


@dataclass
class SandboxOutcome:
    """Result of a sandbox run (W3). ``diff_entries`` is the authoritative diff."""

    passed: bool
    diff_entries: list[DiffEntry] = field(default_factory=list)
    declared_paths: list[str] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    suggested_risk: int = 1


@dataclass
class JudgeOutcome:
    """Result of a judge panel attempt (W3)."""

    panel_attempt_id: str
    families_seated: int
    votes: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False  # 3 family-distinct votes recorded


class SandboxRunner(Protocol):
    def run(self, improvement: Improvement, repo_root: str) -> SandboxOutcome: ...


class JudgePanel(Protocol):
    def evaluate(self, improvement: Improvement, sandbox: SandboxOutcome) -> JudgeOutcome: ...


# --- Pipeline result records


@dataclass
class DecisionResult:
    """Outcome of the sandbox→judge decision phase for one improvement."""

    status: str
    risk_level: int
    auto_applied: bool = False
    requires_human: bool = False
    reason: str = ""


@dataclass
class ApplyResult:
    applied: bool = False
    deferred: bool = False
    reason: str = ""
    applied_sha: str | None = None


@dataclass
class RollbackResult:
    success: bool = False
    conflict: bool = False
    revert_sha: str | None = None
    reason: str = ""


@dataclass
class _Signals:
    """Failure signals over a window: detector events vs detector-independent raw.

    ``hours`` is the exposure length used for rate normalization (H-24). Absolute
    counts are not comparable across unequal baseline vs post-apply windows.
    """

    events: int = 0
    raw: int = 0
    hours: float = 0.0

    def rate(self, field: str) -> float:
        """Failures-per-hour for ``events`` or ``raw`` (0 when exposure is zero)."""
        count = float(getattr(self, field, 0) or 0)
        if self.hours <= 0:
            return 0.0
        return count / self.hours


@dataclass
class ActivationAssessment:
    """Whether the production sandbox/judge path may run (C-03).

    Production activation stays gated on L09 CSI containment and the L03 fenced
    apply-lease state contract. Missing sandbox/judge collaborators also block.
    Tests inject collaborators and may set ``force_activation=True`` to exercise
    the full path without waiting for L09/L03 integration.
    """

    active: bool
    reasons: list[str] = field(default_factory=list)
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "degraded": self.degraded or (not self.active),
            "reasons": list(self.reasons),
        }


def l09_containment_ready() -> tuple[bool, str]:
    """Report whether the L09 CSI containment API is integrated (C-03 gate).

    L09 (ce5e75e) ships, on ``omniagentos.csi.frozen``:
      * ``assert_writable(paths, *, patterns, repo_root)`` — the pre-L09 signature
        has no ``repo_root``, so containment cannot be anchored to a repo root;
      * ``assert_canonical_destination(path, *, root, allowed_prefix)`` — absent
        entirely before L09.

    Both clauses of :data:`L09_CONTAINMENT_CONTRACT` must hold. Anything less is
    fail-closed: production sandbox/judge activation stays blocked, and the
    returned reason names the specific unmet clause so the block is diagnosable.
    """
    try:
        from omniagentos.csi import frozen as csi_frozen
    except Exception as exc:  # noqa: BLE001 - any import failure is fail-closed
        return False, f"l09_import_failed:{exc}"
    gap = _contract_gap(csi_frozen, L09_CONTAINMENT_CONTRACT)
    if gap is not None:
        return False, f"l09_containment_not_integrated:{gap}"
    return True, "l09_containment_contract_met"


def l03_fenced_state_ready(store: Any = None) -> tuple[bool, str]:
    """Report whether the L03 generation-fenced lease API is integrated (C-03 gate).

    L03 (3c896de) ships ``acquire_lease`` returning a token and bumping a
    ``generation``, plus ``assert_lease(key, owner, token)`` which proves that
    exact token is still current and raises ``LeaseConflict`` otherwise. Without
    ``assert_lease`` there is no way to authorise an individual mutation, so the
    fence-ticket discipline degrades to bookkeeping and activation must stay shut.

    Checks the injected ``store`` instance when given (that is the object the
    pipeline will actually fence against), otherwise the module-level
    ``SqliteReliabilityStore``. There is no marker-flag bypass: only the real
    shipped signatures open this gate.
    """
    if store is not None:
        gap = _contract_gap(store, L03_FENCED_LEASE_CONTRACT)
        if gap is not None:
            return False, f"l03_fenced_state_not_integrated:{gap}"
        return True, "l03_fenced_lease_contract_met_on_store"

    try:
        from omniagentos.reliability import store as store_mod
    except Exception as exc:  # noqa: BLE001 - any import failure is fail-closed
        return False, f"l03_import_failed:{exc}"
    store_cls = getattr(store_mod, "SqliteReliabilityStore", None)
    if store_cls is None:
        return False, "l03_fenced_state_not_integrated:missing:SqliteReliabilityStore"
    gap = _contract_gap(store_cls, L03_FENCED_LEASE_CONTRACT)
    if gap is not None:
        return False, f"l03_fenced_state_not_integrated:{gap}"
    return True, "l03_fenced_lease_contract_met"


def assess_pipeline_activation(
    *,
    store: Any = None,
    sandbox_runner: Any = None,
    judge_panel: Any = None,
    force_activation: bool = False,
) -> ActivationAssessment:
    """Evaluate whether sandbox/judge activation is allowed (C-03).

    ``force_activation`` is the test seam only — production must leave it False so
    L09 + L03 gates cannot be bypassed by constructing collaborators alone.
    """
    reasons: list[str] = []
    if not force_activation:
        ok, reason = l09_containment_ready()
        if not ok:
            reasons.append(reason)
        ok, reason = l03_fenced_state_ready(store)
        if not ok:
            reasons.append(reason)
    if sandbox_runner is None:
        reasons.append("sandbox_runner_absent")
    if judge_panel is None:
        reasons.append("judge_panel_absent")
    active = not reasons
    return ActivationAssessment(active=active, reasons=reasons, degraded=not active)


@dataclass
class _FenceSection:
    """One leased region and its single-slot mutation authorisation (M-01).

    ``armed`` holds the name of the at-most-one mutation currently authorised by
    a fresh ``assert_lease``. It is set by ``_fenced_mutation`` and cleared by the
    primitive that performs the mutation. Because the slot holds one name, a
    single assertion can never authorise a second mutation.
    """

    key: str
    owner: str
    token: str
    generation: int | None
    enforced: bool
    armed: str | None = None


class ImprovementPipeline:
    """Stateless orchestrator over a ReliabilityStore + a git worktree (main tree)."""

    def __init__(
        self,
        store: ReliabilityStore,
        *,
        repo_root: str | Path,
        governance_config: GovernanceConfig | None = None,
        governance_config_path: str | Path | None = None,
        sandbox_runner: SandboxRunner | None = None,
        judge_panel: JudgePanel | None = None,
        notifier: Callable[..., Any] | None = None,
        owner: str = "pipeline",
        clock: Callable[[], datetime] | None = None,
        force_activation: bool = False,
    ) -> None:
        import logging
        import warnings

        warnings.warn(
            "DEPRECATION WARNING: The reliability engine is frozen and deprecated. It will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        logging.getLogger("omniagentos").warning(
            "DEPRECATION WARNING: The reliability engine is frozen and deprecated. It will be removed in a future release."
        )
        self.store = store
        self.repo_root = Path(repo_root)
        self.config = governance_config or load_governance_config(governance_config_path)
        self.sandbox_runner = sandbox_runner
        self.judge_panel = judge_panel
        self.force_activation = force_activation
        self.owner = f"{owner}:{uuid.uuid4().hex[:8]}"
        self._clock = clock
        self._uses_default_notifier = notifier is None
        self._notifier = notifier or self._default_notifier
        # Durable, operator-visible notification failures (M-46). Never silent.
        self.notify_failures: list[dict[str, Any]] = []
        # Raw access to reliability tables the frozen store exposes no method for
        # (apply journal in reliability_state, rollback_points, runs/steps raw signal).
        # Coupled only to SqliteStore's stable (_connection, _lock) — the same idiom
        # StewardStore uses. The lock is a genuine per-store object and is safe to
        # bind once; the connection is NOT (see the _conn property below).
        self._lock = getattr(store, "_lock", None)
        # Capability-detect the L03 fenced lease API once at init (C-03/M-01).
        self._has_fenced_api = self._detect_fenced_lease_api()
        # The currently open leased region, if any (M-01).
        self._fence_section: _FenceSection | None = None
        # A notification ticket is armed by the atomic DAL guard after
        # BEGIN IMMEDIATE and consumed only after the insert commits. It is
        # deliberately separate from ``_FenceSection.armed`` because no
        # mutation exists on the guarded dedupe path.
        self._guarded_notification_ticket: tuple[_FenceSection, str] | None = None
        # Identity captured immediately after this pipeline acquires a lease.
        # `_fenced_section` refuses externally supplied/uncaptured tokens so a
        # generation rewritten between acquisition and section entry cannot be
        # mistaken for the generation this invocation acquired.
        self._acquired_lease_identities: dict[tuple[str, str], tuple[str, int]] = {}
        # Ordered ("fence", name) / ("mutation", name) pairs for the last leased
        # region. Exposed for tests and post-incident audit: a well-disciplined
        # run alternates strictly fence→mutation with matching names.
        self.fence_trace: list[tuple[str, str]] = []

    def _detect_fenced_lease_api(self) -> bool:
        """Whether the store implements the full L03 generation-fenced lease API."""
        return _contract_gap(self.store, L03_FENCED_LEASE_CONTRACT) is None

    # === Fence-ticket discipline (M-01) ==================================
    #
    # One assertion authorises exactly one mutation. See the module docstring.

    @contextmanager
    def _fenced_section(self, key: str, token: str) -> Iterator[_FenceSection]:
        """Open the leased region in which mutations may be individually fenced.

        Resets ``fence_trace`` so each leased region yields a self-contained,
        auditable sequence. Sections do not nest: a nested section would make
        "which lease authorised this mutation" ambiguous.

        An enforced L03 section captures the positive integer generation that
        belongs to the acquired owner/token before any section mutation. The
        immutable identity is later revalidated at the notification DAL seam;
        owner/token equality alone cannot stand in for generation equality.
        """
        if self._fence_section is not None:
            raise FenceDisciplineError("fenced section already open; sections do not nest")
        # This attempted region is now the most recent leased region even when
        # identity validation rejects it before entry. Never retain a prior
        # region's successful mutations as a fictional trace for this failure.
        self.fence_trace = []
        owner = self.owner
        generation: int | None = None
        if self._has_fenced_api:
            captured = self._acquired_lease_identities.get((key, token))
            if captured is None:
                raise LeaseConflict(
                    f"Lease {key} generation identity was not captured at acquisition"
                )
            owner, generation = captured
            connection = self._conn
            if connection is None or self._lock is None:
                self._acquired_lease_identities.pop((key, token), None)
                raise FenceDisciplineError(
                    "generation-fenced section requires the reliability store connection"
                )
            try:
                with self._lock:
                    self._validated_lease_identity(
                        connection,
                        key=key,
                        owner=owner,
                        token=token,
                        expected_generation=generation,
                    )
            except BaseException:
                self._acquired_lease_identities.pop((key, token), None)
                raise
        section = _FenceSection(
            key=key,
            owner=owner,
            token=token,
            generation=generation,
            enforced=self._has_fenced_api,
        )
        self._fence_section = section
        try:
            yield section
        finally:
            self._fence_section = None
            self._acquired_lease_identities.pop((key, token), None)

    @contextmanager
    def _fenced_mutation(self, name: str) -> Iterator[None]:
        """Arm exactly one mutation with a FRESH ``assert_lease`` call (M-01).

        The body must perform exactly one destructive mutation, which consumes the
        authorisation via :meth:`_spend_fence`. Arming while another authorisation
        is unconsumed, or leaving this one unconsumed, raises
        :class:`FenceDisciplineError` — that is what makes a single assertion
        covering a group of mutations impossible to express.

        A stale or displaced owner surfaces here as ``LeaseConflict`` from
        ``store.assert_lease``, i.e. BEFORE the mutation runs.

        Outside a leased section this is a pass-through: there is no lease to
        fence against, and the same primitives serve unleased call sites. The
        guarantee this enforces is scoped to leased regions — inside one, every
        mutation has its own assertion.
        """
        section = self._fence_section
        if section is None:
            yield
            return
        if section.armed is not None:
            raise FenceDisciplineError(
                f"fence for {section.armed!r} is still unconsumed; cannot arm {name!r} "
                "(one assertion authorises exactly one mutation)"
            )
        if section.enforced:
            self.store.assert_lease(section.key, section.owner, section.token)
        section.armed = name
        self.fence_trace.append(("fence", name))
        try:
            yield
        except BaseException:
            section.armed = None
            raise
        if section.armed is not None:
            section.armed = None
            raise FenceDisciplineError(f"fence armed for {name!r} was never consumed by a mutation")

    def _spend_fence(self, name: str) -> None:
        """Consume the authorisation armed for ``name`` (called by primitives).

        Outside a leased section this is a no-op: the same primitives serve
        unleased call sites (e.g. ``human_approve``), which are not racing another
        apply. Inside a section, an unarmed or mismatched mutation is a violation.
        """
        section = self._fence_section
        if section is None:
            return
        if section.armed is None:
            raise FenceDisciplineError(
                f"destructive mutation {name!r} with no fresh fence assertion"
            )
        if section.armed != name:
            raise FenceDisciplineError(
                f"fence armed for {section.armed!r} but mutation {name!r} ran"
            )
        section.armed = None
        self.fence_trace.append(("mutation", name))

    def _renew_fence(self, key: str, token: str, duration_seconds: int = 1800) -> None:
        """Extend the lease before a lengthy phase so it cannot expire mid-mutation.

        Renewal is not an authorisation and never appears in ``fence_trace``; each
        mutation still arms its own fence.
        """
        if self._has_fenced_api:
            self.store.renew_lease(key, self.owner, token, duration_seconds)

    def _acquire_fenced_lease(
        self,
        key: str,
        *,
        duration_seconds: int,
    ) -> str:
        """Acquire a lease and immediately capture its persisted generation.

        L03's compatible public API returns only the token. Reading the row
        immediately after that successful acquisition preserves compatibility
        while binding this pipeline invocation to the owner/token/generation
        triple. The expected generation is derived before acquisition from L03's
        monotonic ``previous + 1`` contract, then required on the acquired row.
        A legitimate intervening acquisition changes the token and/or generation
        and is rejected. `_fenced_section` accepts only identities captured here.
        """
        owner = self.owner
        if not self._has_fenced_api:
            return self.store.acquire_lease(
                key,
                owner=owner,
                duration_seconds=duration_seconds,
            )
        connection = self._conn
        if connection is None or self._lock is None:
            raise FenceDisciplineError(
                "generation-fenced acquisition requires the reliability store connection"
            )
        token: str | None = None
        try:
            with self._lock:
                expected_generation = self._next_lease_generation(connection, key)
                token = self.store.acquire_lease(
                    key,
                    owner=owner,
                    duration_seconds=duration_seconds,
                )
                generation = self._validated_lease_identity(
                    connection,
                    key=key,
                    owner=owner,
                    token=token,
                    expected_generation=expected_generation,
                )
        except BaseException:
            if token is not None:
                try:
                    self.store.release_lease(key, owner, token)
                except Exception:  # noqa: BLE001 - preserve identity validation failure
                    pass
            raise
        self._acquired_lease_identities[(key, token)] = (owner, generation)
        return token

    @staticmethod
    def _next_lease_generation(connection: Any, key: str) -> int:
        """Expected generation of L03's next acquisition for ``key``."""
        row = connection.execute(
            "SELECT value_json FROM reliability_state WHERE key = ?",
            (f"lease:{key}",),
        ).fetchone()
        if not row:
            return 1
        try:
            lease = json.loads(row["value_json"])
            if not isinstance(lease, dict):
                raise ValueError("lease payload is not an object")
            previous = lease.get("generation", 0)
            if isinstance(previous, bool) or not isinstance(previous, int) or previous < 0:
                raise ValueError("lease generation is not a non-negative integer")
        except (TypeError, ValueError) as exc:
            raise LeaseConflict(f"Lease {key} generation is corrupt") from exc
        return previous + 1

    def _validated_lease_identity(
        self,
        connection: Any,
        *,
        key: str,
        owner: str,
        token: str,
        expected_generation: int | None = None,
    ) -> int:
        """Validate owner/token/expiry and return a strict positive generation.

        L03's authoritative decoder validates owner/token/expiry but deliberately
        returns the decoded lease for callers that need stronger identity checks.
        Notification fencing requires that stronger contract: missing, boolean,
        non-integer, non-positive, or changed generations are all claim loss.
        """
        row = connection.execute(
            "SELECT value_json FROM reliability_state WHERE key = ?",
            (f"lease:{key}",),
        ).fetchone()

        validate = getattr(self.store, "_validated_lease", None)
        if callable(validate):
            lease = validate(row, key=key, owner=owner, token=token)
            if not isinstance(lease, dict):
                raise LeaseConflict(f"Lease {key} state is corrupt")
        else:
            if not row:
                raise LeaseConflict(f"Lease {key} not found")
            try:
                lease = json.loads(row["value_json"])
                if not isinstance(lease, dict):
                    raise ValueError("lease payload is not an object")
                expires_at = datetime.fromisoformat(str(lease["expires_at"]).replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    raise ValueError("lease expiry must include a timezone")
            except (KeyError, TypeError, ValueError) as exc:
                raise LeaseConflict(f"Lease {key} state is corrupt") from exc
            if lease.get("owner") != owner or lease.get("token") != token:
                raise LeaseConflict(f"Lease {key} token invalid")
            if expires_at.astimezone(UTC) <= datetime.now(UTC):
                raise LeaseConflict(f"Lease {key} expired")

        generation = lease.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise LeaseConflict(f"Lease {key} generation is corrupt")
        if expected_generation is not None and generation != expected_generation:
            raise LeaseConflict(f"Lease {key} generation invalid")
        return generation

    def _guard_notification_persistence(
        self,
        connection: Any,
        *,
        mutation: str,
        section: _FenceSection,
    ) -> None:
        """Validate the exact lease inside the notification write transaction.

        ``NotificationsDal.create_guarded`` calls this only after
        ``BEGIN IMMEDIATE`` and holds that write transaction through dedupe/insert.
        A takeover therefore cannot land between this proof and the mutation.
        """
        if self._fence_section is not section or not section.enforced:
            raise FenceDisciplineError(
                "guarded notification persistence requires the active enforced section"
            )
        if self._guarded_notification_ticket is not None:
            raise FenceDisciplineError(
                "a guarded notification persistence ticket is already active"
            )
        if section.generation is None:
            raise FenceDisciplineError(
                "guarded notification persistence requires an acquired generation"
            )
        self._validated_lease_identity(
            connection,
            key=section.key,
            owner=section.owner,
            token=section.token,
            expected_generation=section.generation,
        )
        self._guarded_notification_ticket = (section, mutation)

    def _record_guarded_notification_persisted(
        self, *, mutation: str, section: _FenceSection
    ) -> None:
        """Record the fence→mutation pair from an authoritative persisted result.

        This is deliberately not a service callback. The guarded DAL first
        returns a complete ``persisted`` result after COMMIT; only then does the
        pipeline consume the atomic ticket established by the in-transaction
        owner/token/generation guard. An unrelated post-commit observer can
        therefore fail without suppressing or fabricating this exact trace.
        """
        expected = (section, mutation)
        if self._guarded_notification_ticket != expected:
            raise FenceDisciplineError(
                f"guarded notification {mutation!r} committed without its atomic ticket"
            )
        if self._fence_section is not section or section.armed is not None:
            raise FenceDisciplineError(
                f"guarded notification {mutation!r} committed outside its clean section"
            )
        self.fence_trace.extend((("fence", mutation), ("mutation", mutation)))
        self._guarded_notification_ticket = None

    def _clear_guarded_notification_ticket(self, *, mutation: str, section: _FenceSection) -> None:
        """Clear a validated ticket after dedupe or failed persistence."""
        if self._guarded_notification_ticket == (section, mutation):
            self._guarded_notification_ticket = None

    # --- Fenced wrappers over the store's own mutating calls.
    #
    # These serve both leased call sites (apply/rollback/reconcile) and unleased
    # ones (human_approve/human_reject, decision persistence). Inside a section
    # they consume a ticket; outside one they pass straight through.

    def _store_transition(
        self, imp_id: str, frm: str, to: str, *, actor: str, detail_json: Any = None
    ) -> None:
        """CAS status transition — a destructive state change, individually fenced."""
        self._spend_fence(f"store:transition:{to}")
        self.store.transition_improvement(imp_id, frm, to, actor=actor, detail_json=detail_json)

    def _store_update_fields(self, imp_id: str, what: str, **fields: Any) -> None:
        """Improvement field update, individually fenced under ``store:update:<what>``."""
        self._spend_fence(f"store:update:{what}")
        self.store.update_improvement_fields(imp_id, **fields)

    def _store_create_agent(self, **kwargs: Any) -> None:
        """Declarative agent creation (B1: never shell), individually fenced."""
        self._spend_fence("store:create_agent")
        self.store.create_agent(**kwargs)

    def _fs_write(self, rel: str, content: str) -> None:
        """Write one declared file, individually fenced (M-01)."""
        self._spend_fence(f"fs:write:{_norm_rel(rel)}")
        abspath = self.repo_root / rel
        abspath.parent.mkdir(parents=True, exist_ok=True)
        abspath.write_text(content, encoding="utf-8")

    def _fs_delete(self, rel: str) -> None:
        """Delete one declared file, individually fenced (M-01)."""
        self._spend_fence(f"fs:delete:{_norm_rel(rel)}")
        abspath = self.repo_root / rel
        if abspath.exists():
            abspath.unlink()

    def activation_status(self) -> ActivationAssessment:
        """Current production activation assessment for this pipeline instance."""
        return assess_pipeline_activation(
            store=self.store,
            sandbox_runner=self.sandbox_runner,
            judge_panel=self.judge_panel,
            force_activation=self.force_activation,
        )

    @property
    def _conn(self) -> Any:
        """The CALLING thread's connection on the backing store, or ``None``.

        Resolved per access rather than captured in ``__init__``: ``SqliteStore``
        hands out one connection per thread, so a handle captured at construction
        time would pin this pipeline to whichever thread built it. Because those
        connections are opened with ``check_same_thread=False``, SQLite would not
        raise — it would interleave this pipeline's ``BEGIN IMMEDIATE`` with
        another thread's transaction and land the commits in the wrong one.
        Returns ``None`` for test doubles that are not SqliteStore-backed, which
        ``_write`` turns into an explicit RuntimeError.
        """
        return getattr(self.store, "_connection", None)

    # --- Clock (injectable for simulated observation windows in tests)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def _now_iso(self) -> str:
        return self._now().strftime("%Y-%m-%dT%H:%M:%SZ")

    # === Phase 1: sandbox + judges + decision ============================

    def sandbox_and_judge(self, imp_id: str) -> DecisionResult:
        """Run sandbox, classify risk on the real diff, judge, then decide (§5).

        Reaches ``awaiting_human`` ONLY after sandbox green + a complete panel attempt
        (or ``panel_blocked`` when < 3 distinct families can be seated). Auto-applies
        only under the full guard set. Every gate is a CAS transition.

        Absent collaborators or unmet L09/L03 activation gates block the path and
        return a visible degraded result without transitioning the improvement (C-03).
        """
        imp = self._require(imp_id)
        if imp.status != ImprovementStatus.PROPOSED.value:
            raise ValueError(f"sandbox_and_judge requires 'proposed', got {imp.status}")
        activation = self.activation_status()
        if not activation.active:
            # Fail closed + operator-visible: stay proposed, never silently no-op.
            self._record_activation_block(imp_id, activation)
            return DecisionResult(
                status="degraded",
                risk_level=int(imp.risk_level or 0),
                requires_human=True,
                reason=",".join(activation.reasons) or "activation_blocked",
            )

        # proposed → testing
        self.store.transition_improvement(
            imp_id,
            ImprovementStatus.PROPOSED.value,
            ImprovementStatus.TESTING.value,
            actor="pipeline",
        )
        sandbox_runner = self.sandbox_runner
        assert sandbox_runner is not None  # activation gate above guarantees presence
        sandbox = sandbox_runner.run(imp, str(self.repo_root))

        # Risk is classified on the AUTHORITATIVE sandbox diff, only ever raised.
        risk = classify_risk(
            sandbox.diff_entries,
            sandbox.declared_paths,
            suggested_level=sandbox.suggested_risk,
            repo_root=self.repo_root,
        )
        self.store.update_improvement_fields(
            imp_id,
            risk_level=risk.level,
            sandbox_json=json.dumps(self._sandbox_json(sandbox, risk)),
        )

        if not sandbox.passed:
            # Failed sandbox ⇒ terminal 'failed', never judged (B1).
            self.store.transition_improvement(
                imp_id,
                ImprovementStatus.TESTING.value,
                ImprovementStatus.FAILED.value,
                actor="pipeline",
                detail_json={"reason": "sandbox_failed", "report": sandbox.report},
            )
            self._notify(imp_id, "escalation", "Sandbox failed", severity="warning")
            return DecisionResult(
                ImprovementStatus.FAILED.value, risk.level, reason="sandbox_failed"
            )

        # testing → judging
        self.store.transition_improvement(
            imp_id,
            ImprovementStatus.TESTING.value,
            ImprovementStatus.JUDGING.value,
            actor="pipeline",
        )
        judge_panel = self.judge_panel
        assert judge_panel is not None  # activation gate above guarantees presence
        panel = judge_panel.evaluate(imp, sandbox)
        self._persist_votes(imp_id, panel)

        if not panel.complete or panel.families_seated < 3:
            # Fail-closed: a complete panel attempt needs 3 distinct families (§5b.7).
            self.store.transition_improvement(
                imp_id,
                ImprovementStatus.JUDGING.value,
                ImprovementStatus.PANEL_BLOCKED.value,
                actor="pipeline",
                detail_json={"families_seated": panel.families_seated},
            )
            self._notify(
                imp_id,
                "approval",
                "Judge panel blocked (needs human pull)",
                severity="warning",
            )
            return DecisionResult(
                ImprovementStatus.PANEL_BLOCKED.value,
                risk.level,
                requires_human=True,
                reason="panel_blocked",
            )

        imp = self._require(imp_id)  # re-read for fresh risk_level/version
        return self._decide(imp, sandbox, panel, risk)

    def _decide(
        self,
        imp: Improvement,
        sandbox: SandboxOutcome,
        panel: JudgeOutcome,
        risk: RiskResult,
    ) -> DecisionResult:
        """Compute quorum + mode and either auto-apply or queue for a human."""
        quorum_passed, requires_human, reason = self._quorum(panel.votes, risk.level)
        mode = self.store.resolve_autonomy(kind=imp.kind)

        auto = (
            mode.mode == AutonomyMode.AUTO.value
            and int(risk.level) <= int(mode.max_auto_risk)
            and int(risk.level) <= int(self.config.max_auto_risk_cap)
            and quorum_passed
            and not requires_human
            and sandbox.passed
            and not risk.tier_p
            and not risk.tier_s
        )

        if auto:
            self.store.transition_improvement(
                imp.id,
                ImprovementStatus.JUDGING.value,
                ImprovementStatus.APPROVED.value,
                actor="pipeline",
                detail_json={"auto": True, "quorum": True, "risk": risk.level},
            )
            result = self.apply(imp.id, decided_by="auto")
            return DecisionResult(
                self._require(imp.id).status,
                risk.level,
                auto_applied=result.applied,
                reason=result.reason or "auto_applied",
            )

        # Approve mode / L3-L4 / rejected / needs_human ⇒ queue for a human.
        self.store.transition_improvement(
            imp.id,
            ImprovementStatus.JUDGING.value,
            ImprovementStatus.AWAITING_HUMAN.value,
            actor="pipeline",
            detail_json={
                "quorum_passed": quorum_passed,
                "requires_human": requires_human,
                "risk": risk.level,
                "reason": reason,
            },
        )
        self._notify(
            imp.id,
            "approval",
            f"Improvement awaiting approval (L{risk.level})",
            severity="warning" if risk.level >= ChangeRisk.L3.value else "info",
        )
        return DecisionResult(
            ImprovementStatus.AWAITING_HUMAN.value,
            risk.level,
            requires_human=True,
            reason=reason,
        )

    def _quorum(self, votes: list[dict[str, Any]], risk_level: int) -> tuple[bool, bool, str]:
        """Quorum per §4: L1 3/3 (2/3 iff allow_majority_l1); L2 3/3; L3/L4 3/3 + human.

        Returns (quorum_passed, requires_human, reason). ``quorum_passed`` is the panel's
        affirmative; ``requires_human`` forces a human regardless of the panel.
        """
        approvals = sum(1 for v in votes if v.get("verdict") in _APPROVE_VERDICTS)
        rejects = sum(1 for v in votes if v.get("verdict") == VerdictKind.REJECT.value)
        needs_human = any(v.get("verdict") == VerdictKind.NEEDS_HUMAN.value for v in votes)

        requires_human = False
        reasons: list[str] = []
        if needs_human:
            requires_human = True
            reasons.append("needs_human_vote")
        if risk_level >= ChangeRisk.L2.value and rejects > 0:
            requires_human = True
            reasons.append("reject_on_l2+")
        if risk_level >= ChangeRisk.L3.value:
            requires_human = True
            reasons.append("l3+_always_human")

        if risk_level <= ChangeRisk.L1.value:
            needed = 2 if self.config.allow_majority_l1 else 3
            quorum = approvals >= needed and rejects == 0 and not needs_human
        else:
            # L2/L3/L4 require unanimous 3/3.
            quorum = approvals >= 3 and rejects == 0 and not needs_human

        return quorum, requires_human, ",".join(reasons)

    # === Human decision helpers (W7 may call the store directly instead) =====

    def human_approve(self, imp_id: str, decided_by: str) -> ApplyResult:
        """awaiting_human → approved (records decided_by), then apply."""
        self.store.update_improvement_fields(imp_id, decided_by=decided_by)
        self.store.transition_improvement(
            imp_id,
            ImprovementStatus.AWAITING_HUMAN.value,
            ImprovementStatus.APPROVED.value,
            actor=f"human:{decided_by}",
        )
        return self.apply(imp_id, decided_by=decided_by)

    def human_reject(self, imp_id: str, decided_by: str) -> None:
        """awaiting_human → rejected (terminal)."""
        self.store.update_improvement_fields(imp_id, decided_by=decided_by)
        self.store.transition_improvement(
            imp_id,
            ImprovementStatus.AWAITING_HUMAN.value,
            ImprovementStatus.REJECTED.value,
            actor=f"human:{decided_by}",
        )
        self._notify(imp_id, "info", "Improvement rejected", severity="info")

    def human_pull_panel_blocked(self, imp_id: str, decided_by: str) -> None:
        """panel_blocked → awaiting_human (explicit human pull, decision recorded, §5b.7)."""
        self.store.transition_improvement(
            imp_id,
            ImprovementStatus.PANEL_BLOCKED.value,
            ImprovementStatus.AWAITING_HUMAN.value,
            actor=f"human:{decided_by}",
            detail_json={"pulled_by": decided_by},
        )
        self._notify(imp_id, "approval", "Panel-blocked item pulled for review")

    # === Phase 2: apply (journaled, leased, single-flight) ===============

    def apply(self, imp_id: str, decided_by: str = "auto") -> ApplyResult:
        """Apply an approved improvement in the main tree (§5, §5b.2/3, M2).

        Single-flight via a generation-fenced lease. Deferred (left ``approved``) if the
        apply lease is held or the diff overlaps a still-monitoring improvement's paths.
        The fencing token is asserted immediately before every file/git/database/status
        mutation so a displaced owner cannot interleave destructive operations (M-01).
        """
        imp = self._require(imp_id)
        if imp.status != ImprovementStatus.APPROVED.value:
            raise ValueError(f"apply requires 'approved', got {imp.status}")

        try:
            token = self._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=1800)
        except LeaseConflict:
            # Another apply/rollback is executing — defer, stay approved (single-flight).
            return ApplyResult(deferred=True, reason="apply_lease_held")

        try:
            with self._fenced_section(_APPLY_LEASE_KEY, token):
                touched = self._touched_paths(imp)
                for other in self.store.list_improvements(
                    status=ImprovementStatus.MONITORING.value
                ):
                    if other.id == imp_id:
                        continue
                    if touched & self._touched_paths(other):
                        return ApplyResult(deferred=True, reason="overlap_with_monitoring")

                # approved → applying (CAS), individually fenced (M-01).
                with self._fenced_mutation(f"store:transition:{ImprovementStatus.APPLYING.value}"):
                    self._store_transition(
                        imp_id,
                        ImprovementStatus.APPROVED.value,
                        ImprovementStatus.APPLYING.value,
                        actor=f"pipeline:{decided_by}",
                    )
                return self._journaled_apply(self._require(imp_id), token)
        finally:
            self.store.release_lease(_APPLY_LEASE_KEY, self.owner, token)

    def _journaled_apply(self, imp: Improvement, token: str) -> ApplyResult:
        """Execute the apply through journal phases with a git commit trailer.

        Runs inside the caller's fenced section: every destructive step below arms
        its OWN fresh ``assert_lease`` and consumes it, so a displaced owner is
        rejected at the next step rather than after the group (M-01). Renewals
        before lengthy phases extend runway but never substitute for a fence.
        """
        imp_id = imp.id
        attempt = int(imp.attempt) + 1
        attempt_id = new_id("app")
        pre_sha = self._git_head()

        with self._fenced_mutation("store:update:attempt"):
            self._store_update_fields(
                imp_id, "attempt", attempt=attempt, stage_started_at=self._now_iso()
            )

        journal = {
            "phase": "prepared",
            "pre_sha": pre_sha,
            "attempt": attempt,
            "attempt_id": attempt_id,
        }
        self._journal_put(imp_id, journal)

        rp_id = self._create_rollback_point(imp_id, "git", pre_sha)
        with self._fenced_mutation("store:update:rollback_point_id"):
            self._store_update_fields(imp_id, "rollback_point_id", rollback_point_id=rp_id)

        # Capture the pre-apply baseline for the observation window.
        # Use the same risk-level window the post-apply monitor will use (H-24).
        self._monitor_baseline_put(
            imp_id, self._measure_baseline(risk_level=int(imp.risk_level or 0))
        )

        # Renew before file mutations which may be lengthy (M-01).
        self._renew_fence(_APPLY_LEASE_KEY, token)

        # Each declared file/config/DB mutation fences itself individually.
        written = self._apply_mutations(imp)
        journal["phase"] = "files_written"
        journal["written"] = written
        self._journal_put(imp_id, journal)

        # Renew before git commit to ensure we have enough lease runway (M-01).
        self._renew_fence(_APPLY_LEASE_KEY, token)

        message = f"reliability: apply {imp_id}\n\nImp-Id: {imp_id} Attempt: {attempt}"
        # git add and git commit fence separately inside _git_commit.
        sha = self._git_commit(written, message)
        journal["phase"] = "committed"
        journal["applied_sha"] = sha
        self._journal_put(imp_id, journal)

        self._record_applied(imp_id, sha)
        journal["phase"] = "recorded"
        self._journal_put(imp_id, journal)
        self._notify(imp_id, "done", "Improvement applied", severity="info")

        # restart_required: launchctl kickstart api/dashboard only, auto+L1 only.
        self._maybe_restart(imp)

        self._enter_monitoring(imp_id, imp.risk_level)
        return ApplyResult(applied=True, applied_sha=sha)

    def _record_applied(self, imp_id: str, sha: str) -> None:
        """Record the applied SHA, tag it, and advance to ``applied``.

        Three distinct destructive steps — a DB field update, a git tag write, and
        a CAS transition — each with its own fresh fence assertion (M-01).
        """
        with self._fenced_mutation("store:update:applied_sha"):
            self._store_update_fields(
                imp_id, "applied_sha", applied_sha=sha, applied_at=self._now_iso()
            )
        # Self-fencing, and only when the tag does not already exist.
        self._git_tag_idempotent(f"imp/{imp_id}", sha)
        with self._fenced_mutation(f"store:transition:{ImprovementStatus.APPLIED.value}"):
            self._store_transition(
                imp_id,
                ImprovementStatus.APPLYING.value,
                ImprovementStatus.APPLIED.value,
                actor="pipeline",
                detail_json={"applied_sha": sha},
            )

    def _enter_monitoring(self, imp_id: str, risk_level: int) -> None:
        """Set the observation deadline and advance to ``monitoring`` (two fences)."""
        hours = self.config.observation_hours(risk_level)
        monitor_until = (self._now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._fenced_mutation("store:update:monitor_until"):
            self._store_update_fields(imp_id, "monitor_until", monitor_until=monitor_until)
        with self._fenced_mutation(f"store:transition:{ImprovementStatus.MONITORING.value}"):
            self._store_transition(
                imp_id,
                ImprovementStatus.APPLIED.value,
                ImprovementStatus.MONITORING.value,
                actor="pipeline",
                detail_json={"monitor_until": monitor_until},
            )

    # === Crash recovery (restart reconciliation, §5b.2, M2) ==============

    def reconcile_apply(self, imp_id: str) -> ApplyResult:
        """Resume an improvement stuck in ``applying`` after a crash.

        Commit with the trailer found in ``git log`` ⇒ the commit landed: advance
        (record SHA + monitoring). Not found ⇒ restore the worktree from ``pre_sha``
        and retreat (approved for a bounded retry, else failed + escalation).

        Reconcile must never reset/clean the worktree without holding a valid fenced
        lease — a displaced owner performing reset/clean would corrupt the working
        tree of the current owner (M-01).
        """
        imp = self._require(imp_id)
        if imp.status != ImprovementStatus.APPLYING.value:
            return ApplyResult(reason="not_applying")

        journal = self._journal_get(imp_id) or {}
        attempt = int(journal.get("attempt", imp.attempt) or imp.attempt)
        sha = self._git_find_trailer(imp_id, attempt)

        if sha:
            # Commit landed — advance path needs no destructive git ops, but still
            # requires a fence for DB mutations.
            try:
                token = self._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=1800)
            except LeaseConflict:
                return ApplyResult(deferred=True, reason="apply_lease_held")
            try:
                with self._fenced_section(_APPLY_LEASE_KEY, token):
                    self._record_applied(imp_id, sha)
                    self._journal_put(imp_id, {**journal, "phase": "recorded", "applied_sha": sha})
                    self._notify(imp_id, "done", "Improvement applied (recovered)", severity="info")
                    self._enter_monitoring(imp_id, imp.risk_level)
                return ApplyResult(applied=True, applied_sha=sha, reason="recovered_advance")
            finally:
                self.store.release_lease(_APPLY_LEASE_KEY, self.owner, token)

        # Commit never landed — must acquire a fenced lease BEFORE any reset/clean (M-01).
        try:
            token = self._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=1800)
        except LeaseConflict:
            # Cannot acquire — fail closed, do not restore without valid fence.
            return ApplyResult(deferred=True, reason="apply_lease_held_for_restore")

        try:
            with self._fenced_section(_APPLY_LEASE_KEY, token):
                pre_sha = journal.get("pre_sha")
                if pre_sha:
                    # reset and clean fence separately inside _git_restore_clean (M-01).
                    self._git_restore_clean(pre_sha)

                if attempt < _MAX_APPLY_ATTEMPTS:
                    with self._fenced_mutation(
                        f"store:transition:{ImprovementStatus.APPROVED.value}"
                    ):
                        self._store_transition(
                            imp_id,
                            ImprovementStatus.APPLYING.value,
                            ImprovementStatus.APPROVED.value,
                            actor="pipeline:recover",
                            detail_json={"retreat": True, "attempt": attempt},
                        )
                    return ApplyResult(reason="recovered_retreat")

                with self._fenced_mutation(f"store:transition:{ImprovementStatus.FAILED.value}"):
                    self._store_transition(
                        imp_id,
                        ImprovementStatus.APPLYING.value,
                        ImprovementStatus.FAILED.value,
                        actor="pipeline:recover",
                        detail_json={
                            "retreat": True,
                            "attempt": attempt,
                            "exhausted": True,
                        },
                    )
                self._notify(
                    imp_id, "escalation", "Apply failed after retries", severity="critical"
                )
                return ApplyResult(reason="recovered_failed")
        finally:
            self.store.release_lease(_APPLY_LEASE_KEY, self.owner, token)

    # === Phase 3: monitoring + auto-rollback (M1) ========================

    def monitor_tick(self, imp_id: str) -> str:
        """Evaluate one monitoring cycle. Returns the resulting status.

        Compares baseline vs current failure signals using BOTH events AND the raw
        detector-independent signal. Divergence (raw up, events flat/down) ⇒ critical
        alert (M1). Regression ⇒ auto-rollback + escalation. Clean at window end ⇒
        confirmed.

        Comparisons use **normalized rates over comparable exposure** (H-24), not
        unequal absolute counts (e.g. 72h baseline vs a short post-apply window).
        """
        imp = self._require(imp_id)
        if imp.status != ImprovementStatus.MONITORING.value:
            return imp.status

        baseline = self._monitor_baseline_get(imp_id) or {}
        applied_at = imp.applied_at or baseline.get("captured_at")
        after = self._measure_window(applied_at, self._now_iso())
        before_hours = float(baseline.get("hours") or 0.0)
        if before_hours <= 0:
            # Backward-compat for baselines captured before H-24: treat missing
            # exposure as 1.0 so equal absolute counts remain comparable.
            before_hours = 1.0
        before = _Signals(
            events=int(baseline.get("events", 0) or 0),
            raw=int(baseline.get("raw", 0) or 0),
            hours=before_hours,
        )
        after_hours = after.hours
        if after_hours <= 0:
            after_hours = self._window_hours(applied_at, self._now_iso()) or 1.0
            after = _Signals(events=after.events, raw=after.raw, hours=after_hours)

        reg = self.config.regression_fraction()
        div = self.config.divergence_fraction()
        before_raw_rate = before.rate("raw")
        after_raw_rate = after.rate("raw")
        before_event_rate = before.rate("events")
        after_event_rate = after.rate("events")

        raw_up = after_raw_rate > before_raw_rate * (1.0 + reg)
        events_up = after_event_rate > before_event_rate * (1.0 + reg)

        # Divergence (M1): raw failure *rate* rose while detector event rate did not.
        if after_raw_rate > before_raw_rate * (1.0 + div) and after_event_rate <= before_event_rate:
            self._notify(
                imp_id,
                "alert",
                "Monitoring divergence: raw failure rate up while event rate flat",
                severity="critical",
                dedupe=False,
                payload={
                    "before": before.__dict__,
                    "after": after.__dict__,
                    "before_raw_rate": before_raw_rate,
                    "after_raw_rate": after_raw_rate,
                    "before_event_rate": before_event_rate,
                    "after_event_rate": after_event_rate,
                },
            )

        worse = raw_up or events_up
        if worse:
            rb = self.rollback(imp_id, reason="kpi_regression")
            return (
                self._require(imp_id).status if not rb.conflict else ImprovementStatus.FAILED.value
            )

        if self._now_iso() >= (imp.monitor_until or self._now_iso()):
            self.store.update_improvement_fields(imp_id, resolved_at=self._now_iso())
            self.store.transition_improvement(
                imp_id,
                ImprovementStatus.MONITORING.value,
                ImprovementStatus.CONFIRMED.value,
                actor="pipeline",
                detail_json={
                    "before": before.__dict__,
                    "after": after.__dict__,
                    "before_raw_rate": before_raw_rate,
                    "after_raw_rate": after_raw_rate,
                },
            )
            self._notify(imp_id, "done", "Improvement confirmed", severity="info")
            return ImprovementStatus.CONFIRMED.value

        return ImprovementStatus.MONITORING.value

    # === Rollback (journaled, clean-abort on conflict, M2) ===============

    def rollback(self, imp_id: str, reason: str, decided_by: str = "auto") -> RollbackResult:
        """Revert an applied improvement's commit. Conflict ⇒ clean abort + escalate.

        The fencing token is asserted immediately before every git/database mutation
        so a displaced owner cannot interleave destructive operations (M-01).
        """
        imp = self._require(imp_id)
        if imp.status not in (
            ImprovementStatus.MONITORING.value,
            ImprovementStatus.APPLIED.value,
        ):
            raise ValueError(f"rollback requires monitoring/applied, got {imp.status}")
        if not imp.applied_sha:
            raise ValueError("cannot rollback: no applied_sha recorded")

        try:
            token = self._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=1800)
        except LeaseConflict:
            return RollbackResult(reason="apply_lease_held")

        try:
            with self._fenced_section(_APPLY_LEASE_KEY, token):
                with self._fenced_mutation(
                    f"store:transition:{ImprovementStatus.ROLLING_BACK.value}"
                ):
                    self._store_transition(
                        imp_id,
                        imp.status,
                        ImprovementStatus.ROLLING_BACK.value,
                        actor=f"pipeline:{decided_by}",
                        detail_json={"reason": reason, "revert_of": imp.applied_sha},
                    )
                pre_revert = self._git_head()

                self._journal_put(
                    f"rollback:{imp_id}",
                    {
                        "phase": "reverting",
                        "revert_of": imp.applied_sha,
                        "pre_revert": pre_revert,
                    },
                )

                # Renew before git revert which may be lengthy (M-01).
                self._renew_fence(_APPLY_LEASE_KEY, token)

                message = f"reliability: rollback {imp_id}\n\nImp-Revert: {imp_id}"
                # revert and its commit fence separately inside _git_revert.
                ok = self._git_revert(imp.applied_sha, message)
                if not ok:
                    # Conflict — abort CLEANLY, no partial revert, restore, escalate.
                    # abort/reset/clean fence separately inside _git_revert_abort.
                    self._git_revert_abort(pre_revert)
                    with self._fenced_mutation(
                        f"store:transition:{ImprovementStatus.FAILED.value}"
                    ):
                        self._store_transition(
                            imp_id,
                            ImprovementStatus.ROLLING_BACK.value,
                            ImprovementStatus.FAILED.value,
                            actor=f"pipeline:{decided_by}",
                            detail_json={"reason": "revert_conflict"},
                        )
                    self._notify(
                        imp_id,
                        "escalation",
                        "Rollback revert conflict — aborted cleanly, needs human",
                        severity="critical",
                        dedupe=False,
                    )
                    return RollbackResult(conflict=True, reason="revert_conflict")

                revert_sha = self._git_head()

                with self._fenced_mutation("store:update:resolved_at"):
                    self._store_update_fields(imp_id, "resolved_at", resolved_at=self._now_iso())
                # Self-fencing restoration stamp on the rollback point (M-01).
                self._mark_rollback_restored(imp_id)
                with self._fenced_mutation(
                    f"store:transition:{ImprovementStatus.ROLLED_BACK.value}"
                ):
                    self._store_transition(
                        imp_id,
                        ImprovementStatus.ROLLING_BACK.value,
                        ImprovementStatus.ROLLED_BACK.value,
                        actor=f"pipeline:{decided_by}",
                        detail_json={"revert_sha": revert_sha, "reason": reason},
                    )
                self._notify(
                    imp_id,
                    "escalation",
                    "Improvement rolled back after regression",
                    severity="critical",
                    dedupe=False,
                )
                return RollbackResult(success=True, revert_sha=revert_sha)
        finally:
            self.store.release_lease(_APPLY_LEASE_KEY, self.owner, token)

    # === Mutations (B1: declared diffs / config / DB ops ONLY) ===========

    def _apply_mutations(self, imp: Improvement) -> list[str]:
        """Apply ONLY declared file diffs, schema-validated config_edits, DB ops.

        NEVER executes ``proposal['plan']`` or any shell — those are narrative (B1).

        Each declared file is a separate destructive mutation and therefore arms
        its own fresh fence assertion: a lease stolen after the third of ten file
        writes must stop the fourth, not be discovered only at the end (M-01).
        """
        proposal = imp.proposal_json or {}
        written: list[str] = []

        for f in proposal.get("files", []) or []:
            rel = f.get("path")
            if not rel:
                continue
            self._guard_in_repo(rel)
            action = (f.get("action") or "modify").lower()
            if action == "delete":
                with self._fenced_mutation(f"fs:delete:{_norm_rel(rel)}"):
                    self._fs_delete(rel)
                written.append(rel)
            elif "content" in f:
                with self._fenced_mutation(f"fs:write:{_norm_rel(rel)}"):
                    self._fs_write(rel, f["content"])
                written.append(rel)
            elif "diff" in f and f["diff"]:
                # Self-fencing.
                self._git_apply(f["diff"])
                written.append(rel)

        for edit in proposal.get("config_edits", []) or []:
            rel = self._apply_config_edit(edit)
            if rel:
                written.append(rel)

        # skill/agent DB ops are declarative store calls, never shell.
        change_type = (proposal.get("change_type") or "").lower()
        if change_type in ("new_agent", "agent", "skill"):
            self._apply_db_op(imp, proposal)

        return written

    def _apply_config_edit(self, edit: dict[str, Any]) -> str | None:
        """Schema-validated config edit: set dotted keys in a configs/ yaml file."""
        rel = edit.get("path")
        if not rel:
            return None
        self._guard_in_repo(rel)
        if not _norm_rel(rel).startswith("configs/"):
            raise ValueError(f"config_edit path must live under configs/: {rel}")
        sets = edit.get("set")
        if not isinstance(sets, dict):
            raise ValueError("config_edit requires a 'set' mapping")

        import yaml

        abspath = self.repo_root / rel
        data: dict[str, Any] = {}
        if abspath.exists():
            data = yaml.safe_load(abspath.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"config target is not a mapping: {rel}")
        for dotted, value in sets.items():
            if not _is_json_scalar(value):
                raise ValueError(f"config_edit value not JSON-safe: {dotted}")
            _set_dotted(data, str(dotted), value)
        # Validation and the read above are non-destructive; only the write is
        # fenced, and it arms its own assertion (M-01).
        with self._fenced_mutation(f"fs:write:{_norm_rel(rel)}"):
            self._fs_write(rel, yaml.safe_dump(data, sort_keys=True))
        return rel

    def _apply_db_op(self, imp: Improvement, proposal: dict[str, Any]) -> None:
        """Declarative skill/agent DB op via the store (never shell). Minimal by design.

        The concrete new-agent/skill authoring lives in W6/W5; the pipeline only
        performs a declared, schema-shaped store write when the proposal carries one.
        """
        design = proposal.get("agent") or proposal.get("design")
        if (
            (proposal.get("change_type") or "").lower() in ("new_agent", "agent")
            and isinstance(design, dict)
            and design.get("name")
        ):
            with self._fenced_mutation("store:create_agent"):
                self._store_create_agent(
                    name=design["name"],
                    org_unit_id=design.get("org_unit_id"),
                    org_role=design.get("org_role", "specialist"),
                    title=design.get("title", ""),
                    charter=design.get("charter", ""),
                    model=design.get("model"),
                    harness=design.get("harness"),
                )

    def _guard_in_repo(self, rel: str) -> None:
        target = (self.repo_root / rel).resolve()
        root = self.repo_root.resolve()
        if inode_relative_parts_anchored(target, root) is None:
            raise ValueError(f"refusing mutation outside repo: {rel}")

    # === Restart (launchctl kickstart api/dashboard only) ================

    def _maybe_restart(self, imp: Improvement) -> None:
        proposal = imp.proposal_json or {}
        if not proposal.get("restart_required"):
            return
        mode = self.store.resolve_autonomy(kind=imp.kind)
        # Only auto mode at L1 may auto-restart; api/dashboard only, never runner.
        if mode.mode != AutonomyMode.AUTO.value or int(imp.risk_level) != ChangeRisk.L1.value:
            self._notify(
                imp.id,
                "info",
                "Restart required — deferred to human (not auto-L1)",
                severity="info",
            )
            return
        for label in ("com.omniagentos.api", "com.omniagentos.dashboard"):
            self._launchctl_kickstart(label)

    def _launchctl_kickstart(self, label: str) -> None:
        """Restart one service. Each label is its own fenced mutation (M-01)."""
        try:
            uid = os.getuid()  # type: ignore[attr-defined]
            with self._fenced_mutation(f"system:launchctl:{label}"):
                self._spend_fence(f"system:launchctl:{label}")
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except FenceDisciplineError:
            raise
        except Exception:  # noqa: BLE001 - restart is best-effort; failure notifies below
            self._notify(
                None, "escalation", f"launchctl kickstart failed: {label}", severity="warning"
            )

    # === Signals (both detector events AND raw runs/steps, M1) ===========

    def _measure_baseline(self, risk_level: int | None = None) -> dict[str, Any]:
        """Capture pre-apply failure signals over the *matching* observation window.

        Uses the risk-level observation hours (not always the global max) so the
        baseline exposure is comparable to the post-apply monitoring window (H-24).
        """
        end = self._now_iso()
        if risk_level is None:
            hours = float(max(self.config.observation_windows_hours.values()))
        else:
            hours = float(self.config.observation_hours(int(risk_level)))
        start = (self._now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sig = self._measure_window(start, end)
        return {
            "events": sig.events,
            "raw": sig.raw,
            "hours": hours,
            "captured_at": end,
        }

    def _window_hours(self, start: str | None, end: str) -> float:
        """Exposure length in hours for rate normalization; 0 when unknown."""
        if not start:
            return 0.0
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            seconds = (e - s).total_seconds()
            return max(seconds / 3600.0, 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _measure_window(self, start: str | None, end: str) -> _Signals:
        """Failure signals in [start, end): detector events AND raw runs/steps."""
        if not start:
            return _Signals()
        hours = self._window_hours(start, end)
        events = self._read_scalar(
            "SELECT COUNT(*) AS c FROM reliability_events "
            "WHERE detected_at >= ? AND detected_at < ? "
            "AND severity IN ('warning','critical')",
            (start, end),
        )
        raw_runs = self._read_scalar(
            "SELECT COUNT(*) AS c FROM runs "
            "WHERE (error IS NOT NULL OR state = 'failed') "
            "AND COALESCE(finished_at, updated_at) >= ? "
            "AND COALESCE(finished_at, updated_at) < ?",
            (start, end),
        )
        raw_steps = self._read_scalar(
            "SELECT COUNT(*) AS c FROM steps s JOIN runs r ON s.run_id = r.id "
            "WHERE s.status = 'failed' "
            "AND COALESCE(s.finished_at, r.updated_at) >= ? "
            "AND COALESCE(s.finished_at, r.updated_at) < ?",
            (start, end),
        )
        return _Signals(events=events, raw=raw_runs + raw_steps, hours=hours)

    # === Raw reliability-table access (no frozen-store method exists) =====

    def _touched_paths(self, imp: Improvement) -> set[str]:
        sj = imp.sandbox_json or {}
        paths = set(sj.get("diff_paths", []) or [])
        paths.update(sj.get("declared_paths", []) or [])
        return {_norm_rel(p) for p in paths}

    def _journal_key(self, imp_id: str) -> str:
        return f"apply_journal:{imp_id}"

    def _journal_put(self, key_or_id: str, journal: dict[str, Any]) -> None:
        """Write one journal phase. Self-fencing: arms and spends its own ticket."""
        key = (
            key_or_id
            if key_or_id.startswith(("apply_journal:", "rollback:"))
            else self._journal_key(key_or_id)
        )
        with self._fenced_mutation("state:journal_put"):
            self._state_put(key, journal, mutation="state:journal_put")

    def _journal_get(self, imp_id: str) -> dict[str, Any] | None:
        return self._state_get(self._journal_key(imp_id))

    def _monitor_baseline_put(self, imp_id: str, baseline: dict[str, Any]) -> None:
        """Persist the pre-apply baseline. Self-fencing (M-01)."""
        with self._fenced_mutation("state:monitor_baseline_put"):
            self._state_put(
                f"monitor_baseline:{imp_id}",
                baseline,
                mutation="state:monitor_baseline_put",
            )

    def _monitor_baseline_get(self, imp_id: str) -> dict[str, Any] | None:
        return self._state_get(f"monitor_baseline:{imp_id}")

    def _state_put(self, key: str, value: dict[str, Any], *, mutation: str | None = None) -> None:
        """Upsert one ``reliability_state`` row.

        ``mutation`` names the fence ticket this write consumes. ``None`` marks an
        unleased call site such as activation-block bookkeeping. Leased callers,
        including notification-failure bookkeeping, must always name and consume
        an independently asserted mutation ticket.
        """
        now = utc_now_iso()
        self._write(
            "INSERT INTO reliability_state (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), now),
            mutation=mutation,
        )

    def _state_get(self, key: str) -> dict[str, Any] | None:
        row = self._read_one("SELECT value_json FROM reliability_state WHERE key = ?", (key,))
        if not row:
            return None
        return json.loads(row["value_json"])

    def _create_rollback_point(self, imp_id: str, kind: str, git_ref: str) -> str:
        """Insert the pre-apply rollback point. Self-fencing (M-01)."""
        rp_id = new_id("rbp")
        with self._fenced_mutation("state:rollback_point_create"):
            self._write(
                "INSERT INTO rollback_points (id, improvement_id, kind, git_ref, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rp_id, imp_id, kind, git_ref, "pre-apply immutable SHA", utc_now_iso()),
                mutation="state:rollback_point_create",
            )
        return rp_id

    def _mark_rollback_restored(self, imp_id: str) -> None:
        """Stamp the rollback point as restored. Self-fencing (M-01)."""
        with self._fenced_mutation("state:rollback_point_restore"):
            self._write(
                "UPDATE rollback_points SET restored_at = ? WHERE improvement_id = ? AND restored_at IS NULL",
                (utc_now_iso(), imp_id),
                mutation="state:rollback_point_restore",
            )

    # --- Low-level DB helpers (BEGIN IMMEDIATE + busy-retry, store connection)

    def _write(self, sql: str, params: tuple[Any, ...], *, mutation: str | None = None) -> None:
        """Execute one write. Consumes the fence ticket named by ``mutation`` (M-01)."""
        if mutation is not None:
            self._spend_fence(mutation)
        if self._conn is None or self._lock is None:
            raise RuntimeError("pipeline requires a SqliteReliabilityStore-backed store")
        last: Exception | None = None
        for attempt in range(_BUSY_RETRY_ATTEMPTS):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._conn.execute(sql, params)
                        self._conn.commit()
                        return
                    except BaseException:
                        self._conn.rollback()
                        raise
                return
            except Exception as exc:  # noqa: BLE001 - retry only on BUSY/LOCKED
                msg = str(exc)
                if "SQLITE_BUSY" not in msg and "locked" not in msg.lower():
                    raise
                last = exc
                self._backoff(attempt)
        if last:
            raise last

    def _read_one(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self._lock:  # type: ignore[union-attr]
            return self._conn.execute(sql, params).fetchone()

    def _read_scalar(self, sql: str, params: tuple[Any, ...]) -> int:
        row = self._read_one(sql, params)
        if not row:
            return 0
        return int(row["c"] if "c" in row.keys() else row[0])

    @staticmethod
    def _backoff(attempt: int) -> None:
        wait_ms = min(_BUSY_RETRY_BASE_MS * (2**attempt), _BUSY_RETRY_MAX_MS)
        time.sleep(wait_ms / 1000.0)

    # === Git plumbing (real subprocess; tests use tmp git repos) =========

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        mutation: str | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Run one git command.

        A command whose subcommand is in :data:`_DESTRUCTIVE_GIT_SUBCOMMANDS` must
        declare the fence ticket it consumes via ``mutation``. Omitting it raises
        :class:`FenceDisciplineError` rather than silently running unfenced — this
        is the structural guard that catches a destructive git call added later
        without its own assertion (M-01).
        """
        sub = args[0] if args else ""
        if mutation is None:
            if sub in _DESTRUCTIVE_GIT_SUBCOMMANDS and not _is_read_only_git(args):
                raise FenceDisciplineError(
                    f"destructive git command issued without a fence ticket: git {' '.join(args)}"
                )
        else:
            self._spend_fence(mutation)
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def _git_head(self) -> str:
        return self._git(["rev-parse", "HEAD"]).stdout.strip()

    def _git_commit(self, paths: list[str], message: str) -> str:
        """Stage then commit. ``add`` and ``commit`` are SEPARATE mutations (M-01).

        Staging is itself destructive (it rewrites the index), so it arms and
        spends its own fence rather than riding on the commit's.
        """
        add_args = ["add", "--", *paths] if paths else ["add", "-A"]
        with self._fenced_mutation("git:add"):
            self._git(add_args, check=False, mutation="git:add")
        with self._fenced_mutation("git:commit"):
            self._git(["commit", "--no-verify", "-m", message], mutation="git:commit")
        return self._git_head()

    def _git_apply(self, diff_text: str) -> None:
        """Apply one declared diff. Self-fencing (M-01)."""
        with self._fenced_mutation("git:apply"):
            proc = self._git(
                ["apply", "--whitespace=nowarn", "-"],
                check=False,
                mutation="git:apply",
                stdin=diff_text,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"git apply failed: {proc.stderr.strip()}")

    def _git_tag_idempotent(self, tag: str, sha: str) -> None:
        """Create the apply tag if absent. Self-fencing only when it actually writes.

        ``git tag -l`` is a read, so the fence is armed after the existence check —
        otherwise the no-op path would leave an unconsumed ticket.
        """
        existing = self._git(["tag", "-l", tag], check=False).stdout.strip()
        if existing:
            return
        with self._fenced_mutation("git:tag"):
            self._git(["tag", tag, sha], check=False, mutation="git:tag")

    def _git_find_trailer(self, imp_id: str, attempt: int) -> str | None:
        grep = f"Imp-Id: {imp_id} Attempt: {attempt}"
        out = self._git(
            ["log", "--all", "--grep", grep, "-F", "--pretty=%H"], check=False
        ).stdout.strip()
        if not out:
            return None
        return out.splitlines()[0].strip()

    def _git_restore_clean(self, pre_sha: str) -> None:
        """Hard-restore then remove untracked files — TWO separately fenced mutations.

        A lease stolen between the reset and the clean would leave the new owner's
        worktree being scrubbed by the old one, so ``clean`` re-asserts (M-01).
        """
        with self._fenced_mutation("git:reset"):
            self._git(["reset", "--hard", pre_sha], check=False, mutation="git:reset")
        with self._fenced_mutation("git:clean"):
            self._git(["clean", "-fd"], check=False, mutation="git:clean")

    def _git_revert(self, sha: str, message: str) -> bool:
        """Revert then commit — two separately fenced mutations (M-01)."""
        with self._fenced_mutation("git:revert"):
            proc = self._git(
                ["revert", "--no-edit", "--no-commit", sha],
                check=False,
                mutation="git:revert",
            )
        if proc.returncode != 0:
            return False
        with self._fenced_mutation("git:revert_commit"):
            commit = self._git(
                ["commit", "--no-verify", "-m", message],
                check=False,
                mutation="git:revert_commit",
            )
        return commit.returncode == 0

    def _git_revert_abort(self, pre_revert_sha: str) -> None:
        """Abort the in-progress revert and hard-restore — no partial revert.

        Three destructive steps, three fences: abort, reset, clean (M-01).
        """
        with self._fenced_mutation("git:revert_abort"):
            self._git(["revert", "--abort"], check=False, mutation="git:revert_abort")
        with self._fenced_mutation("git:reset"):
            self._git(["reset", "--hard", pre_revert_sha], check=False, mutation="git:reset")
        with self._fenced_mutation("git:clean"):
            self._git(["clean", "-fd"], check=False, mutation="git:clean")

    # === Persistence of judge votes + sandbox summary ====================

    def _persist_votes(self, imp_id: str, panel: JudgeOutcome) -> None:
        summary: dict[str, Any] = {"panel_attempt_id": panel.panel_attempt_id, "verdicts": {}}
        for v in panel.votes:
            self.store.insert_vote(
                improvement_id=imp_id,
                panel_attempt_id=panel.panel_attempt_id,
                judge_agent=v.get("judge_agent", ""),
                model_family=v.get("model_family", ""),
                verdict=v.get("verdict", VerdictKind.NEEDS_HUMAN.value),
                scores_json=v.get("scores"),
                reasoning=v.get("reasoning", ""),
                conditions=v.get("conditions", ""),
                model=v.get("model", ""),
            )
            summary["verdicts"][v.get("model_family", "")] = v.get("verdict")
        self.store.update_improvement_fields(imp_id, votes_summary_json=json.dumps(summary))

    @staticmethod
    def _sandbox_json(sandbox: SandboxOutcome, risk: RiskResult) -> dict[str, Any]:
        return {
            "passed": sandbox.passed,
            "report": sandbox.report,
            "declared_paths": [_norm_rel(p) for p in sandbox.declared_paths],
            "diff_paths": [_norm_rel(e.path) for e in sandbox.diff_entries],
            "risk_level": risk.level,
            "risk_reasons": risk.reasons,
            "risk_tier": risk.tier,
        }

    # === Notifications (§11 policy; every terminal / human-gating transition) ===

    def _record_activation_block(self, imp_id: str, activation: ActivationAssessment) -> None:
        """Persist a durable, operator-visible record that sandbox/judge was blocked."""
        detail = {
            "reason": "activation_blocked",
            "activation": activation.as_dict(),
            "at": self._now_iso(),
        }
        try:
            self.store.update_improvement_fields(
                imp_id,
                last_error_json=json.dumps(detail),
            )
        except Exception:  # noqa: BLE001 - best-effort durable record
            pass
        try:
            self._state_put(f"activation_block:{imp_id}", detail)
        except Exception:  # noqa: BLE001
            pass
        self._notify(
            imp_id,
            "escalation",
            f"Sandbox/judge activation blocked: {','.join(activation.reasons)}",
            severity="warning",
            dedupe=True,
            payload=detail,
        )

    def _notify(
        self,
        imp_id: str | None,
        kind: str,
        title: str,
        *,
        severity: str = "info",
        dedupe: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist/emit one notification as a separately fenced side effect.

        Inside apply/rollback/reconcile, the default notifier validates the exact
        L03 owner/token/generation identity in the same SQLite transaction as
        dedupe/insert. Its fence→mutation trace is appended only after commit.
        An unprovable custom notifier fails closed in production leased sections.
        A persistence failure is followed by two independently fenced degradation
        writes; those writes never recurse through the notifier.
        """
        mutation = f"notify:{kind}"
        section = self._fence_section
        try:
            if section is not None and section.enforced:
                if self._uses_default_notifier:
                    # Preserve L03's public assertion audit, then prove the same
                    # owner/token/generation identity again atomically at the
                    # actual DAL mutation seam.
                    self.store.assert_lease(section.key, section.owner, section.token)
                    self._default_notifier(
                        kind=kind,
                        title=title,
                        severity=severity,
                        imp_id=imp_id,
                        dedupe=dedupe,
                        payload=payload or {},
                        guarded_mutation=mutation,
                        guarded_section=section,
                    )
                    return
                if not self.force_activation:
                    raise FenceDisciplineError(
                        "custom notifier cannot prove atomic persistence fencing "
                        "inside a production leased section"
                    )
                # Test-only compatibility seam. An injected fake may observe the
                # event, but no fence/mutation pair is claimed for unprovable
                # persistence.
                self._notifier(
                    kind=kind,
                    title=title,
                    severity=severity,
                    imp_id=imp_id,
                    dedupe=dedupe,
                    payload=payload or {},
                )
                return
            self._notifier(
                kind=kind,
                title=title,
                severity=severity,
                imp_id=imp_id,
                dedupe=dedupe,
                payload=payload or {},
            )
        except (FenceDisciplineError, LeaseConflict):
            # A stale owner must stop; do not convert fencing loss into a
            # notification degradation record under the displaced token.
            raise
        except Exception as exc:  # noqa: BLE001 - main work continues, failure is durable (M-46)
            self._record_notify_failure(
                imp_id=imp_id,
                kind=kind,
                title=title,
                severity=severity,
                error=str(exc),
            )

    def _record_notify_failure(
        self,
        *,
        imp_id: str | None,
        kind: str,
        title: str,
        severity: str,
        error: str,
    ) -> None:
        """Record M-46 degradation without recursively emitting a notification."""
        failure: dict[str, Any] = {
            "stage": "notify",
            "imp_id": imp_id,
            "kind": kind,
            "title": title,
            "severity": severity,
            "error": error,
            "at": self._now_iso(),
            "durable_targets": [],
            "durability_errors": [],
        }
        self.notify_failures.append(failure)

        key = f"notify_failure:{imp_id or 'none'}:{new_id('nfy')}"
        try:
            with self._fenced_mutation("state:notify_failure"):
                self._state_put(
                    key,
                    failure,
                    mutation="state:notify_failure",
                )
            failure["durable_targets"].append("reliability_state")
        except (FenceDisciplineError, LeaseConflict):
            raise
        except Exception as exc:  # noqa: BLE001 - retain explicit in-memory degradation
            failure["durability_errors"].append({"target": "reliability_state", "error": str(exc)})

        if not imp_id:
            return
        try:
            existing = self._require(imp_id)
            prior = (
                dict(existing.last_error_json or {})
                if isinstance(existing.last_error_json, dict)
                else {}
            )
            prior.setdefault("notify_failures", []).append(failure)
            prior["notify_degraded"] = True
            with self._fenced_mutation("store:update:notify_failure"):
                self._store_update_fields(
                    imp_id,
                    "notify_failure",
                    last_error_json=json.dumps(prior),
                )
            failure["durable_targets"].append("improvements")
        except (FenceDisciplineError, LeaseConflict):
            raise
        except Exception as exc:  # noqa: BLE001 - never recurse through the notifier
            failure["durability_errors"].append({"target": "improvements", "error": str(exc)})

    def _default_notifier(
        self,
        *,
        kind: str,
        title: str,
        severity: str,
        imp_id: str | None,
        dedupe: bool,
        payload: dict[str, Any],
        guarded_mutation: str | None = None,
        guarded_section: _FenceSection | None = None,
    ) -> None:
        from omniagentos.notifications.service import (
            NotificationPersistenceError,
            NotificationPersistenceGuardUnavailable,
            record_notification_result,
        )

        if (guarded_mutation is None) != (guarded_section is None):
            raise FenceDisciplineError("guarded notification requires both mutation and section")
        persistence_guard: Callable[[Any], None] | None = None
        if guarded_mutation is not None and guarded_section is not None:

            def _guard(connection: Any) -> None:
                self._guard_notification_persistence(
                    connection,
                    mutation=guarded_mutation,
                    section=guarded_section,
                )

            persistence_guard = _guard
        try:
            result = record_notification_result(
                kind=kind,
                title=title,
                severity=severity,
                ref_type="improvement" if imp_id else None,
                ref_id=imp_id,
                payload=payload,
                connection=self._conn,
                push=False,
                dedupe=dedupe,
                persistence_guard=persistence_guard,
            )
            if (
                result.status == "persisted"
                and guarded_mutation is not None
                and guarded_section is not None
            ):
                self._record_guarded_notification_persisted(
                    mutation=guarded_mutation,
                    section=guarded_section,
                )
        except NotificationPersistenceGuardUnavailable as exc:
            raise FenceDisciplineError(str(exc)) from exc
        finally:
            if guarded_mutation is not None and guarded_section is not None:
                self._clear_guarded_notification_ticket(
                    mutation=guarded_mutation,
                    section=guarded_section,
                )
        if result.status == "failed":
            raise NotificationPersistenceError(result.error or "notification persistence failed")

    # === Small utilities =================================================

    def _require(self, imp_id: str) -> Improvement:
        imp = self.store.get_improvement(imp_id)
        if imp is None:
            raise ValueError(f"improvement {imp_id} not found")
        return imp


# --- module-level helpers


def _is_read_only_git(args: list[str]) -> bool:
    """Whether a git invocation whose subcommand *can* mutate is a pure read here.

    Only the listing forms are exempted (``tag -l``, ``branch --list``,
    ``stash list``); everything else in :data:`_DESTRUCTIVE_GIT_SUBCOMMANDS`
    requires a fence ticket.
    """
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "tag":
        return "-l" in rest or "--list" in rest
    if sub == "branch":
        return not rest or "--list" in rest or "-l" in rest
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    return False


def _norm_rel(path: str) -> str:
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _is_json_scalar(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_json_scalar(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_scalar(v) for k, v in value.items())
    return False


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = data
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value


__all__ = [
    "ImprovementPipeline",
    "SandboxOutcome",
    "JudgeOutcome",
    "SandboxRunner",
    "JudgePanel",
    "DecisionResult",
    "ApplyResult",
    "RollbackResult",
    "ActivationAssessment",
    "FenceDisciplineError",
    "L03_FENCED_LEASE_CONTRACT",
    "L09_CONTAINMENT_CONTRACT",
    "assess_pipeline_activation",
    "l09_containment_ready",
    "l03_fenced_state_ready",
    "_Signals",
]
