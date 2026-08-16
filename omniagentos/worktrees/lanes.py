"""Per-unit git worktrees for the runner, session and longhaul lanes (T3.8).

WHY THIS EXISTS
===============
All three lanes declare a realm-ROOT claim on the directory they work in (see
``runner/scope_wiring.py``, ``sessions/scope_wiring.py`` and
``LonghaulStore._acquire_attempt_scope``). That claim is honest — a coding agent
pointed at a repo may touch anything in it — and its consequence is that under
``enforce`` a PROJECT is serialized: one run, or one session, or one longhaul
task at a time, per checkout, across the whole fleet.

A private ``git worktree`` is its OWN realm. ``scope.paths.realm_of`` resolves
private-workspace bases BEFORE it asks git for a toplevel, so a worktree under a
lane's ``<var root>/worktrees`` base keys to itself rather than folding into the
enclosing checkout. Move each unit into its own worktree and the same realm-root
claim becomes a claim on a directory nobody else can name: a NO-CONTENTION
claim. Only the merge back into the shared workspace is contended, and that is
one short critical section under the realm's commit lock instead of a lock held
for the whole life of a run.

THE BASE MUST BE REGISTERED OR THIS IS WORSE THAN NOTHING
=========================================================
``private_workspace_bases()`` ships with ``var/runs`` (depth 1),
``var/intake-workspace``, ``var/swarm/worktrees`` (depth 2) and ``var/projects``.
It does NOT know about the three bases here, and the two failure modes of that
are not symmetric:

* ``<var>/runs/worktrees/<run_id>/main`` sits under the depth-1 ``var/runs``
  base, so without registration EVERY runner worktree keys to the single realm
  ``<var>/runs/worktrees`` — total serialization, i.e. the exact disease this
  work package treats;
* ``<var>/sessions/worktrees/...`` and ``<var>/longhaul/worktrees/...`` sit
  under no private base at all, so they fold into the OmniAgentOS repo's git
  toplevel — every session worktree would then conflict with the REPO ITSELF and
  with every other lane working in it.

:meth:`LaneWorktrees.register_realm_base` is therefore called before any path
this module produces is handed to ``realm_of``, and it is called from every
entry point that can be first (activation, and the lock-acquire path of a lane
that was handed a recorded worktree by a previous process).

MODE IS RECORDED, NOT AMBIENT
=============================
:meth:`LaneWorktrees.resolve_mode` mirrors ``swarm.scheduler._resolve_worktree_mode``
(the m6 fix) exactly: the mode is resolved ONCE per unit, at first activation,
persisted by the caller, and from then on the RECORDED value wins over the
ambient env/config on every later read. Without that, a daemon restarted with a
different flag flips a live unit's mode mid-flight — and here that is worse than
it was for swarm, because the unit's LOCKS were sized for the other mode: a run
that claimed a private worktree realm and then resumes in same-directory mode is
writing the shared project while holding a claim on a directory nobody is using.

SHIPS DARK
==========
Every flag defaults to 0 (:func:`lane_worktrees_enabled`), construction of
:class:`LaneWorktrees` reads NOTHING (no env, no config, no realpath, no git —
the var root and the subprocess seam both resolve lazily), and with the flag off
:meth:`LaneWorktrees.activate` returns an inert activation whose ``working_dir``
is the caller's own base dir. ``tests/worktrees/`` proves it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from omniagentos.scope.config import parallelism_config
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.paths import ScopePathError, realm_of, register_private_base
from omniagentos.worktrees.git import (
    MergeOutcome,
    RemoveOutcome,
    SubprocessWorktrees,
    WorktreeInfo,
    WorktreesProto,
)

LOG = logging.getLogger(__name__)

__all__ = [
    "DEP_LINK_DIRS",
    "LANE_SPECS",
    "MODE_KEY",
    "SAME_DIR_MODE",
    "UNIT_KEY",
    "WORKTREE_MODE",
    "WORKTREE_RECORD_KEY",
    "Activation",
    "Integration",
    "LaneName",
    "LaneSpec",
    "LaneWorktree",
    "LaneWorktrees",
    "ModeDecision",
    "lane_spec",
    "lane_var_root",
    "lane_worktrees",
    "lane_worktrees_enabled",
    "recorded_mode",
    "worktree_from_record",
]

LaneName = Literal["runner", "session", "longhaul"]

#: The single unit-key every lane here uses. ``SubprocessWorktrees`` is keyed on
#: ``(owner_id, unit_key)`` because swarm fans ONE run out into many tasks; these
#: three lanes have exactly one worktree per unit, so the unit id is the owner and
#: the key is a constant. It is not ``""`` because ``safe_component`` (rightly)
#: refuses an empty component.
UNIT_KEY = "main"

#: Gitignored, read-mostly dependency dirs symlinked into a fresh worktree when
#: they exist in the base dir (a worktree checkout carries TRACKED files only, so
#: without this a JS/Rust project's first command in a new worktree fails).
#:
#: Deliberately a constant rather than a read of ``configs/swarm.yaml``'s
#: ``worktrees.dep_link_dirs``: that file is one lane's configuration and these
#: are three other lanes. ``.venv``/``venv`` stay out for the reason
#: ``swarm.worktrees`` documents — python venvs embed absolute paths that do not
#: survive being symlinked under another root.
DEP_LINK_DIRS: tuple[str, ...] = ("node_modules", "vendor", "target")

#: ``scope_json.mode`` — the RECORDED registration. See the module docstring.
MODE_KEY = "mode"
WORKTREE_MODE = "worktree"
SAME_DIR_MODE = "same_dir"

#: The key the whole record lives under inside a lane's declaration blob
#: (``runs.scope_json`` / ``sessions.scope_json`` / ``board_tasks.longhaul_json``).
WORKTREE_RECORD_KEY = "worktree"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

#: How long :meth:`LaneWorktrees.integrate` waits out ANOTHER process's commit
#: lock on the shared realm. Same bounds argument as ``swarm.scope_wiring``'s
#: default: comfortably above the 90s lock TTL, so a holder that died mid-merge
#: always lapses inside the window and nobody raises over a corpse.
DEFAULT_MERGE_WAIT_S = 100.0
DEFAULT_MERGE_POLL_S = 0.25


# ---------------------------------------------------------------------------
# Lane bindings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaneSpec:
    """One lane's binding of the shared worktree mechanism.

    ``var_subdir`` is a component of ``<var>``, so the three layouts are

    * runner   — ``var/runs/worktrees/<run_id>/main``       on ``run/<run_id>/main``
    * session  — ``var/sessions/worktrees/<ses_id>/main``   on ``session/<ses_id>/main``
    * longhaul — ``var/longhaul/worktrees/<task_id>/main``  on ``longhaul/<task_id>/main``

    The branch carries the ``/main`` unit component because
    ``SubprocessWorktrees.branch_name`` composes ``<namespace>/<owner>/<unit>``
    and that mechanism is frozen for this work package; every consumer of the
    branch (``branch_prefix``, ``list_run_worktrees``, ``delete_run_branches``)
    is built on the same three-part shape, so shortening it here would have meant
    forking the algebra rather than binding it.
    """

    lane: LaneName
    namespace: str
    env: str
    config_key: str
    var_subdir: str
    unit_key: str = UNIT_KEY


LANE_SPECS: dict[str, LaneSpec] = {
    "runner": LaneSpec(
        lane="runner",
        namespace="run",
        env="OMNIAGENTOS_WORKTREES_RUNNER",
        config_key="runner",
        var_subdir="runs",
    ),
    "session": LaneSpec(
        lane="session",
        namespace="session",
        env="OMNIAGENTOS_WORKTREES_SESSION",
        config_key="session",
        var_subdir="sessions",
    ),
    "longhaul": LaneSpec(
        lane="longhaul",
        namespace="longhaul",
        env="OMNIAGENTOS_WORKTREES_LONGHAUL",
        config_key="longhaul",
        var_subdir="longhaul",
    ),
}


def lane_spec(lane: LaneName | str) -> LaneSpec:
    """The binding for one lane name; ``KeyError`` for anything else."""
    return LANE_SPECS[str(lane)]


def _repo_root() -> str:
    """This checkout's root, derived from the package location (cwd-independent)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def lane_var_root(spec: LaneSpec) -> Path:
    """``<var>/<lane subdir>`` — worktrees then land at ``<that>/worktrees/...``.

    Honors ``OMNIAGENTOS_VAR_DIR``, the knob every other var-anchored helper in
    the repo reads (``scope.paths._var_root``, ``adapters.common``,
    ``intake.service``). Read at call time rather than import time so a test
    (or a relocated host) is not stuck with whatever the value was when the
    module first loaded.
    """
    base = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.path.join(_repo_root(), "var")
    return Path(base) / spec.var_subdir


#: HARD INTERLOCK -- the lane flags cannot be turned on yet, no matter what the
#: env or config says. Flip to True ONLY when every item in
#: ``EXECUTOR_WIRING_REMAINING`` below is done.
#:
#: Why an interlock and not just a default-off flag: with the flag on TODAY, this
#: module creates and merges a worktree, but the lane's EXECUTOR still runs in the
#: shared project directory -- the cwd is chosen in files this package does not
#: own (runner/core.py:1278, sessions/supervisor.py._launch,
#: longhaul/engine.py:434+615). The scope claim would then key on the private
#: worktree realm while the writes land in the project: PARALLEL AND WRONG, which
#: is strictly worse than today's SERIAL AND CORRECT. A default-off flag protects
#: against inaction; it does not protect against someone reasonably deciding to
#: try the feature. This does.
_EXECUTOR_WIRING_COMPLETE = False

#: The exact remaining work, so the interlock is a checklist and not a mystery.
EXECUTOR_WIRING_REMAINING = (
    "runner/core.py:_execute_agent -- use working_dir_for() as the cwd AND pass "
    "that same path to the scope wiring, so claim and write agree",
    "runner/core.py:_granted_scope_roots -- admit the lane worktrees base, or "
    "_assert_working_dir_in_scope rejects the worktree cwd as out-of-scope "
    "(same pattern as the existing intake-workspace entry)",
    "sessions/supervisor.py._launch -- same cwd/claim agreement",
    "longhaul/engine.py:434,615 -- same cwd/claim agreement",
    "an agent-side commit protocol: integrate() merges HEAD, so uncommitted work "
    "is invisible to the merge (swarm has this via its worker protocol + quality "
    "gate; the other three lanes do not)",
)


def lane_worktrees_enabled(lane: LaneName | str) -> bool:
    """The lane's opt-in flag: env override wins, else config, else OFF.

    The env override is bidirectional — ``OMNIAGENTOS_WORKTREES_LONGHAUL=0``
    force-disables the lane on a host whose ``configs/parallelism.yaml`` turns it
    on — because a one-directional override is not a kill switch. Identical
    semantics to ``swarm.worktrees.swarm_worktrees_enabled`` and to
    ``scope.config.scope_locks_mode``; an unparseable value is IGNORED rather
    than read as truthy, so a typo can never silently start rewriting where a
    lane runs.

    The config surface is ``configs/parallelism.yaml``:

    .. code-block:: yaml

        worktrees:
          runner: false
          session: false
          longhaul: false

    An absent file, an absent block or an absent key all resolve to False.
    """
    if not _EXECUTOR_WIRING_COMPLETE:
        # See _EXECUTOR_WIRING_COMPLETE. Returning False unconditionally is the
        # point: the mechanism below is correct and tested, but enabling it before
        # the executor cwd follows the worktree would put claims and writes in
        # different places.
        return False

    spec = lane_spec(lane)
    env = os.environ.get(spec.env, "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSY:
        return False
    try:
        raw = parallelism_config().get("worktrees")
    except Exception:  # noqa: BLE001 -- a broken config never enables a feature.
        LOG.debug("could not read the worktrees config block", exc_info=True)
        return False
    if not isinstance(raw, Mapping):
        return False
    value = raw.get(spec.config_key)
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in _TRUTHY:
        return True
    return False


# ---------------------------------------------------------------------------
# The recorded registration
# ---------------------------------------------------------------------------


def _mode_text(enabled: bool) -> str:
    return WORKTREE_MODE if enabled else SAME_DIR_MODE


def recorded_mode(record: Any) -> bool | None:
    """The RECORDED mode from a persisted record, or ``None`` if it says nothing.

    Total by design: an absent record, a malformed one, or a mode spelling from
    a future version all return ``None`` and the caller re-resolves from the
    ambient flag. A declaration blob is bookkeeping, not a trust boundary, and a
    unit must never be undispatchable because of its own bookkeeping.
    """
    if not isinstance(record, Mapping):
        return None
    text = str(record.get(MODE_KEY) or "").strip().lower()
    if text == WORKTREE_MODE:
        return True
    if text == SAME_DIR_MODE:
        return False
    return None


@dataclass(frozen=True, slots=True)
class LaneWorktree:
    """One unit's private worktree."""

    lane: str
    unit_id: str
    path: str
    branch: str
    base_sha: str
    base_dir: str
    realm: str
    reused: bool = False

    def as_record(self) -> dict[str, str]:
        """The blob persisted into the lane's declaration column.

        ``base_dir`` is in here on purpose: it is what a SUCCESSOR process needs
        to merge this branch back without being told, and re-deriving it from a
        plan or a config that has since changed is exactly how a merge lands in
        the wrong repo.
        """
        return {
            MODE_KEY: WORKTREE_MODE,
            "lane": self.lane,
            "unit_id": self.unit_id,
            "path": self.path,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "base_dir": self.base_dir,
            "realm": self.realm,
        }


def worktree_from_record(record: Any) -> LaneWorktree | None:
    """Rebuild a :class:`LaneWorktree` from a persisted record; ``None`` if unusable.

    Touches neither git nor the filesystem — a resumed process can read back what
    it (or its predecessor) claimed without paying a subprocess for it.
    """
    if recorded_mode(record) is not True or not isinstance(record, Mapping):
        return None
    path = str(record.get("path") or "")
    branch = str(record.get("branch") or "")
    if not path or not branch:
        return None
    return LaneWorktree(
        lane=str(record.get("lane") or ""),
        unit_id=str(record.get("unit_id") or ""),
        path=path,
        branch=branch,
        base_sha=str(record.get("base_sha") or ""),
        base_dir=str(record.get("base_dir") or ""),
        realm=str(record.get("realm") or ""),
    )


@dataclass(frozen=True, slots=True)
class ModeDecision:
    """The m6 mode resolution for one unit.

    ``enabled``
        Run this unit in worktree mode NOW.
    ``recorded``
        What the row said (``None`` = nothing recorded yet).
    ``record``
        What the caller must PERSIST (``None`` = a value is already recorded and
        must not be overwritten — that overwrite is the mid-flight flip).
    ``registered``
        The recorded registration, for GC/cleanup gating: a unit recorded as
        worktree-mode whose probe failed on THIS launch still has worktrees to
        clean up. Mirrors ``_RunState.worktree_recorded``.
    """

    enabled: bool
    recorded: bool | None
    record: bool | None
    registered: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Activation:
    """What one unit got, and what the caller must persist.

    ``working_dir`` is the whole point of the API: the caller runs the unit
    THERE and claims ``realm_of(working_dir)``. With the flag off (or on any
    degradation) it is the caller's own ``base_dir``, so a caller that always
    uses it is correct in both modes and cannot drift.
    """

    lane: str
    unit_id: str
    mode: bool
    working_dir: str
    record: dict[str, str] | None
    worktree: LaneWorktree | None = None
    registered: bool = False
    detail: str = ""


IntegrationStatus = Literal["merged", "noop", "conflict", "skipped"]


@dataclass(frozen=True, slots=True)
class Integration:
    """The result of merging one unit's branch back into the shared workspace.

    ``conflict`` leaves the branch ALIVE (``merge --abort`` has already run
    inside ``merge_branch``, so the shared workspace is pristine) and names it,
    because a conflicting branch is unmerged WORK and deleting it is data loss.
    """

    status: IntegrationStatus
    branch: str = ""
    sha: str | None = None
    conflict_files: tuple[str, ...] = ()
    detail: str = ""

    @property
    def clean(self) -> bool:
        """True when nothing is left for a human to resolve."""
        return self.status in ("merged", "noop")


# ---------------------------------------------------------------------------
# In-process serialization of the shared workspace
# ---------------------------------------------------------------------------

_REALM_LOCKS: dict[str, threading.RLock] = {}
_REALM_LOCKS_GUARD = threading.Lock()


def _realm_lock(key: str) -> threading.RLock:
    """One process-wide RLock per shared realm.

    ``commit_guard`` takes an in-process lock FIRST and the durable commit lock
    second — a fixed order no caller can invert. That in-process half has to be
    shared by every ``LaneWorktrees`` instance in the process or two lanes in one
    daemon would merge into the same workspace concurrently while each politely
    held its own private mutex.
    """
    with _REALM_LOCKS_GUARD:
        lock = _REALM_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _REALM_LOCKS[key] = lock
        return lock


class _Clock:
    """The two methods ``commit_guard`` needs."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_CLOCK = _Clock()


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


class LaneWorktrees:
    """One lane's per-unit worktrees. Construction touches nothing.

    Every method is TOTAL: a git failure, an unusable id, a missing repo or a
    broken config degrades to same-directory mode and says so in ``detail``. A
    lane that cannot make a worktree must still run the work — the worktree is an
    isolation optimization, never a precondition.
    """

    def __init__(
        self,
        spec: LaneSpec,
        *,
        worktrees: WorktreesProto | None = None,
        var_root: Path | None = None,
        dep_link_dirs: Sequence[str] | None = None,
        base_ref: str = "HEAD",
        merge_wait_s: float = DEFAULT_MERGE_WAIT_S,
        merge_poll_s: float = DEFAULT_MERGE_POLL_S,
    ) -> None:
        self._spec = spec
        self._seam: WorktreesProto | None = worktrees
        self._var_root = var_root
        self._dep_link_dirs = tuple(dep_link_dirs) if dep_link_dirs is not None else DEP_LINK_DIRS
        self._base_ref = base_ref
        self._merge_wait_s = float(merge_wait_s)
        self._merge_poll_s = float(merge_poll_s)
        self._registered_base: str | None = None

    # --- identity -----------------------------------------------------------

    @property
    def spec(self) -> LaneSpec:
        return self._spec

    @property
    def lane(self) -> str:
        return self._spec.lane

    def enabled(self) -> bool:
        """The ambient flag. Read fresh — it is this lane's kill switch."""
        return lane_worktrees_enabled(self._spec.lane)

    def var_root(self) -> Path:
        if self._var_root is None:
            self._var_root = lane_var_root(self._spec)
        return self._var_root

    def worktrees_base(self) -> Path:
        return self.var_root() / "worktrees"

    def _worktrees(self) -> WorktreesProto:
        if self._seam is None:
            self._seam = SubprocessWorktrees(
                namespace=self._spec.namespace,
                var_root=self.var_root(),
                dep_link_dirs=self._dep_link_dirs,
            )
        return self._seam

    def path_for(self, unit_id: str) -> str:
        """``<var root>/worktrees/<unit_id>/main``."""
        seam = self._worktrees()
        path_for = getattr(seam, "worktree_path", None)
        if path_for is None:  # a fake seam without the helper
            return str(self.worktrees_base() / unit_id / self._spec.unit_key)
        return str(path_for(unit_id, self._spec.unit_key))

    def branch_for(self, unit_id: str) -> str:
        """``<namespace>/<unit_id>/main``."""
        seam = self._worktrees()
        branch_for = getattr(seam, "branch_name", None)
        if branch_for is None:
            return f"{self._spec.namespace}/{unit_id}/{self._spec.unit_key}"
        return str(branch_for(unit_id, self._spec.unit_key))

    # --- realm ---------------------------------------------------------------

    def register_realm_base(self) -> str | None:
        """Declare ``<var root>/worktrees`` a private base at depth 2. Idempotent.

        MANDATORY before any path from this module reaches ``realm_of``; see the
        module docstring for the two ways skipping it fails. Depth 2 because the
        private unit is ``<unit_id>/<unit_key>``, the same shape (and the same
        constant) ``scope.paths`` already uses for the swarm worktrees base.
        """
        if self._registered_base is not None:
            return self._registered_base
        base = str(self.worktrees_base())
        try:
            register_private_base(base, 2)
        except (ScopePathError, OSError, ValueError):
            LOG.warning("could not register the %s worktrees base %s", self.lane, base)
            return None
        self._registered_base = base
        return base

    def realm_of_unit(self, unit_id: str) -> str | None:
        """The private realm key of one unit's worktree (registers the base first)."""
        self.register_realm_base()
        return realm_of(self.path_for(unit_id))

    # --- mode ----------------------------------------------------------------

    def resolve_mode(
        self,
        base_dir: str,
        *,
        recorded: bool | None = None,
        ambient: bool | None = None,
    ) -> ModeDecision:
        """Resolve worktree mode for ONE unit — the m6 discipline, exactly.

        A RECORDED value wins over the ambient flag in BOTH directions: a unit
        recorded as worktree-mode stays in worktree mode in a process whose flag
        is off, and a unit recorded as same-directory never flips ON mid-flight.
        Only a unit with NOTHING recorded consults the ambient flag, and the
        result is handed back as ``record`` for the caller to persist — one
        write, at first activation, forever after replayed.

        Enabling additionally requires a workspace git can make a worktree in: a
        clean ``worktree list`` probe AND a resolvable git common dir. A miss
        logs and falls back to same-directory mode, which is byte-for-byte what
        the lane did before this module existed. When the miss contradicts a
        RECORDED registration the log is an ERROR, not a warning — that unit has
        a branch and possibly uncommitted work sitting in a worktree this process
        is about to ignore.
        """
        if recorded is not None:
            enabled = recorded
        elif ambient is not None:
            enabled = bool(ambient)
        else:
            enabled = self.enabled()
        supported = False
        detail = ""
        if enabled:
            try:
                seam = self._worktrees()
                supported = bool(seam.supported(base_dir) and seam.git_common_dir(base_dir))
            except (OSError, subprocess.SubprocessError, ValueError):
                LOG.warning("%s worktree probe raised for %s", self.lane, base_dir, exc_info=True)
                supported = False
            if not supported:
                detail = "probe_failed"
        resolved = bool(enabled and supported)
        if enabled and not supported:
            log = LOG.error if recorded else LOG.warning
            log(
                "%s worktree mode %s but the probe failed for %s; falling back to the "
                "shared-directory model",
                self.lane,
                "was RECORDED for this unit" if recorded else "requested",
                base_dir,
            )
        return ModeDecision(
            enabled=resolved,
            recorded=recorded,
            # Nothing recorded yet -> record the RESOLVED mode (activation).
            # Something recorded -> None: overwriting it IS the mid-flight flip.
            record=resolved if recorded is None else None,
            registered=bool(recorded) if recorded is not None else resolved,
            detail=detail,
        )

    # --- activate ------------------------------------------------------------

    def activate(
        self,
        unit_id: str,
        base_dir: str,
        *,
        record: Any = None,
        ambient: bool | None = None,
        base_ref: str | None = None,
    ) -> Activation:
        """Create (or reuse) this unit's worktree and say where it should run.

        ``record`` is the blob the lane persisted last time (``None`` on first
        activation); its ``mode`` is what wins over the ambient flag.

        REUSE IS THE POINT FOR A CHAIN OF ATTEMPTS. ``SubprocessWorktrees.create``
        reuses a still-registered worktree AS-IS — a successor attempt therefore
        inherits its predecessor's UNCOMMITTED work — and attaches an existing
        branch AT ITS TIP, so salvage-committed work relays forward across a
        worktree that was removed and remade. Longhaul depends on both: its unit
        is the BOARD TASK, not the attempt, and an attempt chain that reset the
        tree every time would throw away attempt N's partial work at attempt N+1.

        Never raises: an unsafe unit id, a git failure or a probe miss all come
        back as an inert activation pointing at ``base_dir``.
        """
        unit = str(unit_id or "")
        decision = self.resolve_mode(base_dir, recorded=recorded_mode(record), ambient=ambient)
        if not decision.enabled:
            return Activation(
                lane=self.lane,
                unit_id=unit,
                mode=False,
                working_dir=base_dir,
                record=({MODE_KEY: _mode_text(False)} if decision.record is not None else None),
                registered=decision.registered,
                detail=decision.detail,
            )
        self.register_realm_base()
        try:
            info: WorktreeInfo = self._worktrees().create(
                base_dir, unit, self._spec.unit_key, base_ref or self._base_ref
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            # A repo that refuses a worktree add (unsafe id, disk full, a hook
            # the -c core.hooksPath= disable did not cover) must not take the
            # unit down with it: fall back to the shared directory, which is
            # exactly where the lane ran before this feature existed.
            LOG.warning(
                "%s worktree create failed for %s in %s; using the shared directory",
                self.lane,
                unit,
                base_dir,
                exc_info=True,
            )
            return Activation(
                lane=self.lane,
                unit_id=unit,
                mode=False,
                working_dir=base_dir,
                record=None,  # NOT recorded: a transient git failure is not a mode
                registered=decision.registered,
                detail="create_failed",
            )
        realm = realm_of(info.path) or ""
        worktree = LaneWorktree(
            lane=self.lane,
            unit_id=unit,
            path=info.path,
            branch=info.branch,
            base_sha=info.base_sha,
            base_dir=base_dir,
            realm=realm,
            reused=info.reused,
        )
        return Activation(
            lane=self.lane,
            unit_id=unit,
            mode=True,
            working_dir=info.path,
            # Re-record on every activation: the blob carries the live path,
            # branch and base sha, and only ``mode`` is the one-way decision.
            record=worktree.as_record(),
            worktree=worktree,
            registered=True,
        )

    # --- integrate -----------------------------------------------------------

    def integrate(
        self,
        worktree: LaneWorktree,
        *,
        message: str = "",
        locks: PathLockStore | None = None,
        holder: LockHolder | None = None,
        generation: int = 0,
        wait_s: float | None = None,
    ) -> Integration:
        """Merge this unit's branch into the shared workspace, BY SHA.

        THE SHA IS CAPTURED INSIDE THE LOCK, and that ordering is the whole
        contract. Capturing it outside leaves a window in which another lane (or
        this unit's own still-running process) advances the branch between the
        capture and the merge, so the commit that lands is not the commit that
        was read — the merge would then be "whatever the ref says at merge time",
        which is precisely the ref-tampering surface ``merge_branch(sha=...)``
        exists to close.

        The lock is the shared realm's COMMIT lock, not a work lock: a merge
        rewrites the index and moves HEAD, so it is realm-wide in effect whatever
        files it names. The unit's own work locks are on the WORKTREE realm — a
        different realm — so this can never block on itself.

        With scope locking off, ``commit_guard`` degrades to the in-process lock
        alone, which is the protection this lane had before Phase 3.

        Conflicts are NOT an error: ``merge_branch`` has already run
        ``merge --abort`` (the shared workspace is pristine), the branch stays
        alive, and it is named in the result so the caller can route it to a
        human or to an integration unit.

        ``wait_s`` overrides how long this call waits out another process's
        commit lock. It exists for callers on a POLL LOOP: the default is sized
        to outlast a lock TTL (nobody raises over a corpse), and a poll thread
        that blocks that long is a stalled lane. Such a caller passes a short
        wait and accepts a ``skipped`` it can retry.
        """
        base_dir = worktree.base_dir
        if not base_dir:
            return Integration(status="skipped", branch=worktree.branch, detail="no_base_dir")
        seam = self._worktrees()
        realm = realm_of(base_dir)
        label = f"{self.lane}:{worktree.unit_id}:merge"
        try:
            with self._commit_guard(realm, locks, holder, generation, label, wait_s):
                sha = seam.head_sha(worktree.path)
                if not sha:
                    # No readable HEAD: the worktree is gone or was never made.
                    # Merging the BRANCH here instead would be the exact
                    # capture-outside-the-lock hazard, so refuse.
                    return Integration(
                        status="skipped", branch=worktree.branch, detail="no_head_sha"
                    )
                outcome: MergeOutcome = seam.merge_branch(
                    base_dir,
                    worktree.branch,
                    message or self._merge_message(worktree),
                    sha=sha,
                )
        except Exception as exc:  # noqa: BLE001 -- a busy/fenced lock is a deferral
            LOG.warning("%s: merge deferred (%s)", label, exc)
            return Integration(
                status="skipped",
                branch=worktree.branch,
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
        if outcome.status == "conflict":
            LOG.warning(
                "%s: merge conflicted on %s; branch %s kept for manual integration",
                label,
                ", ".join(outcome.conflict_files) or "unknown paths",
                worktree.branch,
            )
            return Integration(
                status="conflict",
                branch=worktree.branch,
                sha=sha,
                conflict_files=outcome.conflict_files,
                detail=outcome.detail,
            )
        return Integration(
            status="noop" if outcome.status == "noop" else "merged",
            branch=worktree.branch,
            sha=outcome.sha or sha,
        )

    def _merge_message(self, worktree: LaneWorktree) -> str:
        return f"{self.lane}: merge {worktree.branch}"

    @contextmanager
    def _commit_guard(
        self,
        realm: str | None,
        locks: PathLockStore | None,
        holder: LockHolder | None,
        generation: int,
        label: str,
        wait_s: float | None = None,
    ) -> Iterator[Any]:
        """The shared realm's commit lock, in-process lock first.

        Delegates to ``swarm.scope_wiring.commit_guard`` deliberately. That
        helper is generic in everything but its home — it takes the store, the
        realm, the holder and an in-process lock and knows nothing about swarm —
        and its wait/degrade policy (hold the in-process lock across the wait;
        proceed without the durable lock when the blocker is not another
        COMMIT; raise only on a fence or a timeout) is the one thing here that
        must not exist twice. FOLLOW-UP: it belongs in ``omniagentos/scope/``;
        moving it is a rename this work package is not allowed to make.
        """
        from omniagentos.swarm.scope_wiring import commit_guard

        with commit_guard(
            locks if realm else None,
            realm=realm,
            holder=holder,
            generation=int(generation),
            in_process_lock=_realm_lock(realm or f"{self.lane}:no-realm"),
            clock=_CLOCK,  # type: ignore[arg-type]  # structural: monotonic + sleep
            wait_s=self._merge_wait_s if wait_s is None else float(wait_s),
            poll_s=self._merge_poll_s,
            label=label,
        ) as result:
            yield result

    # --- finish --------------------------------------------------------------

    def finish(
        self, worktree: LaneWorktree, *, salvage: bool = True, message: str = ""
    ) -> RemoveOutcome | None:
        """Remove the unit's worktree, salvaging anything uncommitted first.

        ``salvage_failed`` means the tree is STILL dirty and could not be
        committed: ``remove`` then LEAVES THE WORKTREE IN PLACE rather than
        rmtree-ing over unpersisted work, and the caller gets that status back so
        it can be surfaced. ``None`` means the removal itself raised — same
        conclusion, nothing was destroyed.

        Call AFTER :meth:`integrate`: removal is what makes a later ``create``
        re-attach the branch at its tip instead of reusing the tree, and doing it
        first would mean merging a branch whose last work had not been salvaged.
        """
        try:
            return self._worktrees().remove(
                worktree.base_dir or str(self.var_root()),
                worktree.path,
                salvage=salvage,
                message=message or f"{self.lane}: salvage {worktree.unit_id}",
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            LOG.warning("%s: could not remove worktree %s", self.lane, worktree.path, exc_info=True)
            return None


_LANES: dict[str, LaneWorktrees] = {}
_LANES_GUARD = threading.Lock()


def lane_worktrees(lane: LaneName | str) -> LaneWorktrees:
    """The process-wide :class:`LaneWorktrees` for one lane.

    Shared because the registered private base and the subprocess seam are
    per-lane facts, not per-caller ones, and because two supervisors in one
    process must not each register their own. Construction is inert, so building
    it costs nothing on the dark path — but nothing builds it there either.
    """
    key = str(lane)
    with _LANES_GUARD:
        existing = _LANES.get(key)
        if existing is None:
            existing = LaneWorktrees(lane_spec(key))
            _LANES[key] = existing
        return existing
