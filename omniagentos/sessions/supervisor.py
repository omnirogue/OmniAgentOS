"""Long-lived supervisor for bridge-managed Claude Code sessions."""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from omniagentos.adapters.claude import _resolve_claude_cli
from omniagentos.budget.policy import blocks as budget_blocks
from omniagentos.budget.policy import session_budget_cap
from omniagentos.contracts import (
    API_HOST,
    ActionClass,
    ApprovalState,
    default_db_path,
    new_id,
    utc_now_iso,
)
from omniagentos.path_containment import inode_paths_equal
from omniagentos.policy import approval_satisfies_gate, evaluate_action, load_policy
from omniagentos.runner import sandbox
from omniagentos.scope.model import ScopeClaim
from omniagentos.sessions import hook_token, notify, ssh_keys
from omniagentos.sessions.dal import (
    TERMINAL_SESSION_STATES,
    SessionsDal,
    SessionState,
    decode_granted_roots,
    encode_granted_roots,
)
from omniagentos.sessions.lifecycle import (
    _OPEN_SUPERVISORS,
    _OPEN_SUPERVISORS_LOCK,
    DEFAULT_JOIN_TIMEOUT_S,
    SHUTDOWN_KILLED_BY,
    AdoptedPidVerdict,
    LaunchTicket,
    ManifestObligation,
    SessionSupervisorCloseError,
    _ensure_atexit_registered,
    observe_process,
    verify_adopted_pid,
)
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.policy_map import action_hash
from omniagentos.sessions.scope_wiring import SessionScopeGate
from omniagentos.toolplane.scrub import scrub_optional_text, scrub_text

LOG = logging.getLogger(__name__)

_CONTINUATION = "The pending action was approved by a human; proceed with that action."


# --- SEAM 2 (agent-output redaction) -------------------------------------------
# Session output and session error are RAW CLI stdout: the stream-json events the
# reader harvests, the transcript's final reply, and exception text built from
# both. Nothing on this path constructs an AgentResult, so the runner's receipt
# seam (SEAM 1, runner/core.py) cannot cover it -- and per-writer scrubbing was
# proven incomplete over five review rounds, because each round found one more
# writer. These two functions are therefore the ONLY sanctioned way this module
# writes agent text to a session row; tests/security/
# test_agent_output_redaction_coverage.py fails if a new caller bypasses them.


def _persist_session_output(dal: Any, session_id: str, text: str | None) -> None:
    """Write ``sessions.output_text``, redacted at the capture point."""
    dal.set_session_output(session_id, scrub_optional_text(text))


def _persist_session_error(dal: Any, session_id: str, text: str | None) -> None:
    """Write the ``sessions.error`` row field, redacted at the capture point."""
    dal.set_session_error(session_id, scrub_optional_text(text))


def _record_session_error(dal: Any, session_id: str, text: str) -> None:
    """Append the spawn-failure AUDIT EVENT, redacted at the capture point.

    Deliberately separate from :func:`_persist_session_error`: the DAL's two
    error writers hit two different surfaces (an audit event row versus the
    session's ``error`` column), so folding them together would silently move
    where a spawn failure is recorded.
    """
    dal.record_session_error(session_id, scrub_text(text))


def _merge_session_standing_roots(roots: list[str], *, project_dir: str | None = None) -> list[str]:
    """AUTO-APPROVE Phase 1: fold standing roots into every session grant set."""
    try:
        from omniagentos.policy.roots import merge_standing_roots

        return merge_standing_roots(roots, working_dir=project_dir)
    except Exception:  # noqa: BLE001
        LOG.debug("standing roots merge failed at session spawn", exc_info=True)
        return list(roots or [])


# --- A2 idle/hang reaper knobs -------------------------------------------------
# Idle threshold in minutes for RUNNING/RESUMING bridge sessions (default 15).
IDLE_MINUTES_ENV = "OMNIAGENTOS_SESSION_IDLE_MINUTES"
DEFAULT_IDLE_MINUTES = 15.0
# Enforce is the intended production default (the launcher exports 1).  Unset/0
# is the exceptional dry-run/debug mode: log structured would-kill lines only.
REAPER_ENFORCE_ENV = "OMNIAGENTOS_REAPER_ENFORCE"
# MAX-PARK CEILING. awaiting_approval is parked BY DESIGN so a human decision can
# resume the exact session — but "by design" cannot be unbounded. A delivered,
# still-pending approval fails at this ceiling; an undelivered/no-row one is
# durably re-armed and escalated up to the outer ceiling. It is never
# auto-approved: failing closed is the contract.
# Set the env var to 0 (or a negative number) to disable the ceiling outright;
# an unparseable value falls back to the default.
MAX_PARK_MINUTES_ENV = "OMNIAGENTOS_SESSION_MAX_PARK_MINUTES"
DEFAULT_MAX_PARK_MINUTES = 20.0
# An undelivered alert re-arms one normal window at a time, but a broken delivery
# channel must not pin a worker forever. This absolute cap includes every re-arm.
_MAX_PARK_OUTER_CEILING_MULTIPLIER = 4.0
# A live child process in the session's process group (a running tool subprocess,
# e.g. a 20-minute build) defers the kill one service interval — but never past
# this multiple of the idle threshold.
_IDLE_HARD_CEILING_MULTIPLIER = 3.0
# STARTING with no pid and no queued spawn request for longer than this is a
# launch that never happened (crash between create and launch) -> failed.
_STARTING_STALL_SECONDS = 300.0
# Longhaul's engine is the SOLE RESPAWN owner for its sessions
# (docs/architecture/longhaul.md): auth-retry and the broken-login notifier
# stay gated off for marked sessions. The A2 reaper MAY kill longhaul
# sessions (idle/budget) — they carry a per-lane idle_minutes override
# persisted at spawn, and the engine respawns off the killed_by attribution.
_LONGHAUL_TITLE_MARKER = "[longhaul:"
_SWARM_TITLE_MARKER = "[swarm:"
# Phase 3 (T3.x) scope locks: those SAME two markers also mean "this session takes
# no scope locks of its own" — the owning lane already holds them, and a session
# claiming them again under its own holder identity would be refused by its own
# coordinator and park forever. sessions.scope_wiring owns that predicate and a
# test asserts its marker tuple is exactly these two.


def _approval_delivered(approval: Mapping[str, Any]) -> bool:
    """True only when THIS approval row itself reached a human-facing transport.

    Both halves of the carrier are required: ``delivery_state`` says an attempt
    was accepted, ``delivered_at`` says when. A row missing either is treated as
    never delivered -- absence of a carrier is never a favourable value (that is
    exactly the pre-migration-121 shape, which must read as undelivered).
    """
    if str(approval.get("delivery_state") or "unattempted") != "delivered":
        return False
    return _iso_to_epoch(approval.get("delivered_at")) is not None


def _approval_action_hash(approval: Mapping[str, Any]) -> str | None:
    """The action this approval row is ABOUT, or None when unreadable.

    ``create_session_approval`` always writes ``action_hash`` into
    ``params_json``; a row whose params are missing or corrupt answers None,
    which never matches a denied hash -- an unreadable row must not be able to
    stand in for the action a process was actually blocked on.
    """
    try:
        params = json.loads(str(approval.get("params_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(params, dict):
        return None
    action_hash = params.get("action_hash")
    return str(action_hash) if isinstance(action_hash, str) else None


def _approval_label(approval_ids: Sequence[str], *, prefix: str = "") -> str:
    """Forensic label for the approvals a max-park decision was taken over.

    A park can be about several approvals, so the label is a list, never one id
    standing in for the rest. The zero case stays ``none recorded`` rather than
    an empty string: an unreadable/absent approval is a real supervision shape
    (the unreachable-hook park) and must be legible as such in the ledger.
    """
    if not approval_ids:
        return "none recorded"
    return f"{prefix}{', '.join(approval_ids)}"


@dataclass
class _ParkEscalations:
    """Max-park escalations this PROCESS sent for ONE park episode of a session.

    Durable ``reaper.max_park_extended`` events are the real dedupe, across
    processes and restarts; this is only the in-process fast path for the case
    where that write could not happen. It therefore records TWO things, not one:
    ``escalated_at`` (an escalation was sent) and ``unpersisted_at`` (and it left
    no durable receipt). The second is what may outlive its episode, because
    only it carries information the durable log does not.

    ``anchor`` is the episode's park-start epoch in whole seconds -- the
    resolution the durable stamps carry. ``awaiting_since`` re-bases it on every
    re-park, so a session that parks, is decided, resumes and parks again is a
    new episode. It is a coarse key on purpose and is NOT relied on alone: two
    park episodes a fraction of a second apart share an anchor.
    """

    anchor: int
    escalated_at: float = 0.0
    unpersisted_at: float = 0.0


def _iso_to_epoch(value: Any) -> float | None:
    """Parse a stored ISO-8601 timestamp (utc_now_iso format) to a Unix epoch."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scheduler_owns_respawn(title: Any) -> bool:
    """True when a durable lane engine, not the supervisor, owns successors."""

    text = str(title or "")
    return _LONGHAUL_TITLE_MARKER in text or _SWARM_TITLE_MARKER in text


def _pgid_live_children(pid: int) -> int:
    """Count live processes in ``pid``'s process group besides the leader itself.

    The bridge launches with ``start_new_session=True``, so the session pid is its
    own process-group id and any running tool subprocess (Bash build, test run, …)
    is a non-leader member. Best-effort: any failure reports 0 children, which
    falls through to the plan-default reap at the idle threshold."""
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, integer arg
            ["pgrep", "-g", str(pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    children = 0
    for token in completed.stdout.split():
        try:
            member = int(token)
        except ValueError:
            continue
        if member != pid:
            children += 1
    return children


_SAFE_ENV_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    }
)


def build_clean_env(
    parent: Mapping[str, str] | None = None, *, session_id: str | None = None
) -> dict[str, str]:
    """Build a minimal environment without inheriting broker/API credentials."""

    source = os.environ if parent is None else parent
    clean = {
        name: value
        for name, value in source.items()
        if name in _SAFE_ENV_NAMES or name.startswith("LC_")
    }
    if session_id is not None:
        clean["OMNIAGENTOS_BRIDGE_SESSION_ID"] = session_id
    return clean


# --- control-plane URL stamped into every bridge session ----------------------
# The hook subprocess (sessions/hook_client.py) POSTs approval requests to the
# control plane. It used to resolve that URL from a compiled-in :8484 default —
# the SIBLING product's port — unless OMNI_SESSION_API_URL happened to be set,
# which build_clean_env scrubs out of the child environment anyway. A bench run
# on :8499 therefore posted into the void: no approval row was ever created and
# the session parked forever (bench swr_8474e958870543388267).
#
# The supervisor is the right resolver because it runs INSIDE the stack whose
# API the hook must reach: scripts/launch-omniagentos.sh exports
# OMNIAGENTOS_API_PORT (default 8485) before exec'ing `python -m
# omniagentos.sessions.supervisor`, so the port is already in this process's
# environment. No launcher change is needed; an operator running the supervisor
# by hand can set either variable below explicitly.
API_BASE_URL_ENV = "OMNIAGENTOS_API_BASE_URL"  # stamped INTO the session env
SESSION_API_URL_ENV = "OMNI_SESSION_API_URL"  # explicit override (daemon side)
API_PORT_ENV = "OMNIAGENTOS_API_PORT"  # set by scripts/launch-omniagentos.sh


def resolved_api_base_url(environ: Mapping[str, str] | None = None) -> str | None:
    """This stack's control-plane base URL, with sensible fallback.

    Precedence: explicit ``OMNI_SESSION_API_URL`` > explicit
    ``OMNIAGENTOS_API_BASE_URL`` > ``http://127.0.0.1:$OMNIAGENTOS_API_PORT`` >
    ``http://127.0.0.1:8485`` (the standard OmniAgentOS port).

    This ensures the hook client always has a valid URL and avoids falling back
    to the sibling product's port (8484). When OMNIAGENTOS_API_PORT is set (by
    launch-omniagentos.sh or an operator), that port is used; otherwise
    we use 8485, the default configured in launch-omniagentos.sh.
    """
    source = os.environ if environ is None else environ
    for name in (SESSION_API_URL_ENV, API_BASE_URL_ENV):
        value = str(source.get(name, "") or "").strip()
        if value:
            return value.rstrip("/")
    raw = str(source.get(API_PORT_ENV, "") or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            port = 0
        if 0 < port < 65536:
            return f"http://{API_HOST}:{port}"
    # Fallback to the standard port if OMNIAGENTOS_API_PORT is not set,
    # instead of returning None which would cause the hook to use the wrong port (8484).
    return f"http://{API_HOST}:8485"


def _resolve_spawn_account() -> tuple[str | None, dict[str, str], str | None, str | None]:
    """The next enabled Claude account's spawn credential (round-robin), or a no-op.

    Returns ``(config_dir, env, account_id, label)``. ``config_dir`` is ``None`` when
    the selection is the default ``~/.claude`` (setting ``CLAUDE_CONFIG_DIR`` to it
    changes nothing) or when no account is enabled -- so behaviour is byte-identical to
    before this feature until an operator enables a non-default account. Fail-safe: any
    error → the default login.

    Routed through ``routing.limit_state.pick_account`` (WP2: the single durable
    cooldown/limit authority) -- same LRU rotation and cooldown filter as the old
    direct ``next_account_for_spawn`` call, now provider-scoped."""
    try:
        from omniagentos.routing.limit_state import pick_account

        account = pick_account("claude")
    except Exception:  # noqa: BLE001 -- account selection must never break a launch.
        LOG.debug("account selection failed; using default login", exc_info=True)
        return None, {}, None, None
    if account is None:
        return None, {}, None, None
    config_dir = account.config_dir
    if config_dir:
        default_cfg = os.path.realpath(os.path.expanduser("~/.claude"))
        # Safety (`is True`): suppress the override only for positive default identity.
        if inode_paths_equal(os.path.realpath(config_dir), default_cfg) is True:
            config_dir = None  # default account -> no CLAUDE_CONFIG_DIR needed
    return config_dir, dict(account.env or {}), account.account_id, account.label


def _event_text(event: dict[str, Any]) -> str:
    """Return the textual portions of a stream-json event."""

    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(nested, (dict, list)):
                    collect(nested)
                elif key in {"result", "error", "message", "text", "detail", "error_message"}:
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    # Stream-json errors have appeared in ``result``, ``error``, and assistant text
    # blocks across CLI versions, so retain all values rather than relying on one field.
    collect(event)
    return " ".join(values)


def _auth_failure_detail(event: dict[str, Any]) -> str | None:
    """Redacted auth-failure detail for a stream event, or None.

    SEAM 2 applies here as well as at the DAL writers: this detail is verbatim
    CLI stream text and it travels FURTHER than a session row -- it becomes the
    account's health status (``accounts.mark_status``) and the body of an
    operator notification. Classification still runs on the raw text, so
    redaction cannot change which events are read as auth failures; only the
    text that escapes is redacted.
    """
    return scrub_optional_text(_auth_failure_detail_unredacted(event))


def _auth_failure_detail_unredacted(event: dict[str, Any]) -> str | None:
    """Extract authentication failure signals from a stream event (three independent sources).

    Sources:
    1. Numeric error_status == 401 in system/api_retry events
    2. String error == "authentication_failed" field
    3. Text keywords without requiring "401": failed to authenticate, oauth session expired, etc.
    """
    text = _event_text(event)
    lowered = text.lower()

    # Source 1: System api_retry event with 401 status or authentication_failed marker
    if event.get("type") == "system" and event.get("subtype") == "api_retry":
        if event.get("error_status") == 401:
            reason = str(event.get("error") or "api_error")
            return f"system api_retry error_status 401: {reason}"
        if event.get("error") == "authentication_failed":
            return "system api_retry authentication_failed"

    # Source 2: Top-level error field with authentication_failed
    if event.get("error") == "authentication_failed":
        return f"authentication_failed: {text[:200]}" if text else "authentication_failed"

    # Source 3: Text keywords (strong auth phrases, no "401" required)
    has_strong_auth_keyword = (
        "failed to authenticate" in lowered
        or "authentication_failed" in lowered
        or "oauth session expired" in lowered
        or ("oauth" in lowered and "revoked" in lowered)
        or "invalid api key" in lowered
        or ("invalid" in lowered and "authentication" in lowered)
    )

    if has_strong_auth_keyword:
        return text

    return None


def _is_auth_failure(event: dict[str, Any]) -> bool:
    """Detect authentication failure in any event type (system, assistant, result).

    Result events must be marked as errors (is_error flag or error subtype/reason).
    System/assistant events can be auth hints even if not terminal errors.
    """
    event_type = str(event.get("type") or "").lower()

    # System/assistant events can signal auth failures without being terminal errors
    if event_type in ("system", "assistant"):
        return _auth_failure_detail(event) is not None

    # Result events must be marked as errors to count as failures
    if event_type == "result":
        is_error_flag = bool(event.get("is_error"))
        subtype = str(event.get("subtype") or "").lower()
        terminal_reason = str(event.get("terminal_reason") or "").lower()
        is_error = is_error_flag or subtype.startswith("error") or "error" in terminal_reason
        if not is_error:
            return False
        return _auth_failure_detail(event) is not None

    return False


def bridge_settings_path() -> str:
    """Resolve the installed gating hook settings.

    The bridge hook matcher MUST be ``.*`` (gate EVERY tool) in every code path
    (T-CODE-003): a narrow matcher would let an unlisted tool bypass the hook and
    defeat deny-by-default. The installer is the single source of that ``.*``
    settings file; if it cannot be imported we FAIL rather than fall back to a
    weaker matcher.
    """

    try:
        install = importlib.import_module("omniagentos.sessions.install")
        installed_path = cast(Callable[[], str], install.bridge_settings_path)
    except (
        ImportError,
        AttributeError,
    ) as exc:  # pragma: no cover - import always succeeds in-tree
        raise RuntimeError(
            "bridge hook installer unavailable; refusing to launch a session without "
            "the gating (.*) PreToolUse hook"
        ) from exc
    return str(installed_path())


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class _ManagedProcess:
    session_id: str
    process: subprocess.Popen[str]
    resumed: bool = False
    thread: threading.Thread | None = None
    result_error: bool = False
    auth_failure: bool = False
    auth_failure_detail: str | None = None
    auth_hint: bool = False  # Set by system api_retry events, not terminal on its own
    account_id: str | None = None
    account_label: str | None = None
    account_config_dir: str | None = None
    permission_denied: bool = False
    denied_action_hashes: set[str] | None = None
    touched_files: set[str] | None = None
    stream_events: list[dict[str, Any]] | None = None


class SessionSpawnError(RuntimeError):
    """A spawn failure whose durable session row is available for inspection."""

    def __init__(self, session_id: str, error: BaseException) -> None:
        detail = str(error) or type(error).__name__
        super().__init__(f"session {session_id} failed to spawn: {detail}")
        self.session_id = session_id


def _recorded_project_dir(project_dir: str) -> str:
    """Normalize a path for the STARTING row without requiring it to exist."""
    try:
        return str(Path(project_dir).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(project_dir)


def _dal_db_path(dal: SessionsDal) -> str:
    """Resolve the on-disk path backing *dal*'s SQLite connection."""
    db_row = dal._connection.execute("PRAGMA database_list").fetchone()
    return str(db_row["file"] if hasattr(db_row, "keys") else db_row[2])


@functools.lru_cache(maxsize=8)
def _claude_cli_supports_effort(binary: str) -> bool:
    """Once-per-process probe: does ``binary --help`` advertise ``--effort``?

    An older CLI dies on an unknown flag, so ``--effort`` is appended only when
    the help text proves support. Any probe failure (missing binary, timeout)
    reads as unsupported — a spawn must never fail because of the effort knob."""
    try:
        proc = subprocess.run(
            [binary, "--help"], capture_output=True, text=True, timeout=10, check=False
        )
    except Exception:  # noqa: BLE001 - probe failure means "don't pass the flag"
        LOG.warning(
            "claude --effort capability probe failed for %s; spawning without the flag",
            binary,
            exc_info=True,
        )
        return False
    supported = "--effort" in f"{proc.stdout or ''}{proc.stderr or ''}"
    if not supported:
        LOG.warning(
            "claude CLI at %s does not support --effort; router-decided effort is "
            "recorded on the attempt but not applied to this session",
            binary,
        )
    return supported


def _maybe_append_effort(argv: list[str], effort: str | None) -> list[str]:
    """Append ``--effort <level>`` when a level is set and the CLI supports it."""
    level = (effort or "").strip()
    if level and _claude_cli_supports_effort(argv[0]):
        argv.extend(("--effort", level))
    return argv


class SessionSupervisor:
    """Spawn processes and service their durable control state."""

    def __init__(
        self,
        dal: SessionsDal | None = None,
        *,
        db_path: str | None = None,
        manifest: SessionManifest | None = None,
        poll_interval: float = 0.5,
        kill_grace: float = 3.0,
        claude_binary: str = "claude",
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        liveness: Callable[[int], bool] = pid_is_alive,
        notifier: Callable[[str, str], object] = notify.push,
        allowed_ssh_hosts: Sequence[str] | None = None,
        pgid_child_counter: Callable[[int], int] = _pgid_live_children,
    ) -> None:
        self.dal = dal or SessionsDal(db_path or default_db_path())
        self.manifest = manifest or SessionManifest()
        self.poll_interval = max(0.05, poll_interval)
        self.kill_grace = max(0.0, kill_grace)
        self.claude_binary = _resolve_claude_cli() if claude_binary == "claude" else claude_binary
        self._process_factory = process_factory
        self._liveness = liveness
        self._notifier = notifier
        self._allowed_ssh_hosts = tuple(allowed_ssh_hosts or ())
        self._policy = load_policy()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._processes: dict[str, _ManagedProcess] = {}
        # H-39: threads stay registered through reader AND finalizer work so
        # close() can join them before the DAL is closed. `_processes` alone is
        # insufficient — the reader drops its handle before `_process_finished`.
        self._background_threads: dict[str, threading.Thread] = {}
        # H-39: track all sessions ever spawned by this supervisor so crashed
        # finalizers can be explicitly failed on close. Never removed.
        self._ever_owned: set[str] = set()
        # H-39: every session this supervisor has taken responsibility for --
        # launched OR adopted after a restart. Deliberately NOT the same set as
        # `_ever_owned`, which also drives the close-time explicit-failure rule
        # for RUNNING rows; that rule must stay limited to sessions we launched
        # (a launched session still RUNNING after its reader was joined means the
        # finalizer crashed, whereas an adopted session has no reader and RUNNING
        # is simply its normal state). Never removed: `_adopted` is popped the
        # moment a session terminalizes, which is before the manifest is written.
        self._ever_responsible: set[str] = set()
        self._adopted: dict[str, int] = {}
        self._killing: set[str] = set()
        # A dead login may only cause one fresh-account retry for a session.  Keeping
        # this process-local is sufficient: a session id is never reused.
        self._auth_retry_consumed: set[str] = set()
        self._pgid_child_counter = pgid_child_counter
        # DRY-RUN dedupe: one would-kill line per (session, reason); re-armed when
        # the session shows fresh activity. Process-local is fine — the point is
        # not to spam the log every 0.5s poll pass.
        self._reaper_would_kill_logged: set[str] = set()
        # Durable events remain the cross-restart/process dedupe for R-1
        # re-arms.  This fast path closes the locked-DB hole: a failed event
        # insert must not turn one escalation into a poll-rate notification
        # storm while the supervisor stays alive.  Keyed by session and scoped
        # to ONE park episode: a session that re-parks starts a new record, so
        # the first episode's escalations cannot silence the second's, and the
        # entry is dropped in _finish so a long-lived supervisor does not
        # accumulate one per session it ever parked.
        self._max_park_extension_notified: dict[str, _ParkEscalations] = {}
        # WP10: the swarm resume sweep runs ONCE per supervisor lifetime (startup),
        # never per run_once pass — see resume_swarms_once.
        self._swarm_resume_done = False
        # Phase 3 scope locks. SHIPS DARK: scope_locks_mode() is 'off' by default
        # and every gate method returns before touching the DB, the filesystem or
        # git while it is, so the supervisor's behaviour is byte-for-byte what it
        # was. Constructed eagerly because the object itself is inert — it does not
        # build a PathLockStore until a claim is actually made.
        self._scope = SessionScopeGate(self.dal)
        # H-39 lifecycle: ordered, idempotent, concurrent-safe close.
        self._lifecycle_lock = threading.RLock()
        self._close_in_progress = False
        self._closed = False
        self._dal_closed = False
        self._close_done = threading.Event()
        self._close_error: BaseException | None = None
        # H-39 case 1: launches in flight between child creation and reader
        # registration. `close` drains these before snapshotting threads so a
        # child born a microsecond before close cannot escape it. The condition
        # shares `_lock`, which is the lock registration already runs under.
        self._launch_cv = threading.Condition(self._lock)
        self._inflight_launches: dict[int, LaunchTicket] = {}
        self._launch_seq = 0
        # H-39 case 3: when `close` is called from a reader/finalizer it cannot
        # join itself, so the DAL close is handed to the last background thread
        # instead of being performed under a live reader.
        self._dal_close_pending = False
        # Longhaul teardown uses the same last-one-out boundary. Although its
        # terminal hook is synchronous on a reader thread, deferring its close
        # until that thread unregisters keeps the store-before-DAL order without
        # leaving the engine open after a self-joined shutdown.
        self._longhaul_close_pending = False
        # H-39 case 4 evidence: sessions whose finalizer raised. Recorded by the
        # reader so `close` can terminalize + flush them instead of reporting a
        # clean shutdown over a lost manifest.
        self._finalizer_failures: dict[str, str] = {}
        # H-39: manifest obligations, keyed by session id and never removed.
        # `_finalize_on_shutdown` drives the normal flush off `dal.list_sessions`,
        # which is unavailable exactly when it matters most -- a DAL that is
        # already closed or externally broken. Without this ledger, an owned
        # session that completed cleanly and then lost its manifest is invisible
        # to a close over a dead connection, and the close reports success.
        # Recorded from the row we already hold whenever an owned session is
        # observed terminal; discharged only by the file existing on disk.
        self._manifest_obligations: dict[str, ManifestObligation] = {}
        # H-39: register with the global set and ensure atexit draining is armed.
        _ensure_atexit_registered()
        with _OPEN_SUPERVISORS_LOCK:
            _OPEN_SUPERVISORS.add(self)

    def spawn(
        self,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None = None,
        title: str | None = None,
        extra_write_roots: list[str] | None = None,
        title_prefix: str | None = None,
        resume_session_ref: str | None = None,
        orchestrator_owned: bool = False,
        orchestrator_run_id: str | None = None,
        granted_roots: list[str] | None = None,
        idle_minutes: float | None = None,
        effort: str | None = None,
        *,
        allow_task_fanout: bool = False,
        declared_scope: Sequence[ScopeClaim] | None = None,
    ) -> str:
        """Create and launch one bridge session, returning its durable id.

        ``effort`` (048/G4) is the router-decided reasoning effort
        (low|medium|high|xhigh), threaded into the bridge argv as ``--effort``
        behind a once-per-process CLI capability probe; None keeps today's argv.

        ``extra_write_roots`` widens the OS-sandbox write confinement beyond the
        project dir + CLI state dirs -- used by the fast lane to let "make a folder
        on my desktop" actually reach ``~/Desktop``. It only widens *where* files may
        be created; deletion and money moves still escalate via the approval gate
        regardless of scope. Callers must pass only vetted directories (see
        ``intake.fastlane.parse_target_location``).

        ``granted_roots`` (P3) are the session's project-scope grants -- the
        project's validated ``root_dirs`` + ``allowed_dirs``, computed SERVER-SIDE at
        dispatch and FROZEN onto the session row here. They serve two purposes,
        derived from this single persisted value so the two can never desync:
        ``_launch`` adds them to the OS-sandbox write roots (so an in-scope write
        actually lands), and the PreToolUse hook reads them back to classify a write
        inside any granted root as in-scope (auto) instead of parking it. They ONLY
        widen the in-project WRITE boundary; deletes/money/secret reads still hard-stop.

        ``orchestrator_owned`` marks the session hands-off (auto-approve safe /
        escalate money+delete) BEFORE the process launches -- essential, because a
        session that reaches its first gated tool call before the ownership mark lands
        would park for a human (a race the post-spawn mark loses on fast agents).

        ``idle_minutes`` (D8) is the per-session idle threshold the A2 reaper
        honors over its global default. Written at ROW CREATION (not post-spawn)
        so there is no window in which the session exists under the wrong
        threshold — longhaul passes its lane override here on both fresh spawns
        and native resumes.

        ``declared_scope`` (Phase 3) is the session's cross-lane path claim set.
        Leave it None and it is DERIVED from ``project_dir`` + ``granted_roots``,
        i.e. from the very values frozen onto the row — which is what makes it
        impossible for the sandbox grant and the lock claim to disagree. Pass it
        only when a planner genuinely knows a narrower scope. Inert until
        ``scope_locks_mode()`` is turned on; see ``sessions.scope_wiring``."""

        self._require_open("spawn")
        session_id = new_id("ses")
        session_ref = resume_session_ref or str(uuid.uuid4())
        persisted_title = (
            " ".join(
                part.strip()
                for part in (title_prefix, title)
                if isinstance(part, str) and part.strip()
            )
            or None
        )
        now = utc_now_iso()
        recorded_project_dir = _recorded_project_dir(project_dir)
        self.dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "project_dir": recorded_project_dir,
                "provider": "claude",
                "session_ref": session_ref,
                "state": SessionState.STARTING.value,
                "model": model,
                "title": persisted_title,
                "prompt": prompt,
                "budget_usd_max": budget_usd_max,
                "cost_usd": 0.0,
                "kill_requested": 0,
                # D8: per-session reaper threshold frozen at row creation — no
                # post-spawn write, so no race window at the default threshold.
                # bool is excluded explicitly: a YAML `true` leaking this far
                # must not become a 1.0-minute reap threshold.
                "idle_minutes": (
                    float(idle_minutes)
                    if isinstance(idle_minutes, int | float)
                    and not isinstance(idle_minutes, bool)
                    and float(idle_minutes) > 0
                    else None
                ),
                "last_activity_at": now,
                "created_at": now,
                "updated_at": now,
                # Freeze the validated project scope onto the row BEFORE launch, so
                # the sandbox (below) and the hook (server-side lookup) both read the
                # same grants and never trust a hook-supplied path.
                #
                # extra_write_roots is FOLDED IN HERE rather than left as a pure call
                # parameter, and that closes two real defects found by review:
                #  1. The scope claim (open_for_launch, below) derives from
                #     granted_roots, so an unfolded extra_write_root was a write root
                #     the OS sandbox allowed (see _launch's extra_write_roots
                #     assembly) but that NOTHING claimed -- two sessions could both
                #     write one shared root while neither held a lock on it.
                #  2. A scope-PARKED spawn re-launches via _launch_queued, which
                #     re-reads granted_roots from this row but cannot recover a call
                #     parameter -- session_spawn_queue has no column for it. The
                #     parked session silently launched WITHOUT a write root it was
                #     promised.
                # Folding also honours this block's own stated principle, quoted
                # from :882 -- "one persisted source of truth for both the sandbox
                # and the hook" -- which a parameter-only write root violated.
                "granted_roots": encode_granted_roots(
                    _merge_session_standing_roots(
                        list(granted_roots or []) + list(extra_write_roots or []),
                        project_dir=str(project_dir),
                    )
                ),
            }
        )
        try:
            # Establish orchestrator ownership BEFORE launch, so the session's very
            # first gated tool call already routes through the approve-safe policy
            # (auto-approve safe work) instead of racing the mark and parking.
            if orchestrator_owned:
                self.dal.mark_orchestrator_session(session_id, orchestrator_run_id)
            project = Path(project_dir).expanduser().resolve(strict=True)
            if not project.is_dir():
                raise ValueError("project_dir must be a directory")
            if not model.strip() or not prompt:
                raise ValueError("model and prompt are required")
            if budget_usd_max is not None and budget_usd_max < 0:
                raise ValueError("budget_usd_max cannot be negative")
            cli_title = persisted_title if title_prefix else None
            argv = (
                self._bridge_resume_argv(
                    resume_session_ref, prompt, cli_title, budget_usd_max, effort=effort
                )
                if resume_session_ref
                else self._bridge_launch_argv(
                    session_ref,
                    model,
                    prompt,
                    budget_usd_max,
                    cli_title,
                    effort=effort,
                    allow_task_fanout=allow_task_fanout,
                )
            )
            # Phase 3: DECLARE and CLAIM before the process exists, never after —
            # a session that has already started writing cannot be un-started, so
            # a claim taken post-launch would only ever be able to report a
            # collision that had already happened. No-op while the mode is off.
            #
            # NOT inside create_session's transaction, and that is forced rather
            # than chosen: PathLockStore opens its own BEGIN IMMEDIATE through the
            # DAL's re-entrant lock, so a claim nested inside a DAL transaction
            # raises "cannot start a transaction within a transaction". The row is
            # therefore committed in 'starting' first and the claim taken here —
            # which is also the state a refused claim is left parked in.
            scope = self._scope.open_for_launch(
                session_id,
                title=persisted_title,
                project_dir=recorded_project_dir,
                granted_roots=granted_roots,
                declared_scope=declared_scope,
            )
            if scope.fenced:
                # Adopted away: retrying is wrong at any generation, so this is a
                # spawn failure like any other (the except below terminalizes).
                raise RuntimeError(f"scope lease fenced: {scope.detail}")
            if scope.parked:
                # REFUSED, NOT FAILED. Raising would break every caller — spawn's
                # contract is "returns a durable session id" and swarm/longhaul
                # both immediately write to the row it names. So the row stays in
                # 'starting' with a durable waiter row behind it, and the launch is
                # handed to the spawn ingress (_process_spawn_queue), which retries
                # the whole claim every pass. Reusing that machinery is why there
                # is no 'parked' session state to reconcile, back-fill or leak.
                self._park_spawn(
                    session_id,
                    project=str(project),
                    model=model,
                    prompt=prompt,
                    budget_usd_max=budget_usd_max,
                    title=persisted_title,
                    resume_session_ref=resume_session_ref,
                    detail=scope.detail,
                )
                return session_id
            self._launch(
                session_id,
                argv,
                cwd=str(project),
                expected=SessionState.STARTING,
                resumed=False,
                extra_write_roots=extra_write_roots,
            )
        except BaseException as exc:
            # H-39 case 2: a launch that lost the close race must not write to a
            # closed DAL on its way out. The session is already covered by
            # `_finalize_on_shutdown`, which owns terminalization at shutdown.
            if not self._dal_closed:
                try:
                    _record_session_error(self.dal, session_id, str(exc) or type(exc).__name__)
                except Exception:  # noqa: BLE001 - preserve the original spawn failure
                    LOG.exception("could not persist spawn error for session %s", session_id)
                self._finish(session_id, SessionState.FAILED)
            if isinstance(exc, Exception):
                raise SessionSpawnError(session_id, exc) from exc
            raise
        return session_id

    def _park_spawn(
        self,
        session_id: str,
        *,
        project: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None,
        title: str | None,
        resume_session_ref: str | None,
        detail: str,
    ) -> None:
        """Hand a scope-refused spawn to the durable ingress instead of launching.

        The session row stays exactly where ``create_session`` left it —
        ``starting``, no pid — and a ``session_spawn_queue`` request carries the
        launch. ``_process_spawn_queue`` re-runs the whole all-or-nothing claim on
        every pass and only marks the request launched once the scope is actually
        granted, so this is a retry loop with a durable, crash-surviving cursor
        rather than an in-memory one.

        RESUME SPAWNS ARE NOT QUEUED. ``_launch_queued`` builds a FRESH-launch
        argv (``--session-id``), so re-driving a ``--resume`` spawn through it
        would start a new conversation under the resumed session's ref. It is
        unreachable today — longhaul is the only caller that passes
        ``resume_session_ref`` and every longhaul session is scope-exempt by its
        title marker, so it never reaches a refusal — and if that ever changes,
        the STARTING-stall reaper (``_STARTING_STALL_SECONDS``) terminalizes the
        row rather than letting it sit forever. Failing visibly beats resuming
        the wrong conversation.
        """
        LOG.info(
            "scope parked spawn %s",
            json.dumps(
                {
                    "event": "session.scope_parked",
                    "session_id": session_id,
                    "queued": not resume_session_ref,
                    "detail": detail,
                },
                sort_keys=True,
            ),
        )
        if resume_session_ref:
            return
        if self.dal.has_queued_spawn(session_id):
            return
        self.dal.enqueue_spawn(
            session_id=session_id,
            project_dir=project,
            model=model,
            prompt=prompt,
            budget_usd_max=budget_usd_max,
            title=title,
        )

    def _tool_exposure_argv(self, argv: list[str], session_ref: str) -> list[str]:
        """Compute, record and (only in enforce mode) apply the tool-exposure decision.

        DARK BY DEFAULT, AND BYTE-IDENTICAL WHEN DARK. The mode check is the first
        thing that happens and returns the caller's own list object unchanged, so with
        ``OMNIAGENTOS_TOOL_CATALOG`` unset this method is indistinguishable from not
        existing. The whole body is exception-guarded for the same reason: an exposure
        computation is an optimisation, and an optimisation must never be able to stop
        a session from launching.

        **Shadow mode changes nothing and records the decision**, which is the point —
        the durable observation is how the ramp is measured before anybody turns
        enforcement on.

        **Enforce mode is deliberately narrow.** ``enforce_argv_patch`` can only act on
        native tool names that ``toolplane.session.SESSION_TOOL_CAPABILITY`` maps into
        this catalog: Read, Glob, Grep, Write, Edit, MultiEdit, NotebookEdit. Bash,
        WebFetch, WebSearch, TodoWrite and the rest have no catalog equivalent, so they
        are never touched. That incompleteness is honest rather than incidental — see
        the module docstring of ``toolplane/exposure.py``.

        Grants are deliberately EMPTY here. The session lane holds no connector grants:
        a connector capability is reached through the broker with a per-run bearer token
        (``connectors/store.py::issue_token``), never as an entry in a session's tool
        list. Passing the empty grant therefore describes the session lane truthfully,
        and the built-ins are what remain visible.

        The approval-continuation resume argv (``_resume_approved``) is intentionally NOT
        wired: it re-issues one already-approved action under a single-use action_hash
        grant, so narrowing its toolset could only refuse the very call it exists to let
        through.
        """
        try:
            from omniagentos.toolplane.config import tool_catalog_mode

            mode = tool_catalog_mode()
            if mode == "off":
                return argv

            from omniagentos.toolplane.exposure import (
                ExposureContext,
                compute_exposure,
                enforce_argv_patch,
                log_exposure_decision,
            )

            ctx = ExposureContext(session_id=session_ref, lane="session")
            decision = compute_exposure(ctx, grants=())
            log_exposure_decision(decision, ctx)
            if mode == "enforce":
                return enforce_argv_patch(argv, decision)
            return argv
        except Exception:  # noqa: BLE001 - exposure must never break a launch
            LOG.exception("tool exposure computation failed for session %s", session_ref)
            return argv

    def _bridge_launch_argv(
        self,
        session_ref: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None,
        title: str | None = None,
        effort: str | None = None,
        *,
        allow_task_fanout: bool = False,
    ) -> list[str]:
        argv = [
            self.claude_binary,
            "-p",
            prompt,
            "--settings",
            bridge_settings_path(),
            "--output-format",
            "stream-json",
            # CLI v2.1.216 requires --verbose whenever --print is combined with
            # --output-format=stream-json, else it exits 1 before emitting any JSON
            # (live e2e: 100% launch failure without this).
            "--verbose",
            "--include-hook-events",
        ]
        # A bridge executor session is a SINGLE agent -- decomposition/fan-out is
        # the Orchestrator's job, not a nested subagent's. Disallow the Task tool so
        # the executor does the work itself: nested subagents spawn their own tool
        # calls that park the session on approval (TaskCreate) instead of completing.
        #
        # PKG-INSESSION-FANOUT: the ONE exception is a claude swarm worker under
        # the in-session flag (``allow_task_fanout``). Task stays exposed at the
        # CLI, and EVERY Task call is still gated server-side by the ``.*``
        # PreToolUse hook against a live coordinator grant (no grant → deny), so
        # an ungranted worker is exactly as locked down as this flag made it.
        if not allow_task_fanout:
            argv.extend(("--disallowedTools", "Task"))
        argv.extend(("--model", model))
        # Advisory by default: a CLI-level cap aborts the task INSIDE the worker
        # ("Budget limit reached ($X of $Y)") and stops it spawning agents, which
        # is exactly the hard blocker budgets are no longer allowed to be. Spend is
        # still recorded on the session row. See omniagentos.budget.policy.
        cli_cap = session_budget_cap(budget_usd_max)
        if cli_cap is not None:
            argv.extend(("--max-budget-usd", str(cli_cap)))
        # NOTE: the claude CLI has no --title flag — the session title (incl.
        # the [longhaul:<attempt>] marker) lives only on the sessions row.
        argv.extend(("--session-id", session_ref))
        return self._tool_exposure_argv(_maybe_append_effort(argv, effort), session_ref)

    def _bridge_resume_argv(
        self,
        session_ref: str,
        prompt: str,
        title: str | None,
        budget_usd_max: float | None,
        effort: str | None = None,
    ) -> list[str]:
        argv = [
            self.claude_binary,
            "-p",
            "--resume",
            session_ref,
            prompt,
            "--settings",
            bridge_settings_path(),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--disallowedTools",
            "Task",
        ]
        # Advisory by default: a CLI-level cap aborts the task INSIDE the worker
        # ("Budget limit reached ($X of $Y)") and stops it spawning agents, which
        # is exactly the hard blocker budgets are no longer allowed to be. Spend is
        # still recorded on the session row. See omniagentos.budget.policy.
        cli_cap = session_budget_cap(budget_usd_max)
        if cli_cap is not None:
            argv.extend(("--max-budget-usd", str(cli_cap)))
        # NOTE: the claude CLI has no --title flag (see _bridge_launch_argv).
        return self._tool_exposure_argv(_maybe_append_effort(argv, effort), session_ref)

    def _launch_queued(self, session_id: str, prompt: str, budget_usd_max: float | None) -> None:
        """Launch an already-created session from a drained spawn-queue request."""
        session = self.dal.get_session(session_id)
        if session is None:
            raise RuntimeError(f"queued spawn for unknown session {session_id}")
        session_ref = session.get("session_ref")
        if not isinstance(session_ref, str) or not session_ref:
            raise RuntimeError(f"session {session_id} is missing a session_ref")
        argv = self._bridge_launch_argv(
            session_ref,
            str(session.get("model") or ""),
            prompt,
            budget_usd_max,
            str(session.get("title") or "") or None,
        )
        self._launch(
            session_id,
            argv,
            cwd=str(session["project_dir"]),
            expected=SessionState.STARTING,
            resumed=False,
        )

    def _process_spawn_queue(self) -> None:
        """Drain the durable spawn ingress (T-OPS-003).

        Each request is claimed (marked launched) BEFORE Popen so a crash mid-launch
        never double-spawns; a launch failure terminalizes the session.

        Phase 3 adds the scope gate, and its position is load-bearing: it runs
        BEFORE the request is claimed. A scope-refused session must leave its
        request ``queued`` so this same loop retries the claim on the next pass;
        claiming first and then discovering the refusal would burn the request and
        strand the session in ``starting`` with nothing left to launch it.
        """
        for request in self.dal.claim_spawn_requests():
            request_id = str(request["id"])
            session_id = str(request["session_id"])
            session = self.dal.get_session(session_id)
            if session is not None and bool(session.get("kill_requested")):
                self.dal.mark_spawn_result(request_id, "failed", "cancelled before launch")
                target = (
                    SessionState.CANCELLED
                    if session.get("killed_by") == "cancel_requested"
                    else SessionState.KILLED
                )
                self._finish(session_id, target, killed_by="operator")
                continue
            if session is not None:
                # Idempotent: a session that already holds its scope (this loop
                # granted it on an earlier pass and the launch then failed) simply
                # has its lease extended.
                scope = self._scope.open_for_row(session)
                if scope.parked:
                    continue  # still blocked; the request stays queued for next pass
                if scope.fenced:
                    self.dal.mark_spawn_result(
                        request_id, "failed", f"scope lease fenced: {scope.detail}"
                    )
                    self._finish(session_id, SessionState.FAILED)
                    continue
            if not self.dal.mark_spawn_result(request_id, "launched"):
                continue
            budget = request.get("budget_usd_max")
            try:
                self._launch_queued(
                    session_id,
                    str(request.get("prompt") or ""),
                    float(budget) if isinstance(budget, int | float) else None,
                )
            except Exception:
                LOG.exception("spawn ingress failed to launch %s", session_id)
                self._finish(session_id, SessionState.FAILED)

    def _launch(
        self,
        session_id: str,
        argv: Sequence[str],
        *,
        cwd: str,
        expected: SessionState,
        resumed: bool,
        extra_write_roots: list[str] | None = None,
    ) -> None:
        self._require_open("launch")
        # H-39 case 1: claim an in-flight slot BEFORE any child can exist, so a
        # concurrent close() blocks on this launch instead of racing past it.
        # The claim re-checks the close gate under `_lock`, which makes
        # gate-check and slot-claim atomic with respect to close().
        ticket = self._begin_launch(session_id)
        try:
            self._launch_inflight(
                ticket,
                session_id,
                argv,
                cwd=cwd,
                expected=expected,
                resumed=resumed,
                extra_write_roots=extra_write_roots,
            )
        finally:
            # Releases the slot and, if registration never completed, terminates
            # the child this launch created rather than orphaning it.
            self._end_launch(ticket)

    def _begin_launch(self, session_id: str) -> LaunchTicket:
        """Claim an in-flight launch slot, refusing once close has started."""
        with self._launch_cv:
            if self._stop.is_set() or self._close_in_progress or self._closed:
                raise RuntimeError(
                    f"session supervisor refuses launch for {session_id}: shutting down or closed"
                )
            self._launch_seq += 1
            ticket = LaunchTicket(token=self._launch_seq, session_id=session_id)
            self._inflight_launches[ticket.token] = ticket
            return ticket

    def _note_inflight_child(self, ticket: LaunchTicket, process: Any) -> None:
        """Record the child the instant it exists, so close can still reach it."""
        with self._launch_cv:
            ticket.child = process

    def _end_launch(self, ticket: LaunchTicket) -> None:
        """Release the slot; terminate the child if registration never happened."""
        with self._launch_cv:
            self._inflight_launches.pop(ticket.token, None)
            orphan = None if ticket.registered else ticket.child
            ticket.child = None
            self._launch_cv.notify_all()
        if orphan is not None:
            # Refused registration, or an exception between spawn and start:
            # the child has no reader and nobody else will ever reap it.
            self._terminate_child_process(orphan)

    def _launch_inflight(
        self,
        ticket: LaunchTicket,
        session_id: str,
        argv: Sequence[str],
        *,
        cwd: str,
        expected: SessionState,
        resumed: bool,
        extra_write_roots: list[str] | None = None,
    ) -> None:
        if not sandbox.sandbox_available():
            raise RuntimeError(
                "bridge_session_no_sandbox_refused: the OS sandbox is unavailable, "
                "so this session would run without write/secret confinement and "
                "with the classifier as its sole gate; refusing to launch"
            )
        # AC-policy #B4: the bridged Claude session is the primary UNATTENDED
        # execution surface, where classify_shell is the sole gate. Wrap its launch
        # in the OS sandbox confining file writes+deletes to the session's project
        # dir + the CLI's own state dirs, so a classifier miss can never cause
        # out-of-scope file harm (e.g. overwriting ~/.ssh/authorized_keys).
        #
        # AC-policy hook-auth: mint (or rotate) THIS launch's narrowly-scoped
        # hook-eval credential BEFORE the sandboxed process starts, so the
        # PreToolUse hook can read it from turn one, and re-open ONLY this
        # session's own file in its own profile below -- never var/secrets, and
        # never a sibling session's file (see sessions.hook_token for why this is
        # safe even though the agent's own Bash/Read tools share the sandbox).
        hook_token.issue_hook_token(session_id)
        existing_ssh_grant = (
            ssh_keys.read_ssh_key_grant(session_id)
            if ssh_keys.ssh_key_grant_path(session_id).exists()
            else list(self._allowed_ssh_hosts)
        )
        ssh_keys.issue_ssh_key_grant(session_id, existing_ssh_grant)
        # Multiple Claude accounts: run this session under the next enabled account
        # (round-robin over CLAUDE_CONFIG_DIR logins). A config-dir account contributes
        # its dir as a write root + CLAUDE_CONFIG_DIR; a token/api-key account
        # contributes only auth env. Selecting the DEFAULT ~/.claude is a no-op
        # (identical to not setting CLAUDE_CONFIG_DIR), so nothing changes until an
        # operator enables another account.
        account_config_dir, account_env, account_id, account_label = _resolve_spawn_account()
        account_roots = [account_config_dir] if account_config_dir else []
        # WP2 durable inflight attribution: persist WHICH account this session
        # runs under onto the sessions row (migration 045 column). limit_state
        # counts live sessions per account through this; a reservation made by
        # a scheduler converts to real inflight at exactly this write. Written
        # on every (re)launch so an auth-retry account switch stays accurate.
        # Fail-safe: attribution must never break a launch.
        if account_id:
            try:
                self.dal.set_account_id(session_id, account_id)
            except Exception:  # noqa: BLE001
                LOG.debug("could not persist account attribution for %s", session_id, exc_info=True)
        # P3: read the session's frozen granted scope (project root_dirs +
        # allowed_dirs) from the row and widen the sandbox to it, so a write the hook
        # auto-approves as in-scope actually lands. Reading it HERE (not from a spawn
        # arg) means resume/queued launches confine to the SAME roots as the initial
        # launch -- one persisted source of truth for both the sandbox and the hook.
        granted_roots = decode_granted_roots(self.dal.get_session(session_id))
        launch_argv = sandbox.wrap_command(
            list(argv),
            cwd,
            # The session's own project + CLI-state roots, any vetted target dir the
            # fast lane granted, the project's granted scope, and the selected
            # account's config dir. Secret/system denies in the SBPL profile follow
            # the allow, so a widened write root never re-opens ~/.ssh, var/secrets, or
            # a system path (see build_profile).
            # dict.fromkeys de-duplicates while preserving order: extra_write_roots
            # now ALSO arrives inside the persisted granted_roots (see the spawn
            # site), so the two overlap by design. Duplicate SBPL allow-rules are
            # harmless, but a deduped list keeps the generated profile readable.
            extra_write_roots=list(
                dict.fromkeys(
                    sandbox.session_write_roots(cwd)
                    + list(extra_write_roots or [])
                    + granted_roots
                    + account_roots
                )
            ),
            extra_read_allow=sandbox.session_hook_token_read_allow(session_id),
            ssh_key_grant_session_id=session_id,
        )
        session_env = build_clean_env(session_id=session_id)
        # Tell this session's hook where THIS stack's control plane is. Added
        # after the scrub on purpose: build_clean_env stays a pure allow-list of
        # inherited names, and this is a value we compute, not one we inherit.
        # None = undeterminable -> stamp nothing and let the hook warn.
        api_base_url = resolved_api_base_url()
        if api_base_url:
            session_env[API_BASE_URL_ENV] = api_base_url
        if account_config_dir:
            session_env["CLAUDE_CONFIG_DIR"] = account_config_dir
        for _env_key, _env_val in account_env.items():
            session_env[_env_key] = _env_val
        if launch_argv and launch_argv[0].endswith("sandbox-exec"):
            # The launch is sandbox-confined: keep tool scratch inside the project
            # (the strict profile does not allow the system temp dir), and pre-create
            # Claude Code's Bash scratch dir (/tmp/claude-<uid>, an allowed write
            # root) so a fresh host does not need /tmp itself writable.
            session_tmp = os.path.join(cwd, ".tmp")
            try:
                os.makedirs(session_tmp, exist_ok=True)
                os.makedirs(sandbox.claude_tmp_root(), exist_ok=True)
                session_env["TMPDIR"] = session_tmp
            except OSError:
                pass
        process = self._process_factory(
            launch_argv,
            cwd=cwd,
            env=session_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        # H-39 case 1: hand the child to the ticket immediately. From here on a
        # concurrent close that strands this launch can still terminate it, and
        # `_end_launch` reaps it on any abort below.
        self._note_inflight_child(ticket, process)
        handle = _ManagedProcess(
            session_id=session_id,
            process=process,
            resumed=resumed,
            account_id=account_id,
            account_label=account_label,
            account_config_dir=account_config_dir,
            stream_events=[],
        )
        self.dal.set_pid(session_id, process.pid)
        if not self.dal.update_session_state(
            session_id, SessionState.RUNNING.value, expect=expected.value
        ):
            raise RuntimeError(f"lost state transition while launching {session_id}")
        reader = threading.Thread(
            target=self._read_process,
            args=(handle,),
            name=f"session-{session_id}",
            daemon=True,
        )
        handle.thread = reader
        # H-39 atomic registration: check close-in-progress under lock so the
        # close() snapshot never misses a just-spawned child. Close cannot have
        # got past the launch drain while this ticket is in flight, so this is
        # belt-and-braces for a close that started before `_begin_launch` won
        # the lock -- in which case `_begin_launch` would already have refused.
        with self._lock:
            if self._stop.is_set() or self._close_in_progress or self._closed:
                raise RuntimeError(
                    f"session supervisor refuses registration for {session_id}: close in progress"
                )
            self._processes[session_id] = handle
            # Register before start so close() never misses a live reader/finalizer.
            self._background_threads[session_id] = reader
            # H-39: track for crashed-finalizer detection on close.
            self._ever_owned.add(session_id)
            self._ever_responsible.add(session_id)
            if session_id not in self._manifest_obligations:
                self._manifest_obligations[session_id] = ManifestObligation(
                    session_id=session_id,
                    recorded_at=utc_now_iso(),
                )
            self._adopted.pop(session_id, None)
            self._finalizer_failures.pop(session_id, None)
        # Started while the ticket is still in flight, so close() never observes
        # a registered-but-unstarted thread (`is_alive()` is False before
        # `start()`, which a naive join loop would silently skip).
        reader.start()
        ticket.registered = True

    def _read_process(self, handle: _ManagedProcess) -> None:
        # H-39: the whole reader+finalizer body is one registered background unit.
        # Unregister only after `_process_finished` returns so close() can join
        # through manifest / terminal event persistence.
        try:
            seen_cost = False
            stream = handle.process.stdout
            try:
                # H-39 case 2: a reader must not *start* after the DAL closed.
                # Every statement in this loop is a DAL write.
                if stream is not None and not self._dal_closed:
                    for line in stream:
                        # H-39 case 2: nor *resume* across a DAL close mid-stream.
                        # Draining the rest of the pipe would be a use-after-close.
                        if self._dal_closed:
                            break
                        try:
                            event: Any = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        if handle.stream_events is not None:
                            handle.stream_events.append(event)
                        try:
                            self.dal.touch_activity(handle.session_id, utc_now_iso())
                            session_ref = event.get("session_id")
                            if isinstance(session_ref, str) and session_ref:
                                self.dal.set_session_ref(handle.session_id, session_ref)
                        except Exception:
                            if self._dal_closed:
                                break
                            LOG.exception(
                                "error updating activity for session %s", handle.session_id
                            )
                        # Capture auth hints from system api_retry events before terminal result
                        if event.get("type") == "system" and event.get("subtype") == "api_retry":
                            if (
                                event.get("error_status") == 401
                                or event.get("error") == "authentication_failed"
                            ):
                                handle.auth_hint = True
                                if handle.auth_failure_detail is None:
                                    handle.auth_failure_detail = _auth_failure_detail(event)
                        if event.get("type") == "result":
                            subtype = str(event.get("subtype") or "").lower()
                            terminal_reason = str(event.get("terminal_reason") or "").lower()
                            is_error_flag = bool(event.get("is_error"))
                            handle.result_error = (
                                is_error_flag
                                or subtype.startswith("error")
                                or "error" in terminal_reason
                            )
                            if handle.result_error:
                                detail = _auth_failure_detail(event)
                                if _is_auth_failure(event):
                                    # Result event itself carries auth signal (system hint or text)
                                    handle.auth_failure = True
                                    handle.auth_failure_detail = detail
                                elif handle.auth_hint and handle.auth_failure_detail is not None:
                                    # System api_retry set auth_hint (401 or authentication_failed)
                                    # + result is error + prior assistant text has auth keyword = corroborated auth failure
                                    handle.auth_failure = True
                                # Assistant-text-only (no system hint, no result detail): do NOT promote
                                # to avoid false positives on tasks that discuss auth issues
                            denials = event.get("permission_denials")
                            handle.permission_denied = isinstance(denials, list) and bool(denials)
                            if isinstance(denials, list):
                                hashes: set[str] = set()
                                for denial in denials:
                                    if not isinstance(denial, dict):
                                        continue
                                    tool_name = denial.get("tool_name")
                                    tool_input = denial.get("tool_input")
                                    if isinstance(tool_name, str) and isinstance(tool_input, dict):
                                        hashes.add(action_hash(tool_name, tool_input))
                                handle.denied_action_hashes = hashes
                            cost = event.get("total_cost_usd")
                            if not seen_cost and isinstance(cost, int | float) and cost >= 0:
                                try:
                                    self.dal.add_cost(handle.session_id, float(cost))
                                    seen_cost = True
                                except Exception:
                                    if self._dal_closed:
                                        break
                                    LOG.exception(
                                        "error adding cost for session %s", handle.session_id
                                    )
                        elif event.get("type") == "assistant":
                            detail = _auth_failure_detail(event)
                            if detail is not None:
                                handle.auth_failure_detail = detail
                            # Live progress: capture the session's own TodoWrite checklist
                            # + the files it writes/edits, straight from the stream, so the
                            # board shows a real progress bar instead of an opaque spinner.
                            self._capture_progress(handle, event)
            finally:
                return_code = handle.process.wait()
                with self._lock:
                    current = self._processes.get(handle.session_id)
                    if current is handle:
                        self._processes.pop(handle.session_id, None)
                    killing = handle.session_id in self._killing
                if killing:
                    pass
                elif self._dal_closed:
                    # H-39 case 2: finalization is entirely DAL + manifest work.
                    # Record it as a finalizer failure so a later close reports
                    # the loss instead of the session vanishing silently.
                    self._record_finalizer_failure(handle.session_id, "dal-closed-before-finalize")
                else:
                    try:
                        self._process_finished(handle, return_code)
                    except Exception as exc:  # noqa: BLE001 - see below
                        # H-39 case 4: a finalizer that raises would otherwise
                        # (a) kill this daemon thread with an exception nobody
                        # observes and (b) leave the session non-terminal with no
                        # live thread, so close() joins nothing and reports
                        # success over a lost manifest. Record it instead;
                        # `_finalize_on_shutdown` terminalizes and flushes it.
                        self._record_finalizer_failure(
                            handle.session_id, f"{type(exc).__name__}: {exc}"
                        )
                        LOG.exception("session finalizer failed for %s", handle.session_id)
        finally:
            with self._lock:
                registered = self._background_threads.get(handle.session_id)
                if registered is threading.current_thread():
                    self._background_threads.pop(handle.session_id, None)
            # H-39 case 3: if a close() running on this very thread deferred the
            # DAL close, the last background unit out performs it -- after this
            # thread has unregistered and will make no further DAL calls.
            self._complete_deferred_dal_close()

    def _record_finalizer_failure(self, session_id: str, reason: str) -> None:
        with self._lock:
            self._finalizer_failures[session_id] = reason

    def _capture_progress(self, handle: _ManagedProcess, event: dict[str, Any]) -> None:
        """Pull the TodoWrite checklist + touched files from one assistant turn.

        Best-effort by contract: a malformed event or a persistence error is swallowed
        so live-progress capture can never disturb the session read loop."""
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            name = block.get("name")
            if name == "TodoWrite":
                self._store_todos(handle.session_id, tool_input.get("todos"))
            elif name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                if isinstance(path, str) and path.strip():
                    self._record_touched_file(handle, path.strip())

    def _store_todos(self, session_id: str, todos: Any) -> None:
        if not isinstance(todos, list):
            return
        normalized: list[dict[str, str]] = []
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            content = str(todo.get("content") or todo.get("activeForm") or "").strip()
            status = str(todo.get("status") or "pending").strip().lower()
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            if content:
                normalized.append({"content": content, "status": status})
        try:
            self.dal.set_session_todos(session_id, json.dumps(normalized))
        except Exception:  # noqa: BLE001 -- progress capture must never break the loop.
            LOG.debug("could not persist todos for %s", session_id, exc_info=True)

    def _record_touched_file(self, handle: _ManagedProcess, path: str) -> None:
        files = handle.touched_files
        if files is None:
            files = set()
            handle.touched_files = files
        if path in files:
            return
        files.add(path)
        try:
            self.dal.set_session_files(handle.session_id, json.dumps(sorted(files)))
        except Exception:  # noqa: BLE001 -- best-effort; never disturb the read loop.
            LOG.debug("could not persist files for %s", handle.session_id, exc_info=True)

    def _process_finished(self, handle: _ManagedProcess, return_code: int) -> None:
        session = self.dal.get_session(handle.session_id)
        if session is None or SessionState(str(session["state"])) in TERMINAL_SESSION_STATES:
            return
        if handle.auth_failure:
            error_text = handle.auth_failure_detail or "401 authentication failure"
            # F2: Require zero cost as necessary condition for auth path. A session
            # that cost money is not an auth failure — it's a task that ran and failed.
            session_cost = float(session.get("cost_usd") or 0)
            is_auth_failure_confirmed = session_cost == 0
            if is_auth_failure_confirmed:
                try:
                    _persist_session_error(self.dal, handle.session_id, error_text)
                except Exception:  # noqa: BLE001 -- terminal error visibility is best-effort.
                    LOG.exception("could not persist auth error for %s", handle.session_id)

                # Every 401 belongs to a broken credential, including the one on the
                # replacement attempt.  The retry cap limits respawns, not health updates.
                if handle.account_id:
                    try:
                        from omniagentos.accounts.service import mark_status, set_enabled

                        mark_status(handle.account_id, "error", error_text)
                        set_enabled(handle.account_id, False)
                    except Exception:  # noqa: BLE001 -- supervision must continue.
                        LOG.exception("could not disable account %s", handle.account_id)

                # F6: Move per-account notification OUTSIDE retry gate so EVERY
                # 401-disabled account emits its fix command.
                try:
                    account_name = handle.account_label or handle.account_id or "unknown"
                    config_dir = handle.account_config_dir or "~/.claude"
                    self._notifier(
                        f"Claude login broken — {handle.account_label or 'unknown'}",
                        f"Account {account_name} failed 401 authentication. "
                        f"To restore: CLAUDE_CONFIG_DIR={config_dir} claude /login",
                    )
                except Exception:  # noqa: BLE001 -- notification must never block retry.
                    LOG.exception("auth failure notification failed")

                # F5: Check kill_requested before attempting retry.
                retry = False
                with self._lock:
                    if (
                        handle.session_id not in self._auth_retry_consumed
                        and not bool(session.get("kill_requested"))
                        and not _scheduler_owns_respawn(session.get("title"))
                    ):
                        self._auth_retry_consumed.add(handle.session_id)
                        retry = True
                if retry and not self._stop.is_set():
                    # This runs after the old reader removed its handle and outside the
                    # lock, so _launch can safely register the replacement process.
                    # H-39: never start a replacement once shutdown has begun.
                    try:
                        if self.dal.update_session_state(
                            handle.session_id,
                            SessionState.STARTING.value,
                            expect=SessionState.RUNNING.value,
                        ):
                            # F4: Mint fresh uuid for the retry to avoid "Session ID
                            # already in use" on same-config-dir retries.
                            self.dal.set_session_ref(handle.session_id, str(uuid.uuid4()))
                            # F1: Use full prompt if persisted, fall back to title.
                            prompt = str(
                                session.get("prompt")
                                or session.get("title")
                                or "Resumed after auth failure"
                            )
                            budget = session.get("budget_usd_max")
                            self._launch_queued(
                                handle.session_id,
                                prompt,
                                float(budget) if isinstance(budget, int | float) else None,
                            )
                            return
                    except Exception:  # noqa: BLE001 -- terminalize below if retry cannot launch.
                        LOG.exception("auth failure respawn failed for %s", handle.session_id)
                # MINOR 1: Skip this notifier for longhaul-marked sessions (engine emits own blocked notification)
                if not _scheduler_owns_respawn(session.get("title")):
                    try:
                        self._notifier(
                            "ALL Claude logins broken — sessions cannot run",
                            f"Session {handle.session_id} could not recover from: {error_text}",
                        )
                    except Exception:  # noqa: BLE001 -- notification must never block supervision.
                        LOG.exception("all-logins-broken notification failed")
        approvals = self.dal.list_session_approvals(handle.session_id)
        approval = self._approval_for_finished(approvals, handle.denied_action_hashes)
        approval_state = str(approval.get("state") or "") if approval is not None else ""
        if approvals and handle.denied_action_hashes and approval is None:
            # The process was blocked on an action that NO approval row covers
            # (an approval already consumed, or a hook that never minted one).
            # There is nothing outstanding to park on, so this stays a failure --
            # and it is now a statement about the denied action, not about
            # whichever row happened to sort last.
            self._finish(handle.session_id, SessionState.FAILED)
            self._notify_longhaul_terminal(handle, return_code)
            return
        if approval_state == ApprovalState.PENDING.value:
            self._mark_awaiting(handle.session_id)
        elif approval_state == ApprovalState.APPROVED.value and not handle.resumed:
            self._mark_awaiting(handle.session_id)
        elif approval_state == ApprovalState.REJECTED.value:
            self._finish(handle.session_id, SessionState.CANCELLED)
        elif approval_state == ApprovalState.EXPIRED.value:
            # F1 / AUTO-APPROVE 5.2: re-queue a fresh pending approval; do not shred the session.
            # approval_state is non-empty only when a row exists; the None guard
            # is unreachable and exists to narrow the Optional for the call.
            if approval is not None and self._requeue_expired_session_approval(
                handle.session_id, approval
            ):
                self._mark_awaiting(handle.session_id)
            else:
                self._finish(handle.session_id, SessionState.FAILED)
        elif handle.permission_denied and handle.resumed:
            # No new pending approval exists. Never loop on a consumed approval.
            self._finish(handle.session_id, SessionState.FAILED)
        elif handle.permission_denied:
            # An unreachable hook API deliberately creates no approval row. Preserve
            # the fail-closed parked state for operator inspection/kill.
            self._mark_awaiting(handle.session_id)
        elif return_code == 0 and not handle.result_error:
            # F3: Clear any persisted error on successful completion
            try:
                self.dal.clear_session_error(handle.session_id)
            except Exception:  # noqa: BLE001 -- error clearing is best-effort
                LOG.exception("could not clear session error for %s", handle.session_id)
            self._finish(handle.session_id, SessionState.COMPLETED)
        else:
            self._finish(handle.session_id, SessionState.FAILED)
        self._notify_longhaul_terminal(handle, return_code)

    def _longhaul_engine(self) -> Any:
        engine = getattr(self, "_longhaul_engine_instance", None)
        if engine is not None:
            return engine
        from omniagentos.longhaul.config import load_config
        from omniagentos.longhaul.engine import LonghaulEngine
        from omniagentos.longhaul.store import LonghaulStore

        db_path = _dal_db_path(self.dal)
        cfg = load_config()
        cfg["_supervisor"] = self
        engine = LonghaulEngine(LonghaulStore(db_path), cfg, db_path)
        self._longhaul_engine_instance = engine
        return engine

    def _notify_longhaul_terminal(self, handle: _ManagedProcess, return_code: int) -> None:
        try:
            session = self.dal.get_session(handle.session_id)
        except Exception:  # noqa: BLE001 - test/process shutdown may close the DAL first.
            return
        if (
            session is None
            or "[longhaul:" not in str(session.get("title") or "")
            or SessionState(str(session["state"])) not in TERMINAL_SESSION_STATES
        ):
            return
        try:
            asyncio.run(
                self._longhaul_engine().on_session_terminal(
                    dict(session), list(handle.stream_events or []), return_code
                )
            )
        except Exception:  # noqa: BLE001 -- durable tick reconciliation retries it.
            LOG.exception("longhaul terminal hook failed for %s", handle.session_id)

    def _mark_awaiting(self, session_id: str) -> None:
        session = self.dal.get_session(session_id)
        if session is None:
            return
        state = SessionState(str(session["state"]))
        if state == SessionState.AWAITING_APPROVAL:
            return
        if self.dal.update_session_state(
            session_id,
            SessionState.AWAITING_APPROVAL.value,
            expect=(
                SessionState.STARTING.value,
                SessionState.RUNNING.value,
                SessionState.RESUMING.value,
            ),
        ):
            self._notify_awaiting(session_id)

    def _notify_operator(
        self, title: str, body: str, *, kind: str, severity: str
    ) -> bool:
        """Send a best-effort operator alert and report transport acceptance.

        Legacy/injected notifiers keep their two-argument contract.  The
        built-in transport receives labels so both remote legs apply the shared
        allowlist; its boolean result is the durable approval-delivery carrier,
        never a control-flow prerequisite.
        """
        try:
            if self._notifier is notify.push:
                # A local banner is deliberately unconditional, but it is not
                # proof that a human away from this Mac received the approval.
                return notify.push_outcome(
                    title, body, kind=kind, severity=severity
                ).remote_accepted
            return bool(self._notifier(title, body))
        except Exception:  # noqa: BLE001 - notification must never affect supervision
            LOG.exception("session operator notification failed")
            return False

    def _pending_session_approvals(
        self, session_id: str, approvals: Sequence[Mapping[str, Any]] | None = None
    ) -> list[Mapping[str, Any]]:
        """EVERY approval this session still owes a human decision on.

        NOT ``get_latest_session_approval``: an expiry requeue leaves the
        expired row and its pending replacement with the same one-second
        ``created_at``, so that helper's ``(created_at DESC, id DESC)`` ordering
        picks between the pair by random id. And a session can be waiting on
        several UNRELATED approvals at once -- ``create_session_approval``
        dedupes only rows that share an ``action_hash``, so two different tool
        calls are two pending rows by design. "Is a decision still outstanding"
        is a question about state, and how many are outstanding is a question
        no single row can answer.

        Returned in the DAL's stable ``(created_at, id)`` order.

        ``approvals`` lets a caller that already listed the rows reuse them
        rather than re-query, so the predicate stays in exactly one place.
        """
        rows = self.dal.list_session_approvals(session_id) if approvals is None else approvals
        return [row for row in rows if str(row.get("state") or "") == ApprovalState.PENDING.value]

    def _pending_session_approval(
        self, session_id: str, approvals: Sequence[Mapping[str, Any]] | None = None
    ) -> Mapping[str, Any] | None:
        """A representative outstanding approval, or None when none is.

        For the EXISTENCE question only ("does a human still owe this session a
        decision"). It is deliberately not called "the" approval: when several
        are outstanding, picking one of them by row order is precisely how a
        delivered approval came to authorise the reaper to void an undelivered
        sibling. Any decision that turns on delivery, on a deadline, or on which
        action a row is about must fold over ``_pending_session_approvals`` or
        be handed the id it means.
        """
        pending = self._pending_session_approvals(session_id, approvals)
        return pending[-1] if pending else None

    def _approval_for_finished(
        self,
        approvals: Sequence[Mapping[str, Any]],
        denied_action_hashes: set[str] | None,
    ) -> Mapping[str, Any] | None:
        """The approval row a just-finished process is ABOUT, or None.

        A permission-denied exit names the ACTIONS it was blocked on, so that is
        what selects the row -- matching by ``action_hash``, then preferring the
        pending half of a requeued pair. Selecting by row recency instead made
        an unrelated pending approval (a second, still-outstanding tool call)
        answer for the denied one: its params carry a different hash, so the
        caller read "this approval is not the one that was denied" and FAILED a
        session that should have been parked on the approval the process was
        actually waiting for.

        With no denied hashes the process tells us nothing about which approval
        it was blocked on, so the historical choice -- the newest row, which is
        what ``get_latest_session_approval`` returns -- stands unchanged. That
        remains ambiguous for a session with several outstanding approvals, but
        nothing in this shape can disambiguate it, and inventing a preference
        here would change terminal routing (a rejection cancels; a pending row
        parks) on evidence we do not have.
        """
        if not approvals:
            return None
        if denied_action_hashes:
            matches = [
                row for row in approvals if _approval_action_hash(row) in denied_action_hashes
            ]
            if not matches:
                return None
            pending = [
                row for row in matches if str(row.get("state") or "") == ApprovalState.PENDING.value
            ]
            return (pending or matches)[-1]
        return approvals[-1]

    def _notify_awaiting(self, session_id: str, approval_id: str | None = None) -> None:
        delivered = self._notify_operator(
            "OmniAgentOS approval required",
            f"Session {session_id} is waiting for approval.",
            kind="approval",
            severity="warning",
        )
        try:
            # WHICH row(s) this alert is about, stated rather than inferred:
            #
            #  * a caller that just minted a row (the expiry requeue) passes its
            #    id -- it knows, and re-deriving it from row order would let a
            #    pending sibling absorb the stamp for a delivery it never got;
            #  * otherwise the alert names the SESSION, not an action, so it
            #    covers every decision outstanding at this moment. Stamping only
            #    one of them recorded announced approvals as never-delivered;
            #    stamping rows that do not exist yet is impossible by
            #    construction (the snapshot is taken here).
            #
            # record_session_approval_delivery never downgrades a previous
            # success, so a failed alert can only ever leave a row more
            # conservative than it found it.
            if approval_id is not None:
                self.dal.record_session_approval_delivery(approval_id, delivered=delivered)
            else:
                for approval in self._pending_session_approvals(session_id):
                    self.dal.record_session_approval_delivery(
                        str(approval["id"]), delivered=delivered
                    )
        except Exception:  # noqa: BLE001 - durable marker must not block the park
            LOG.exception("session approval delivery marker failed for %s", session_id)
        # NT-notify: persist a durable, inspectable escalation notification
        # linked to the session (the OS push already fired above, so push=False
        # avoids a duplicate). Best-effort; supervision never depends on it.
        try:
            from omniagentos.notifications.service import notify_session_awaiting

            notify_session_awaiting(
                session_id=session_id,
                connection=getattr(self.dal, "_connection", None),
                push=False,
            )
        except Exception:  # noqa: BLE001 - persistence must never affect supervision
            LOG.exception("session approval notification persist failed")

    def run_once(self) -> None:
        """Own spawn ingress + service kill flags, adopted PIDs, and parked approvals."""

        # H-39 case 2: every branch below is DAL work. Polling a closed DAL is a
        # use-after-close, and swallowing it would hide the lifecycle violation.
        if self._dal_closed:
            raise RuntimeError("session supervisor refuses run_once: DAL is closed")
        # T-OPS-003: the running supervisor owns spawn. Drain the durable ingress
        # first so enqueued sessions are launched by the same loop that polls them.
        try:
            self._process_spawn_queue()
        except Exception:  # noqa: BLE001 - a bad ingress row must not starve polling
            LOG.exception("spawn ingress poll failed")

        sessions = self.dal.list_sessions(limit=10_000)
        for session in reversed(sessions):
            session_id = str(session["id"])
            # T-OPS-005: isolate each session so one FS/DB fault cannot starve
            # approvals/kills for every other session in the batch.
            try:
                self._service_session(session_id, session)
            except Exception:  # noqa: BLE001 - per-session isolation
                LOG.exception("session %s poll failed (isolated)", session_id)
        # A5: latency fix for orchestration/swarm-run stale-heartbeat detection.
        # Headless coverage already exists via the routines tick (every 300s) ->
        # board_sweep -> mark_stale_failed; riding this sub-second poll loop (behind
        # the same shared 30s claim as board reads and the routines tick) cuts that
        # to <3 minutes without tripling the sweep rate.
        try:
            self._mark_stale_runs()
        except Exception:  # noqa: BLE001 - stale sweep must never starve session polling.
            LOG.exception("stale run sweep failed (isolated)")
        try:
            asyncio.run(self._longhaul_engine().tick())
        except Exception:  # noqa: BLE001 - longhaul cannot starve base supervision.
            LOG.exception("longhaul tick failed (isolated)")

    def _mark_stale_runs(self) -> None:
        """Fail orchestrations/swarm runs whose coordinator heartbeat went stale.

        Shares ONE claim -- ``_claim_reconcile_stale_check`` (keyed by db path,
        30s window) -- with ``reconcile_board`` (board reads) and
        ``board_sweep.sweep_board`` (routines tick), imported here rather than
        copy-pasted, so at most one of the three call sites actually runs the
        sweep in any given window; the others no-op. ``SwarmDal.mark_stale_failed``
        had no periodic caller anywhere before this; it rides the same claim.
        Each dal's mark is independently best-effort: one failing must never
        block the other or escape to the caller. Swarm runs failed by this
        sweep additionally get the swarm_failed terminal bell (deduped
        kind-aware against the coordinator's own emit) — a sweep-killed run
        must not fail silently.
        """
        from omniagentos.intake.orchestrations import OrchestrationsDal
        from omniagentos.intake.service import (
            _claim_reconcile_stale_check,
            _orchestration_stale_minutes,
        )
        from omniagentos.swarm.dal import SwarmDal

        db_path = _dal_db_path(self.dal)
        if not _claim_reconcile_stale_check(db_path):
            return
        stale_minutes = _orchestration_stale_minutes()

        try:
            orchestrations_dal = OrchestrationsDal(db_path)
            try:
                orchestrations_dal.mark_stale_failed(stale_minutes=stale_minutes)
            finally:
                orchestrations_dal.close()
        except Exception:  # noqa: BLE001 - isolated from the swarm mark + the poll loop.
            LOG.exception("stale orchestration sweep failed (isolated)")

        swept_runs: list[dict[str, Any]] = []
        try:
            swarm_dal = SwarmDal(db_path)
            try:
                swept_runs = swarm_dal.mark_stale_failed(stale_minutes=stale_minutes)
            finally:
                swarm_dal.close()
        except Exception:  # noqa: BLE001 - isolated from the orchestrations mark + the poll loop.
            LOG.exception("stale swarm-run sweep failed (isolated)")

        # A run failed HERE (dead coordinator, stale heartbeat) never reaches
        # the coordinator's own terminal-notification path — without this emit
        # it is a silent failure. Ring the same swarm_failed bell per swept
        # run; notify_run_terminal_failure's kind-aware, read-agnostic dedupe
        # (exactly one swarm_failed row per card/run) makes coordinator-finally
        # + sweep double-emission impossible. Per-run best-effort: one bad row
        # must never silence the rest, and nothing here may escape the sweep.
        for swept in swept_runs:
            try:
                from omniagentos.notifications.service import notify_run_terminal_failure

                notify_run_terminal_failure(
                    run_id=str(swept.get("id") or ""),
                    status=str(swept.get("status") or "failed"),
                    goal=str(swept.get("goal") or "")[:120],
                    board_task_id=(
                        str(swept["board_task_id"]) if swept.get("board_task_id") else None
                    ),
                    workspace=str(swept.get("working_dir") or "") or None,
                    db_path=db_path,
                )
            except Exception:  # noqa: BLE001 - the failure bell is best-effort by contract.
                LOG.exception(
                    "failure bell for stale-swept swarm run %s failed (isolated)",
                    swept.get("id"),
                )
        # NO resume sweep here ON PURPOSE (WP10). ``resume_stale_swarms`` runs
        # exactly once per supervisor lifetime, from ``resume_swarms_once`` at
        # ``serve_forever`` startup — never per ``run_once`` pass. Its own
        # docstring pins the same contract: takeover from a periodic sweep
        # could start coordinators behind every poller's back.

    def _service_session(self, session_id: str, session: Mapping[str, Any]) -> None:
        # WP3 provider-exec rows have their own reader/reconciler.  Adopting them
        # here would reintroduce bare-pid reconciliation and two lifecycle owners.
        if str(session.get("provider") or "claude") != "claude":
            return
        state = SessionState(str(session["state"]))
        if state in TERMINAL_SESSION_STATES:
            # Terminal RELEASE. _finish already released for every transition this
            # process made; this catches the ones it did not make — a row
            # terminalized by the API, by reconcile_park_or_kill, or by another
            # supervisor. Guarded by the gate's own held-set, so the overwhelming
            # majority of terminal rows (which this process never claimed for) cost
            # nothing on a poll loop that visits every row every pass.
            self._scope.release(session_id)
            self._ensure_manifest(session_id)
            return
        if bool(session.get("kill_requested")):
            self._kill_session(session_id)
            return
        # RENEW, every poll. Rate-limited inside the gate to once per third of the
        # lease, and skipped outright for a session this process holds no locks
        # for, so the 2 Hz loop does not become a write storm. Takes the ROW, not
        # the id, because a lease that lapsed under a stalled supervisor is
        # recovered by re-declaring — which needs the declaration.
        self._scope.renew(session)
        with self._lock:
            adopted_pid = self._adopted.get(session_id)
        if adopted_pid is not None and not self._liveness(adopted_pid):
            with self._lock:
                self._adopted.pop(session_id, None)
            # Same predicate: a session whose adopted PID died with a decision
            # still outstanding is PARKED, never failed. Asking for the newest
            # ROW instead shredded it whenever an expired sibling won the id
            # tiebreak.
            if self._pending_session_approval(session_id) is not None:
                self._mark_awaiting(session_id)
            else:
                self._finish(session_id, SessionState.FAILED)
            return
        # A2 idle/hang reaper. Isolated so a reaper fault can never starve the
        # approval servicing (or any later step) for this session.
        try:
            self._reap_if_needed(session_id, session, state)
        except Exception:  # noqa: BLE001 - reaper isolation
            LOG.exception("idle reaper failed for session %s (isolated)", session_id)
        if state == SessionState.AWAITING_APPROVAL:
            # The ceiling runs FIRST: a park that outlived its bound must not be
            # handed to the resume path. It self-limits to undecided approvals,
            # so a decided one still falls through to _service_approval below.
            if self._reap_parked_if_needed(session_id, session):
                return
            self._service_approval(session_id)

    # --- max-park ceiling ------------------------------------------------------

    def _max_park_seconds(self) -> float:
        """Ceiling on awaiting_approval, in seconds. <= 0 disables the ceiling."""
        raw = os.environ.get(MAX_PARK_MINUTES_ENV, "").strip()
        if not raw:
            return DEFAULT_MAX_PARK_MINUTES * 60.0
        try:
            minutes = float(raw)
        except ValueError:
            return DEFAULT_MAX_PARK_MINUTES * 60.0
        if minutes <= 0:
            return 0.0  # explicitly disabled
        return minutes * 60.0

    def _park_clock(
        self, session_id: str, session: Mapping[str, Any], now: float
    ) -> tuple[float, float] | None:
        """``(parked_seconds, park_started_at)``, or None when unknowable.

        Prefers the durable ``session.awaiting_approval`` lifecycle event (the
        moment the park started); falls back to the row's ``updated_at`` for a
        row whose event predates this method or was written by another writer.

        The anchor is returned, not re-derived from ``now - parked`` by the
        caller: it identifies the park EPISODE, and ``awaiting_since`` re-bases
        it to the LAST park transition, so a session that parks, resumes and
        parks again is a different episode with the same session id.
        """
        parked_at: float | None = None
        reader = getattr(self.dal, "awaiting_since", None)
        if callable(reader):
            try:
                parked_at = _iso_to_epoch(reader(session_id))
            except Exception:  # noqa: BLE001 - fall back to the row stamp below
                LOG.debug("awaiting_since lookup failed for %s", session_id, exc_info=True)
        if parked_at is None:
            parked_at = _iso_to_epoch(session.get("updated_at"))
        if parked_at is None:
            return None
        return max(0.0, now - parked_at), parked_at

    def _park_escalations(self, session_id: str, anchor: float) -> _ParkEscalations:
        """This process's escalation record for the CURRENT park episode.

        A record left over from an earlier episode of the same session is
        replaced, never reused: it was written against a park start that has
        since been superseded.
        """
        key = int(anchor)
        with self._lock:
            record = self._max_park_extension_notified.get(session_id)
            if record is None or record.anchor != key:
                record = _ParkEscalations(anchor=key)
                self._max_park_extension_notified[session_id] = record
            return record

    def _max_park_escalated_at(
        self, session_id: str, anchor: float, record: _ParkEscalations
    ) -> float | None:
        """When THIS park episode was last escalated, or None if it never was.

        The durable ``reaper.max_park_extended`` receipt is the authority
        whenever one can be read -- including when it reports a time from an
        EARLIER episode, which is positive evidence that this episode has not
        been escalated yet. That is what lets a repeat offender escalate again:
        a broken delivery channel is exactly the condition where the second and
        third park episodes must still reach an operator.

        The in-process stamps are consulted only where the durable log cannot
        answer, so that a locked or unwritable DB still cannot turn one
        escalation into a poll-rate storm (LS-R1-002).
        """
        try:
            reader = getattr(self.dal, "max_park_extended_since", None)
            durable = _iso_to_epoch(reader(session_id)) if callable(reader) else None
        except Exception:  # noqa: BLE001 - an older/test DAL must not pin a park open
            LOG.exception("could not read max-park extension for %s", session_id)
            durable = None
        if durable is not None:
            if durable >= anchor:
                return durable
            # An older receipt says the previous episode's escalation DID
            # persist, so this process's copy of it is redundant. Only an
            # escalation that left no receipt still counts here.
            return record.unpersisted_at or None
        # No durable evidence at all: the events insert failed, or the reader is
        # missing/threw. Fall back to what this process knows it sent.
        return record.escalated_at or None

    def _reap_parked_if_needed(self, session_id: str, session: Mapping[str, Any]) -> bool:
        """Terminalize a session parked past the max-park ceiling. True when it fired.

        Enforced regardless of the A2 reaper's dry-run flag, on the same grounds
        as the STARTING stall.  A pending approval alone is NOT proof that an
        alert reached a human: undelivered parks get a durable re-arm and an
        escalation before terminalization, while delivered-but-undecided parks
        retain the ordinary ceiling. NEVER auto-approves in either branch.
        """
        if session.get("source") != "bridge":
            return False  # T-OPS-004: report-only external rows are not ours to kill
        ceiling = self._max_park_seconds()
        if ceiling <= 0:
            return False
        # Cheapest discriminator first: this runs for every parked session on
        # every poll, and almost all of them are inside the window.
        now = time.time()
        clock = self._park_clock(session_id, session, now)
        if clock is None:
            return False
        parked, parked_started_at = clock
        if parked <= ceiling:
            return False
        # Ask for the OUTSTANDING approvals, not the newest row: half the time
        # the newest row is the EXPIRED half of a requeued pair, and reading it
        # took the "decided, not our failure mode" exit below -- so the park
        # escaped BOTH ceilings, forever, on a coin toss.
        approvals = self.dal.list_session_approvals(session_id)
        pending = self._pending_session_approvals(session_id, approvals)
        if approvals and not pending:
            # DECIDED (approved/rejected/expired): the decision arrived, so this
            # is not the failure mode. _service_approval owns it.
            return False
        outer_ceiling = ceiling * _MAX_PARK_OUTER_CEILING_MULTIPLIER
        # A park can owe a human SEVERAL decisions at once, and terminalizing it
        # voids EVERY one of them -- so the deadline is a fold over all of them,
        # the window that expires LAST, never one row picked by order. A
        # sibling's successful delivery is not evidence that THIS approval ever
        # reached anybody, and reading one delivered row as "the" approval let a
        # delivered approval spend an undelivered one's window at the normal
        # ceiling, without ever escalating it.
        undelivered = [row for row in pending if not _approval_delivered(row)]
        # NOT the same as "nothing undelivered": a park with no approval row at
        # all (the unreachable-hook shape, and every pre-migration-121 row) has
        # nothing undelivered either, and it is the case that most needs the
        # escalation. Absence of a carrier is never a favourable value.
        all_delivered = bool(pending) and not undelivered
        pending_ids = [str(row["id"]) for row in pending]
        undelivered_ids = [str(row["id"]) for row in undelivered]
        escalations = self._park_escalations(session_id, parked_started_at)
        if parked <= outer_ceiling and all_delivered:
            # An escalation delivered only after the original ceiling earns a
            # fresh human-decision window -- and it is recognised by THAT
            # escalation's own receipt, never by `delivered_at` alone.
            # `delivered_at` has three other writers (the park alert, the expiry
            # requeue, the hook route), so "stamped later than the park start"
            # is not evidence of an escalation: it is what an ordinary delivery
            # looks like, and treating it as one silently promotes a
            # delivered-but-undecided park from the normal ceiling to the 4x
            # outer one. A delivery at park time never weakens the deadline.
            escalated_at = self._max_park_escalated_at(
                session_id, parked_started_at, escalations
            )
            if escalated_at is not None and now - escalated_at <= ceiling:
                return False
        elif parked <= outer_ceiling:
            extension_at = self._max_park_escalated_at(session_id, parked_started_at, escalations)
            if extension_at is not None and now - extension_at <= ceiling:
                return False
            # Mark BEFORE notification and durable persistence.  Either may
            # fail under multi-writer load, but neither failure may re-alert on
            # every 0.5s poll in this ceiling window.  Assume unpersisted until
            # the durable write below actually returns.
            escalations.escalated_at = now
            escalations.unpersisted_at = now
            detail = (
                f"approval was never delivered; extending parked window "
                f"(parked {int(parked)}s, outer ceiling {int(outer_ceiling)}s, "
                f"approval: {_approval_label(undelivered_ids)})"
            )
            delivered = self._notify_operator(
                "OmniAgentOS approval delivery failed — session still parked",
                f"Session {session_id}: {detail}.",
                kind="approval",
                severity="warning",
            )
            # Every row this escalation was sent FOR gets the result, not just a
            # representative one: an unstamped undelivered row would re-read as
            # never-attempted and the ones that were stamped would not.
            for undelivered_id in undelivered_ids:
                try:
                    self.dal.record_session_approval_delivery(undelivered_id, delivered=delivered)
                except Exception:  # noqa: BLE001 - re-arm still must be durable below
                    LOG.exception("could not record approval delivery for %s", session_id)
            try:
                # The receipt's payload key is singular, so the full set lives in
                # `detail`; the id recorded here is the last outstanding row in
                # the DAL's stable order, a stable representative rather than a
                # claim that it was the only one.
                self.dal.record_max_park_extension(
                    session_id,
                    approval_id=undelivered_ids[-1] if undelivered_ids else None,
                    detail=detail,
                )
                # The receipt is durable now, so the in-process copy of this
                # escalation carries nothing the log does not — and holding it
                # would outlive the episode and silence the next one.
                escalations.unpersisted_at = 0.0
            except Exception:  # noqa: BLE001 - never fail close on an unpersisted re-arm
                LOG.exception("could not persist max-park extension for %s", session_id)
                # KNOWN, ACCEPTED, and deliberately not retried. A receipt that
                # never lands costs a duplicate escalation after a restart on
                # this path (no receipt and no in-process record reads as "not
                # escalated yet", which re-alerts rather than reaping), and on
                # the delivered path above it costs the extra window entirely:
                # the escalation's own receipt is what buys it, so a restart
                # that cannot read one terminalizes at the normal ceiling.
                # Both directions are conservative -- an availability cost, and
                # never an escape from either ceiling. A retry here would run
                # inside the 2 Hz poll against the DB lock that just refused the
                # write, which is how the LS-R1-002 alert storm was built.
                return False
            LOG.warning(
                "max-park extension %s",
                json.dumps(
                    {
                        "event": "reaper.max_park_extended",
                        "session_id": session_id,
                        "parked_seconds": round(parked, 1),
                        "ceiling_seconds": round(ceiling, 1),
                        "outer_ceiling_seconds": round(outer_ceiling, 1),
                        "approval": _approval_label(undelivered_ids),
                        "delivery_sent": delivered,
                    },
                    sort_keys=True,
                ),
            )
            return False
        try:
            extension_counter = getattr(self.dal, "max_park_extension_count", None)
            extensions = int(extension_counter(session_id)) if callable(extension_counter) else 0
        except Exception:  # noqa: BLE001 - forensic enrichment must not block terminalization
            LOG.exception("could not count max-park extensions for %s", session_id)
            extensions = 0
        normal_minutes = ceiling / 60.0
        outer_minutes = outer_ceiling / 60.0
        which = _approval_label(pending_ids, prefix="pending ")
        # "delivered but undecided" is a claim about EVERY outstanding approval.
        # One undelivered row among them makes the honest description of this
        # reap "never delivered", because that row never reached a human.
        delivery = "delivered but undecided" if all_delivered else "never delivered"
        if all_delivered:
            detail = (
                f"approval {delivery} within {normal_minutes:g}m normal ceiling — parked session reaped "
                f"(parked {int(parked)}s, approval: {which}, extensions: {extensions})"
            )
        else:
            detail = (
                f"approval {delivery} within {normal_minutes:g}m normal threshold or "
                f"{outer_minutes:g}m outer ceiling — parked session reaped "
                f"(parked {int(parked)}s, outer_ceiling_seconds: {int(outer_ceiling)}, "
                f"extensions: {extensions}, approval: {which})"
            )
        # EVERY outstanding approval is voided by this reap, so every one of them
        # is closed out. Expiring only a representative left the others pending
        # on a terminal session -- a decision the operator can still be shown as
        # outstanding, that nothing will ever service.
        for pending_id in pending_ids:
            try:
                self.dal.expire_approval(pending_id, detail)
            except Exception:  # noqa: BLE001 - expiry is hygiene, never blocks the reap
                LOG.exception("could not expire approval for parked session %s", session_id)
        try:
            _persist_session_error(self.dal, session_id, detail)
        except Exception:  # noqa: BLE001 - error text is best-effort
            LOG.exception("could not persist max-park error for %s", session_id)
        self._finish(session_id, SessionState.FAILED, killed_by="max-park")
        LOG.warning(
            "max-park reap %s",
            json.dumps(
                {
                    "event": "reaper.max_park",
                    "session_id": session_id,
                    "parked_seconds": round(parked, 1),
                    "ceiling_seconds": round(ceiling, 1),
                    "outer_ceiling_seconds": round(outer_ceiling, 1),
                    "extensions": extensions,
                    "approval": which,
                    "detail": detail,
                },
                sort_keys=True,
            ),
        )
        self._notify_operator(
            "OmniAgentOS session reaped (approval never decided)",
            f"Session {session_id}: {detail}.",
            kind="approval",
            severity="warning",
        )
        return True

    # --- A2 idle/hang reaper ---------------------------------------------------

    def _idle_threshold_seconds(self, session: Mapping[str, Any]) -> float:
        """Allowed idle seconds for this session before the reaper acts.

        WP3 persists the swarm scheduler's per-session override so tiered
        timeouts are not preempted by the global env value.
        """
        override = session.get("idle_minutes")
        if isinstance(override, int | float) and float(override) > 0:
            return float(override) * 60.0
        raw = os.environ.get(IDLE_MINUTES_ENV, "").strip()
        try:
            minutes = float(raw) if raw else DEFAULT_IDLE_MINUTES
        except ValueError:
            minutes = DEFAULT_IDLE_MINUTES
        if minutes <= 0:
            minutes = DEFAULT_IDLE_MINUTES
        return minutes * 60.0

    def _reaper_enforced(self) -> bool:
        """False (DRY-RUN, log-only) unless OMNIAGENTOS_REAPER_ENFORCE is truthy."""
        raw = os.environ.get(REAPER_ENFORCE_ENV, "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _transcript_mtime(self, session: Mapping[str, Any]) -> float | None:
        """Best-effort mtime of the claude CLI's own on-disk session transcript.

        The supervisor's reader thread persists activity to the DB only — it
        writes NO per-session stream artifact — so the one on-disk artifact whose
        freshness survives a supervisor restart is the CLI's transcript at
        ``<config_dir>/projects/<munged project_dir>/<session_ref>.jsonl``. That
        matters for ADOPTED sessions: after a restart there is no reader thread,
        so ``last_activity_at`` goes stale on a perfectly healthy session and the
        transcript mtime is what keeps the reaper honest. Fail-safe by contract:
        any resolution or stat failure returns None and idle falls back to the
        DB activity marks alone. Path resolution is shared with the chat bridge
        and transcript routes (:mod:`omniagentos.sessions.transcript`) — the
        candidates here add the live launch facts (handle + env) on top of the
        account-row / ``~/.claude`` chain."""
        from omniagentos.sessions.transcript import config_dir_candidates, project_dir_slug

        session_ref = session.get("session_ref")
        project_dir = session.get("project_dir")
        if not isinstance(session_ref, str) or not session_ref:
            return None
        if not isinstance(project_dir, str) or not project_dir:
            return None
        with self._lock:
            handle = self._processes.get(str(session.get("id") or ""))
        extra: list[str | None] = [
            handle.account_config_dir if handle is not None else None,
            os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or None,
        ]
        config_dirs = config_dir_candidates(
            session,
            account_lookup=getattr(self.dal, "get_claude_account", None),
            extra_config_dirs=tuple(extra),
        )
        newest: float | None = None
        for config_dir in config_dirs:
            candidate = (
                Path(config_dir)
                / "projects"
                / project_dir_slug(project_dir)
                / f"{session_ref}.jsonl"
            )
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
        return newest

    def _session_idle_measurement(
        self, session: Mapping[str, Any], now: float
    ) -> tuple[float | None, float | None]:
        """Return ``(idle_seconds, transcript_mtime)`` from the conservative signals.

        The max over ``last_activity_at`` AND the on-disk transcript mtime is
        load-bearing (plan A2): either source alone under-reports liveness —
        the DB mark when no reader thread exists (adopted session), the file
        when the CLI buffers. ``updated_at``/``created_at`` participate as
        conservative floors so a row that predates activity tracking can never
        be reaped off a missing column."""
        transcript_mtime = self._transcript_mtime(session)
        marks = [
            _iso_to_epoch(session.get("last_activity_at")),
            _iso_to_epoch(session.get("updated_at")),
            _iso_to_epoch(session.get("created_at")),
            transcript_mtime,
        ]
        known = [mark for mark in marks if mark is not None]
        if not known:
            return None, transcript_mtime
        return max(0.0, now - max(known)), transcript_mtime

    def _session_idle_seconds(self, session: Mapping[str, Any], now: float) -> float | None:
        """now − max(DB activity marks, transcript mtime); None if nothing is known."""
        return self._session_idle_measurement(session, now)[0]

    def _adopted_transcript_is_unknown(self, session_id: str, transcript_mtime: float | None) -> bool:
        """Whether a restart-owned live process has no trustworthy disk signal.

        An adopted session has no reader thread in this supervisor, so a stale
        DB mark plus an unresolvable transcript path is an *unknown*, not proof
        of idleness. The bounded defer in ``_reap_if_needed`` lets a slow pure
        LLM turn survive that gap without exempting genuinely hung processes.
        """
        if transcript_mtime is not None:
            return False
        with self._lock:
            return session_id in self._adopted

    def _reap_if_needed(
        self, session_id: str, session: Mapping[str, Any], state: SessionState
    ) -> None:
        """A2: reap idle/hung or over-budget RUNNING/RESUMING bridge sessions.

        Guards (in order): bridge-only; STARTING >5 min with no pid and no
        queued spawn is failed; AWAITING_APPROVAL is excluded by the state
        filter; a live tool subprocess in the session's process group defers
        the kill one service interval up to a 3× threshold hard ceiling; the
        kill itself is CAS-guarded by ``request_kill``'s non-terminal WHERE.

        Longhaul-marked sessions are reaped like any other bridge session,
        under the per-lane ``sessions.idle_minutes`` override their engine
        persists at spawn. Respawn stays the engine's alone: it discriminates
        on the terminal row's ``killed_by`` (idle-reaper/budget → successor;
        operator/cancel_requested → cancelled)."""
        if session.get("source") != "bridge":
            return
        now = time.time()
        raw_pid = session.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else 0
        if state == SessionState.STARTING:
            # Deterministic stall, enforced regardless of dry-run: no pid was ever
            # recorded and no spawn request is queued, so nothing can ever run.
            if pid == 0 and not self.dal.has_queued_spawn(session_id):
                age = self._session_idle_seconds(session, now)
                if age is not None and age > _STARTING_STALL_SECONDS:
                    try:
                        _persist_session_error(
                            self.dal,
                            session_id,
                            f"stalled in starting for {int(age)}s with no process",
                        )
                    except Exception:  # noqa: BLE001 - error text is best-effort
                        LOG.exception("could not persist stall error for %s", session_id)
                    self._finish(session_id, SessionState.FAILED)
            return
        if state not in (SessionState.RUNNING, SessionState.RESUMING):
            return
        if pid == 0 or not self._liveness(pid):
            return  # dead/absent pid belongs to reconcile / the adopted-pid path
        threshold = self._idle_threshold_seconds(session)
        idle, transcript_mtime = self._session_idle_measurement(session, now)
        budget = session.get("budget_usd_max")
        cost = session.get("cost_usd")
        if (
            isinstance(budget, int | float)
            and budget > 0
            and isinstance(cost, int | float)
            and float(cost) >= float(budget)
        ):
            detail = f"cost_usd {float(cost):.2f} >= budget_usd_max {float(budget):.2f}"
            if not budget_blocks():
                # Advisory: a session over budget is WORTH KNOWING, not worth
                # killing mid-task. Logged once per session (the reaper's own
                # dedupe key) so the tick does not restate it every cycle.
                marker = f"{session_id}:budget"
                if marker not in self._reaper_would_kill_logged:
                    self._reaper_would_kill_logged.add(marker)
                    LOG.info(
                        "session over budget (advisory) %s",
                        json.dumps(
                            {
                                "event": "session.budget_overshoot",
                                "session_id": session_id,
                                "detail": detail,
                            },
                            sort_keys=True,
                        ),
                    )
                # Fall through to the ordinary idle checks below.
            else:
                self._reap(
                    session_id,
                    reason="budget",
                    killed_by="budget",
                    idle_seconds=idle or 0.0,
                    threshold_seconds=threshold,
                    detail=detail,
                )
                return
        if idle is None or idle <= threshold:
            # Activity resumed: re-arm the dry-run would-kill log for this session.
            self._reaper_would_kill_logged.discard(f"{session_id}:idle")
            return
        if idle < threshold * _IDLE_HARD_CEILING_MULTIPLIER:
            children = self._pgid_child_counter(pid)
            adopted_transcript_unknown = self._adopted_transcript_is_unknown(
                session_id, transcript_mtime
            )
            if children > 0 or adopted_transcript_unknown:
                # A running tool subprocess (e.g. a 20-min build) is live work
                # the stream cannot show. So is an adopted process with no
                # reader and no resolvable transcript: that is an explicit
                # unknown, not evidence of idleness. Both defers are bounded by
                # the same 3x hard ceiling so a genuinely hung process still dies.
                LOG.info(
                    "reaper defer %s",
                    json.dumps(
                        {
                            "event": "reaper.defer",
                            "session_id": session_id,
                            "idle_seconds": round(idle, 1),
                            "threshold_seconds": round(threshold, 1),
                            "live_children": children,
                            "adopted_transcript_unknown": adopted_transcript_unknown,
                        },
                        sort_keys=True,
                    ),
                )
                return
        self._reap(
            session_id,
            reason="idle",
            killed_by="idle-reaper",
            idle_seconds=idle,
            threshold_seconds=threshold,
            detail=f"no activity for {int(idle)}s (threshold {int(threshold)}s)",
        )

    def _reap(
        self,
        session_id: str,
        *,
        reason: str,
        killed_by: str,
        idle_seconds: float,
        threshold_seconds: float,
        detail: str,
    ) -> None:
        enforce = self._reaper_enforced()
        payload = {
            "event": "reaper.kill" if enforce else "reaper.would_kill",
            "session_id": session_id,
            "reason": reason,
            "idle_seconds": round(idle_seconds, 1),
            "threshold_seconds": round(threshold_seconds, 1),
            "enforce": enforce,
            "detail": detail,
        }
        if not enforce:
            # Exceptional dry-run/debug mode: structured would-kill line, NO kill.
            key = f"{session_id}:{reason}"
            if key not in self._reaper_would_kill_logged:
                self._reaper_would_kill_logged.add(key)
                LOG.warning("reaper would-kill %s", json.dumps(payload, sort_keys=True))
            return
        # CAS-guarded: request_kill no-ops (False) if the session reached a
        # terminal state between the idle check and this call.
        if not self.dal.request_kill(session_id, killed_by=killed_by):
            return
        LOG.warning("reaper kill %s", json.dumps(payload, sort_keys=True))
        self._notify_operator(
            f"OmniAgentOS session reaped ({reason})",
            f"Session {session_id} was killed by {killed_by}: {detail}.",
            kind="escalation",
            severity="warning",
        )

    def _requeue_expired_session_approval(
        self, session_id: str, expired: Mapping[str, Any]
    ) -> bool:
        """Mint a fresh pending approval after expiry so the session is not shredded (F1).

        Returns True when a new pending row was created (or an equivalent pending
        already exists for the same action_hash).
        """
        try:
            try:
                params = json.loads(str(expired.get("params_json") or "{}"))
            except (TypeError, json.JSONDecodeError):
                params = {}
            if not isinstance(params, dict):
                params = {"value": params}
            action_hash_value = str(params.get("action_hash") or "")
            if not action_hash_value:
                action_hash_value = f"requeue:{expired.get('id') or new_id('apr')}"
            hours = int(getattr(self._policy, "approval_expiry_hours", 72) or 72)
            from datetime import UTC, timedelta

            expires_at = (datetime.now(UTC) + timedelta(hours=max(1, hours))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            proposed = str(expired.get("proposed_action") or "requeued action")
            action_class = str(expired.get("action_class") or ActionClass.IRREVERSIBLE.value)
            # An EXPIRED row is never removed from list_session_approvals, so
            # _service_approval re-enters this method for it on EVERY 0.5s poll.
            # create_session_approval dedupes on action_hash and hands back the
            # pending row minted by the FIRST pass, so remember which pending
            # rows already existed: everything below this point is per-EVENT
            # work (an operator alert plus a delivered_at stamp) and must not
            # run again for a requeue that minted nothing.
            already_pending = {
                str(row["id"])
                for row in self.dal.list_session_approvals(session_id)
                if str(row.get("state") or "") == ApprovalState.PENDING.value
            }
            new_id_apr = self.dal.create_session_approval(
                session_id=session_id,
                action_hash=action_hash_value,
                action_class=action_class,
                proposed_action=proposed,
                params_json=json.dumps(params, sort_keys=True, separators=(",", ":")),
                risk=str(expired.get("risk") or "high"),
                evidence=f"requeued after approval_expired (prior={expired.get('id')})",
                expires_at=expires_at,
            )
            if str(new_id_apr) in already_pending:
                # No new row: an equivalent pending approval already covers this
                # expiry. Alerting here would be a poll-rate operator storm, and
                # _notify_awaiting's delivered_at re-stamp would hold the park
                # open to the 4x outer ceiling by making every poll look like a
                # fresh human delivery. The session still HAS a pending approval.
                return True
            # This is a fresh approval row, so it needs the exact same
            # remote-only delivery carrier as the initial park alert -- recorded
            # against THIS row by id. The caller knows which row it just minted;
            # letting _notify_awaiting re-derive it would spread this alert's
            # result over unrelated pending approvals it says nothing about.
            self._notify_awaiting(session_id, str(new_id_apr))
            try:
                from omniagentos.notifications.service import record_notification

                record_notification(
                    kind="approval",
                    title="Approval expired — requeued",
                    body=f"Session kept parked; new approval {new_id_apr}: {proposed[:200]}",
                    severity="warning",
                    ref_type="approval",
                    ref_id=str(new_id_apr),
                    payload={
                        "prior_approval_id": expired.get("id"),
                        "session_id": session_id,
                        "reason": "approval_expired_requeued",
                        "proposed_action": proposed,
                    },
                    # _notify_awaiting already emitted the operator alert and
                    # recorded its remote delivery result for this new row.
                    push=False,
                )
            except Exception:  # noqa: BLE001
                LOG.debug("session expiry requeue notify failed", exc_info=True)
            return True
        except Exception:  # noqa: BLE001
            LOG.exception("failed to requeue expired approval for session %s", session_id)
            return False

    def _service_approval(self, session_id: str) -> None:
        approvals = self.dal.list_session_approvals(session_id)
        if not approvals:
            return
        has_pending = False
        for approval in approvals:
            # CODE-003: sample the clock per approval, not once per batch, so
            # expiry decisions do not skew across a large session set.
            now = utc_now_iso()
            try:
                action_class = ActionClass(str(approval["action_class"]))
                decision = evaluate_action(action_class, self._policy)
            except (KeyError, TypeError, ValueError):
                self._finish(session_id, SessionState.FAILED)
                return
            gate = approval_satisfies_gate(
                approval,
                decision,
                actor="session-supervisor",
                now_iso=now,
            )
            state = str(approval.get("state") or "")
            if gate.expired:
                self.dal.expire_approval(str(approval["id"]), "approval expired")
                # F1 / AUTO-APPROVE 5.2: re-queue rather than fail the session.
                if self._requeue_expired_session_approval(session_id, approval):
                    has_pending = True
                    continue
                self._finish(session_id, SessionState.FAILED)
                return
            if state == ApprovalState.REJECTED.value:
                self._finish(session_id, SessionState.CANCELLED)
                return
            if state == ApprovalState.EXPIRED.value:
                if self._requeue_expired_session_approval(session_id, approval):
                    has_pending = True
                    continue
                self._finish(session_id, SessionState.FAILED)
                return
            if state == ApprovalState.PENDING.value:
                has_pending = True
                continue
            if state != ApprovalState.APPROVED.value:
                self._finish(session_id, SessionState.FAILED)
                return
            # The shared gate is the sole authority for whether a human approved this.
            if not gate.human_ok:
                self._finish(session_id, SessionState.FAILED)
                return
        if has_pending:
            return
        self._resume(session_id)

    def _resume(self, session_id: str) -> None:
        session = self.dal.get_session(session_id)
        if session is None:
            return
        session_ref = session.get("session_ref")
        if not isinstance(session_ref, str) or not session_ref:
            self._finish(session_id, SessionState.FAILED)
            return
        if not self.dal.update_session_state(
            session_id,
            SessionState.RESUMING.value,
            expect=SessionState.AWAITING_APPROVAL.value,
        ):
            return
        # T-DESIGN-005: NO ambient --permission-mode acceptEdits. The sole resume
        # authority is the hook's single-use action_hash grant (SEC-006); acceptEdits
        # would broaden execution beyond the one approved action. Resume with the SAME
        # gating --settings hooks so every re-issued tool is judged at the hook.
        argv = [
            self.claude_binary,
            "-p",
            "--resume",
            session_ref,
            _CONTINUATION,
            "--settings",
            bridge_settings_path(),
            "--output-format",
            "stream-json",
            # See _bridge_launch_argv: --verbose is mandatory with --print + stream-json.
            "--verbose",
            "--include-hook-events",
        ]
        try:
            self._launch(
                session_id,
                argv,
                cwd=str(session["project_dir"]),
                expected=SessionState.RESUMING,
                resumed=True,
            )
        except BaseException:
            LOG.exception("unable to resume session %s", session_id)
            self._finish(session_id, SessionState.FAILED)

    def _kill_session(self, session_id: str) -> None:
        session = self.dal.get_session(session_id)
        if session is None:
            return
        if session.get("source") != "bridge":
            self.dal.clear_kill(session_id)
            self._finish(session_id, SessionState.CANCELLED, killed_by="operator")
            return
        with self._lock:
            self._killing.add(session_id)
            handle = self._processes.get(session_id)
            adopted_pid = self._adopted.get(session_id)
        terminalized = False
        try:
            # F001 (round-3 repair): a False verdict from _terminate_adopted_pid
            # means ownership/the signal itself is unverifiable -- NOT that the
            # process is dead. Terminalizing the row on that False would report
            # a confirmed kill for a process we never proved we touched. Only a
            # managed handle (owned by construction, see _terminate_local_process)
            # or a confirmed adopted-PID signal may terminalize; anything else
            # leaves the row non-terminal so a later cancel read-back reports
            # kill_pending / kill_complete=false, fail closed.
            confirmed = True
            if handle is not None:
                # Managed handle: owned by construction, terminate directly.
                self._terminate_local_process(handle)
            elif adopted_pid is not None and self._liveness(adopted_pid):
                # Adopted PID from the durable row: prove it is ours first.
                confirmed = self._terminate_adopted_pid(adopted_pid, session)
            if not confirmed:
                LOG.warning(
                    "kill: adopted pid %s for session %s unverifiable -- "
                    "leaving row non-terminal (fail closed)",
                    adopted_pid,
                    session_id,
                )
                return
            requested_by = str(session.get("killed_by") or "")
            target = (
                SessionState.CANCELLED
                if requested_by == "cancel_requested"
                else SessionState.KILLED
            )
            # A2: preserve a reaper's identity (idle-reaper/budget) recorded by
            # request_kill(killed_by=...); the legacy paths (plain request_kill,
            # cancel) keep their historical "operator" attribution.
            attribution = (
                requested_by if requested_by and requested_by != "cancel_requested" else "operator"
            )
            self._finish(session_id, target, killed_by=attribution)
            terminalized = True
            if handle is not None:
                self._notify_longhaul_terminal(handle, 143)
        finally:
            with self._lock:
                self._killing.discard(session_id)
            # F007 (round-4): state cleanup happens ONLY when the row reached a
            # terminal state via _finish above. On the fail-closed path (or an
            # exception mid-kill) the row is deliberately non-terminal and the
            # adopted-PID/handle context MUST survive for the next attempt —
            # releasing here made a SECOND kill attempt lose the very context
            # verification needs. The scope release still belongs here and not
            # in the idle reaper (which only sets kill_requested while the
            # session is alive); _finish normally does it, and this repeats it
            # idempotently so a kill whose _finish was a no-op (already-terminal
            # row) still gives the paths back.
            if terminalized:
                self._scope.release(session_id)
                with self._lock:
                    self._processes.pop(session_id, None)
                    self._adopted.pop(session_id, None)

    def _terminate_local_process(self, handle: _ManagedProcess) -> None:
        self._terminate_child_process(handle.process)

    def _terminate_child_process(self, process: Any) -> None:
        """Terminate a child this supervisor holds a live handle for.

        H-39 case 5: a managed handle is owned **by construction** -- we created
        the process and hold its :class:`subprocess.Popen`, so the PID cannot
        have been recycled while the handle is open. No identity check runs
        here, deliberately: gating an owned child on an observation that can
        fail would leak a real Node/Claude session on the first ``ps`` hiccup.
        Adopted PIDs (no handle) go through :meth:`_terminate_adopted_pid`.
        """
        if process.poll() is not None:
            return
        try:
            if type(process) is subprocess.Popen:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=self.kill_grace)
        except subprocess.TimeoutExpired:
            if type(process) is subprocess.Popen:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            try:
                process.wait(timeout=max(1.0, self.kill_grace))
            except subprocess.TimeoutExpired:
                LOG.error("session process %s survived SIGKILL", process.pid)
        except ProcessLookupError:
            return

    def _verify_adopted_pid(self, pid: int, session: Mapping[str, Any]) -> AdoptedPidVerdict:
        """Corroborate an adopted PID against the session's launch provenance.

        The PID came from the durable row, not from a handle we hold, so it may
        have been recycled. ``session_ref`` is the durable provenance: the bridge
        argv carries it as ``--session-id``/``--resume``, so it is visible in the
        target's full command line. Fails closed (``UNKNOWN``) when we cannot
        observe the process or the row has no usable ref.
        """
        decision = verify_adopted_pid(
            pid,
            session_ref=str(session.get("session_ref") or ""),
            session_id=str(session.get("id") or ""),
            claude_binary=self.claude_binary,
            observer=observe_process,
        )
        if not decision.may_signal:
            LOG.warning("adopted pid not confirmed as ours: %s", decision.as_dict())
        return decision.verdict

    def _terminate_adopted_pid(self, pid: int, session: Mapping[str, Any]) -> bool:
        """Signal an adopted PID's process group, but only if it is provably ours.

        Returns True when the process group was signalled. Anything other than
        an ``OWNED`` verdict returns False without signalling -- an unrelated
        process that inherited the PID must never be killed, and an observation
        failure is not evidence of ownership.
        """
        verdict = self._verify_adopted_pid(pid, session)
        if verdict is not AdoptedPidVerdict.OWNED:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + self.kill_grace
        while self._liveness(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._liveness(pid):
            # Re-verify before escalating: the SIGTERM may have reaped the real
            # session and a new process may already hold the PID.
            if self._verify_adopted_pid(pid, session) is not AdoptedPidVerdict.OWNED:
                return True
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True

    def _finish(
        self,
        session_id: str,
        target: SessionState,
        *,
        killed_by: str | None = None,
    ) -> None:
        session = self.dal.get_session(session_id)
        if session is None:
            return
        current = SessionState(str(session["state"]))
        if current not in TERMINAL_SESSION_STATES:
            # T-OPS-007: terminal transition + approval void in ONE transaction, with
            # killed_by persisted before the (post-commit) manifest write.
            if not self.dal.terminalize_session(
                session_id,
                target.value,
                killed_by=killed_by,
                void_note=f"session entered terminal state {target.value}",
            ):
                return
        # AC-policy hook-auth: shrink the residual on-disk credential surface once
        # the session is truly done; best-effort/idempotent, never raises.
        hook_token.revoke_hook_token(session_id)
        ssh_keys.revoke_ssh_key_grant(session_id)
        # Phase 3: the SINGLE terminal-transition funnel, and therefore the single
        # place a scope release is guaranteed. Every terminal path reaches here —
        # a clean exit and a crash both arrive via _process_finished, kills via
        # _kill_session, approval failures via _service_approval, a launch that
        # threw via spawn's except. Releasing anywhere else would be releasing on
        # one of those paths and not the others.
        self._scope.release(session_id)
        # Same argument for the in-process max-park escalation record: this is
        # the one place every terminal path passes, so it is the one place the
        # session's park-episode state can be dropped exactly once.
        with self._lock:
            self._max_park_extension_notified.pop(session_id, None)
        self._ensure_manifest(session_id, killed_by=killed_by)
        # Memory harvest LAST, strictly after the manifest write. Cold, it pays
        # a metacog import plus a schema migration of the metacog store; that
        # cost must never sit between the terminal commit (already visible to
        # pollers) and the exactly-once audit artifacts above. Best-effort by
        # contract: a harvest fault never affects supervision.
        if target == SessionState.COMPLETED:
            # Output harvest: the reader thread records cost from the stream's
            # result event but nothing ever wrote sessions.output_text for
            # claude-bridge sessions. Harvest the final reply from the LIVE CLI
            # transcript (the shared resolution in
            # omniagentos.sessions.transcript) so output_text carries it — and
            # so the chat bridge's close-time fallback finds it. Best-effort:
            # a harvest fault never affects supervision.
            try:
                if not str(session.get("output_text") or "").strip():
                    from omniagentos.sessions.transcript import final_reply_text

                    harvested = final_reply_text(
                        session,
                        account_lookup=getattr(self.dal, "get_claude_account", None),
                    )
                    if harvested:
                        _persist_session_output(self.dal, session_id, harvested)
            except Exception:  # noqa: BLE001
                LOG.debug("output harvest failed for %s", session_id, exc_info=True)
            # Chat v2 (P0-1): close any open chat turn registered on this
            # session — persists the reply to the chat scope and emits
            # chat.turn.completed. Best-effort by contract: supervision must
            # never fail because a chat reply could not be written.
            try:
                from omniagentos.chats.bridge import get_chat_turn_bridge

                get_chat_turn_bridge().close_turn(session_id)
            except Exception:  # noqa: BLE001
                LOG.debug("chat turn bridge close failed for %s", session_id, exc_info=True)
            try:
                import hashlib

                from omniagentos.contracts import default_db_path
                from omniagentos.metacog.service import MetacogService

                metacog_svc = MetacogService(db_path=default_db_path())
                messages = self.dal.list_session_messages(session_id)
                learnings = []
                for msg in reversed(messages):
                    content = msg.get("content") or msg.get("message") or ""
                    if not isinstance(content, str):
                        continue
                    for line in content.splitlines():
                        line_lower = line.lower()
                        if (
                            "remember:" in line_lower
                            or "lesson learned:" in line_lower
                            or "learning:" in line_lower
                        ):
                            for kw in ["remember:", "lesson learned:", "learning:"]:
                                if kw in line_lower:
                                    idx = line_lower.find(kw)
                                    extracted = line[idx + len(kw) :].strip()
                                    if extracted:
                                        learnings.append(extracted)
                                    break
                if not learnings and messages:
                    last_msg = messages[0]
                    content = last_msg.get("content") or last_msg.get("message") or ""
                    if isinstance(content, str) and content:
                        statement = content.splitlines()[0].strip()
                        if len(statement) > 100:
                            statement = statement[:100] + "..."
                        learnings.append(f"Session {session_id} completed: {statement}")
                if learnings:
                    evidence_id = f"art_sess_{session_id}"
                    if metacog_svc.store.get_artifact(evidence_id) is None:
                        from omniagentos.metacog.contracts import ArtifactEnvelope

                        session_art = ArtifactEnvelope(
                            id=evidence_id,
                            artifact_type="session_summary",
                            company_id=session.get("company_id") or "default",
                            project_id=session.get("project_id"),
                            task_id=session.get("task_id"),
                            content_uri=f"session://{session_id}",
                            content_hash=hashlib.sha256(session_id.encode()).hexdigest(),
                        )
                        metacog_svc.store.register_artifact(session_art)
                    for learning in learnings:
                        metacog_svc.create_memory_candidate(
                            statement=learning,
                            evidence=[evidence_id],
                            memory_type="lesson",
                            project_id=session.get("project_id"),
                            company_id=session.get("company_id") or "default",
                        )
            except Exception as exc:
                LOG.exception("Failed to harvest memories on session completion: %s", exc)

    def _ensure_manifest(self, session_id: str, *, killed_by: str | None = None) -> None:
        # T-OPS-005: a manifest/ledger FS failure for one session must never escape
        # into reconcile or the polling loop and starve approvals/kills for others.
        try:
            # ALREADY-SATISFIED SHORT CIRCUIT (fleet-scale-200). run_once visits
            # EVERY session row on EVERY pass (see the list_sessions call there),
            # and terminal rows accumulate forever, so this method was issuing two
            # queries per historical session per pass — thousands of reads per
            # second once a 200-agent fleet has been running for a day. The
            # manifest is exactly-once and immutable: SessionManifest.write()
            # no-ops when the file exists, and _unsatisfied_manifest_obligations()
            # defines "satisfied" as precisely this file existing. So an existing
            # manifest means both statements below would be no-ops, and one stat()
            # replaces them. The first sighting of a terminal row is unaffected.
            if self.manifest.path_for(session_id).exists():
                return
            session = self.dal.get_session(session_id)
            if session is None:
                return
            state = SessionState(str(session["state"]))
            if state not in TERMINAL_SESSION_STATES:
                return
            # H-39: take on the obligation BEFORE attempting the write. The write
            # below has its own FS-fault isolation (T-OPS-005); recording after it
            # would mean the one case that most needs the ledger -- a write that
            # never landed -- is the one case that never gets an entry.
            self._record_manifest_obligation(session, killed_by=killed_by)
            self.manifest.write(
                session,
                self.dal.list_session_approvals(session_id),
                killed_by=killed_by or session.get("killed_by"),
            )
        except OSError:
            LOG.exception("manifest write failed for session %s (isolated)", session_id)

    def reconcile(self) -> None:
        """Adopt live bridge PIDs and terminalize dead nonterminal rows.

        Special cases:
        - awaiting_approval with a dead pid is PARKED BY DESIGN (the claude -p process
          exits when parked); keep it awaiting_approval for resume (AC-13).
        - a dead bridge process with an actionable approval is stale pre-park state;
          move it to awaiting_approval without terminalizing the approval. Pending and
          approved-but-unconsumed approvals are both actionable.
        - external (report-only) sessions are NEVER pid-killed here (T-OPS-004); their
          pid belongs to a Claude process the supervisor does not own.
        - a starting bridge session with a queued spawn request is a PENDING launch;
          leave it for the ingress rather than terminalizing it.
        Live pids are re-attached and observed to a terminal state by the run_once
        adopted-pid path (T-OPS-001: the DB is the restart-safe transport; a
        re-attached session reaches a correct terminal state when its pid dies).
        """

        for session in self.dal.list_sessions(limit=10_000):
            session_id = str(session["id"])
            try:
                self._reconcile_session(session_id, session)
            except Exception:  # noqa: BLE001 - T-OPS-005 per-session isolation on restart
                LOG.exception("reconcile of session %s failed (isolated)", session_id)

    def _reconcile_session(self, session_id: str, session: Mapping[str, Any]) -> None:
        # Non-Claude bridge rows belong exclusively to ProviderSessionRunner,
        # whose orphan check verifies both pgid liveness and command identity.
        if str(session.get("provider") or "claude") != "claude":
            return
        state = SessionState(str(session["state"]))
        if state in TERMINAL_SESSION_STATES:
            self._ensure_manifest(session_id)
            return
        raw_pid = session.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else 0
        if pid and self._liveness(pid):
            if session.get("source") == "bridge":
                # H-39 case 5: this PID survived a supervisor restart, so it is a
                # durable number, not a handle -- corroborate it against the
                # session's launch provenance before recording it as adopted.
                #
                # Only a FOREIGN verdict blocks adoption, because only FOREIGN is
                # positive evidence that the PID was recycled onto somebody else.
                # UNKNOWN (we could not read the process at all) must not silently
                # drop a live session's bookkeeping -- adoption is not a lethal
                # act. The lethal act is gated separately and strictly: every
                # signal to an adopted PID goes through `_terminate_adopted_pid`,
                # which requires an affirmative OWNED verdict, so an unverifiable
                # PID is tracked but can never be killed.
                if self._verify_adopted_pid(pid, session) is AdoptedPidVerdict.FOREIGN:
                    # Someone else's process now holds this number. Fall through to
                    # the no-live-pid path so the session is terminalized rather
                    # than left running against a process we do not own.
                    self._terminalize_stale_pid(session_id, session, state, pid)
                    return
                with self._lock:
                    self._adopted[session_id] = pid
                    # H-39: adoption is taking responsibility, and `_adopted` is
                    # popped before this session's manifest is written.
                    self._ever_responsible.add(session_id)
                # Phase 3 crash-resume: this supervisor restarted under a session
                # that is still running, so its lease is either about to lapse or
                # already has. Re-take the declared scope (reacquire, not acquire,
                # so a narrowed declaration actually gives paths back) and re-arm
                # the renew. Without this the locks quietly expire under a live
                # writer, which is worse than not having taken them.
                self._scope.adopt(session)
            return
        self._terminalize_stale_pid(session_id, session, state, pid)

    def _terminalize_stale_pid(
        self,
        session_id: str,
        session: Mapping[str, Any],
        state: SessionState,
        pid: int,
    ) -> None:
        """Settle a session whose PID no longer names a process we own.

        Reached either because the PID is dead or because it was positively
        observed as somebody else's (a recycled number). Both mean the session's
        process is gone; neither signals anything.
        """
        if state == SessionState.AWAITING_APPROVAL:
            return  # parked by design
        if session.get("source") != "bridge":
            return  # T-OPS-004: never pid-kill report-only external sessions
        if pid == 0 and self.dal.has_queued_spawn(session_id):
            return  # pending spawn ingress will launch it
        # pid>0 & dead -> a genuinely crashed process (killed); pid==0 -> a launch that
        # never recorded a pid and has no queued request left (failed, nothing to kill).
        target = SessionState.KILLED if pid else SessionState.FAILED
        result = self.dal.reconcile_park_or_kill(session_id, target.value)
        if result == "parked":
            self._notify_awaiting(session_id)

    def resume_swarms_once(self) -> None:
        """WP10 startup resume sweep — once per supervisor lifetime, best-effort.

        Re-adopts swarm runs whose coordinator heartbeat went stale (the adopt
        CAS inside ``resume_swarm`` refuses fresh heartbeats, so a live
        coordinator in another process is never displaced) and reconciles WP3
        provider-exec orphans (pgid + command identity). Runs from
        ``serve_forever`` startup, NOT per ``run_once`` pass — the per-pass
        stale handling stays ``_mark_stale_runs``'s (A5) job. Best-effort:
        a sweep fault must never block supervisor startup.
        """
        if self._swarm_resume_done:
            return
        self._swarm_resume_done = True
        try:
            from omniagentos.swarm.scheduler import resume_stale_swarms

            resume_stale_swarms(db_path=_dal_db_path(self.dal))
        except Exception:  # noqa: BLE001 - startup sweep isolation
            LOG.exception("swarm resume sweep failed (isolated)")

    def serve_forever(self) -> None:
        self.reconcile()
        self.resume_swarms_once()
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - one bad row must not kill the daemon
                LOG.exception("session supervisor poll failed")
            self._stop.wait(self.poll_interval)

    @property
    def is_closed(self) -> bool:
        """True after a successful ordered close (DAL may already be closed)."""
        return self._closed and self._close_error is None

    @property
    def dal_closed(self) -> bool:
        """True once the DAL connection has actually been closed."""
        return self._dal_closed

    @property
    def dal_close_pending(self) -> bool:
        """True when a close deferred the DAL close to a live background thread.

        H-39 case 3: ``close`` invoked from a reader/finalizer cannot close the
        DAL, because the calling thread is still going to use it. The last
        background unit out performs the close instead.
        """
        return self._dal_close_pending and not self._dal_closed

    @property
    def owned_session_ids(self) -> list[str]:
        """Every session this supervisor has ever owned.

        Launched or adopted. H-39: identity that does not depend on the DAL. A
        close over a closed or broken connection cannot ask the database which
        sessions were its responsibility, so this set -- which never shrinks --
        is what keeps a failed close truthful about what it was accountable for.
        """
        with self._lock:
            return sorted(self._ever_responsible)

    def manifest_obligations(self) -> list[ManifestObligation]:
        """Snapshot of every terminal manifest this supervisor owes.

        Readable after the DAL is gone; that is the point. Ordered by session id
        so callers (and failure reports) are deterministic.
        """
        with self._lock:
            all_sids = sorted(self._ever_responsible | set(self._manifest_obligations.keys()))
            result: list[ManifestObligation] = []
            for sid in all_sids:
                if sid in self._manifest_obligations:
                    result.append(self._manifest_obligations[sid])
                else:
                    result.append(ManifestObligation(session_id=sid, recorded_at=utc_now_iso()))
            return result

    def _owns_session_locked(self, session_id: str) -> bool:
        """Caller must hold ``self._lock``. See :attr:`owned_session_ids`."""
        return session_id in self._ever_responsible

    def _record_manifest_obligation(
        self, session: Mapping[str, Any], *, killed_by: str | None = None
    ) -> None:
        """Note that an owned session reached a terminal state and owes a manifest.

        Called from the two manifest funnels with the row already in hand, so it
        costs no extra query and captures the identity while the DAL still works.
        Scoped to owned sessions: reconcile walks every terminal row in the
        database, and this supervisor is not accountable for another one's
        sessions. First write wins -- the obligation is about the session id, and
        re-recording it from a later read would only churn the timestamp.
        """
        session_id = str(session.get("id") or "")
        if not session_id:
            return
        with self._lock:
            if not self._owns_session_locked(session_id):
                return
            existing = self._manifest_obligations.get(session_id)
            if existing and existing.final_state in TERMINAL_SESSION_STATES:
                return
            self._manifest_obligations[session_id] = ManifestObligation(
                session_id=session_id,
                session_ref=str(
                    session.get("session_ref") or (existing.session_ref if existing else "")
                ),
                project_dir=str(
                    session.get("project_dir") or (existing.project_dir if existing else "")
                ),
                final_state=str(session.get("state") or ""),
                killed_by=session.get("killed_by")
                or killed_by
                or (existing.killed_by if existing else None),
                recorded_at=utc_now_iso(),
            )

    def _unsatisfied_manifest_obligations(self) -> set[str]:
        """Owed manifests that are not on disk *right now*.

        Filesystem-only by construction: :meth:`SessionManifest.path_for` needs a
        session id and nothing else, so this is answerable with a dead DAL. It
        also re-proves the file rather than trusting a "written" flag, which is
        what catches a manifest that was written and then removed -- the exact
        shape that let a close over a closed DAL report success.
        """
        unsatisfied: set[str] = set()
        for obligation in self.manifest_obligations():
            try:
                if obligation.manifest_path(self.manifest).exists():
                    continue
            except Exception:  # noqa: BLE001 - unverifiable is unsatisfied
                LOG.exception(
                    "shutdown could not verify owed manifest for session %s",
                    obligation.session_id,
                )
            unsatisfied.add(obligation.session_id)
        return unsatisfied

    def _unflushable_without_dal(self, unflushable: set[str]) -> list[str]:
        """Close report for a shutdown whose DAL cannot be queried.

        Emits the owed obligations to the log because this is the one path where
        the identity is otherwise unrecoverable: the manifest is missing, the
        database cannot be read, and the session id alone does not tell an
        operator which project or provider session to reconstruct.
        """
        owed = self._unsatisfied_manifest_obligations()
        if owed:
            with self._lock:
                all_obs = {o.session_id: o for o in self.manifest_obligations()}
            for session_id in sorted(owed):
                ob = all_obs.get(session_id)
                ob_dict = ob.as_dict() if ob else {"session_id": session_id}
                LOG.error(
                    "shutdown cannot flush owed manifest without a usable DAL: %s",
                    ob_dict,
                )
        return sorted(unflushable | owed)

    def _live_background_ids_locked(self) -> list[str]:
        """Registered background units that may still touch the DAL.

        Caller must hold ``self._lock``. A registered thread that has never been
        started counts as live: ``Thread.is_alive()`` is False before ``start()``,
        so treating it as finished is exactly how a reader escapes a close.
        """
        return sorted(
            session_id
            for session_id, thread in self._background_threads.items()
            if thread is not None and (thread.is_alive() or thread.ident is None)
        )

    def _close_dal_or_defer(self) -> None:
        """Close the DAL, or hand the close to the last live background thread.

        H-39 case 3: when ``close`` runs on a reader/finalizer thread, that
        thread could not be joined (a thread cannot join itself) and will keep
        using the DAL after ``close`` returns. Closing here is precisely the
        OA-005 use-after-close. Deferring is not "skipping": ``dal_close_pending``
        stays true and ``_complete_deferred_dal_close`` finishes the job when the
        thread actually exits.
        """
        with self._lock:
            if self._dal_closed:
                return
            blockers = self._live_background_ids_locked()
            if blockers or self._inflight_launches:
                self._dal_close_pending = True
                LOG.debug(
                    "deferring DAL close until background work exits (threads=%s, "
                    "inflight_launches=%d)",
                    blockers,
                    len(self._inflight_launches),
                )
                return
            self._dal_close_pending = False
        self._close_dal()

    def _close_longhaul_engine(self) -> None:
        """Close the lazily-created longhaul engine without aborting teardown."""
        engine = getattr(self, "_longhaul_engine_instance", None)
        if engine is None:
            return
        try:
            engine.close()
        except Exception:  # noqa: BLE001 - DAL/reader teardown must still complete.
            LOG.exception("longhaul engine close failed during supervisor shutdown")

    def _complete_deferred_dal_close(self) -> None:
        """Perform deferred engine/DAL closes once the last background unit is out.

        Called from a reader's ``finally`` after it has unregistered itself, so
        the calling thread is provably done with longhaul terminal delivery and
        the DAL. The engine always closes first because its terminal path reads
        session state through the DAL.
        """
        with self._lock:
            close_longhaul = self._longhaul_close_pending
            close_dal = self._dal_close_pending and not self._dal_closed
            if not close_longhaul and not close_dal:
                return
            if self._live_background_ids_locked() or self._inflight_launches:
                return
            if close_longhaul:
                self._longhaul_close_pending = False
            if close_dal:
                self._dal_close_pending = False
        if close_longhaul:
            self._close_longhaul_engine()
        if close_dal:
            self._close_dal()

    def _require_open(self, action: str) -> None:
        if self._stop.is_set() or self._closed or self._close_in_progress:
            raise RuntimeError(f"session supervisor refuses {action}: shutting down or closed")

    def shutdown(
        self,
        *,
        terminate_children: bool = True,
        join_timeout: float = DEFAULT_JOIN_TIMEOUT_S,
    ) -> None:
        """Stop accepting work, signal children, join readers/finalizers.

        Does not close the DAL — use :meth:`close` for full lifecycle teardown
        (the L17 / production attachment path). Repeated concurrent calls are
        safe. Raises :class:`SessionSupervisorCloseError` on bounded join
        failure rather than returning success while background work remains.
        """
        self.close(
            terminate_children=terminate_children,
            join_timeout=join_timeout,
            close_dal=False,
        )

    def close(
        self,
        *,
        terminate_children: bool = True,
        join_timeout: float = DEFAULT_JOIN_TIMEOUT_S,
        close_dal: bool = True,
    ) -> None:
        """Ordered, idempotent supervisor teardown (H-39 / OA-005).

        Order:
        1. stop accepting work;
        2. signal producers / children;
        3. join every registered reader/finalizer with a bounded timeout;
        4. flush manifests for terminal sessions (or explicitly fail stragglers);
        5. close the lazy longhaul engine after terminal callbacks drain;
        6. optionally close the DAL only after background work has finished.

        Concurrent and repeated calls are safe. A successful close is a no-op
        thereafter. A timed-out close leaves the instance retryable so a later
        call can finish joins and close the DAL without silent background loss.
        See :mod:`omniagentos.sessions.lifecycle` for the L17 attachment contract.
        """
        while True:
            waiter = False
            with self._lifecycle_lock:
                if self._closed and self._close_error is None:
                    if close_dal and not self._dal_closed:
                        self._close_dal_or_defer()
                    return
                if self._close_in_progress:
                    waiter = True
                else:
                    self._close_in_progress = True
                    self._close_done.clear()
                    self._close_error = None
            if waiter:
                self._close_done.wait()
                with self._lifecycle_lock:
                    if self._closed and self._close_error is None:
                        if close_dal and not self._dal_closed:
                            self._close_dal_or_defer()
                        return
                    if self._close_error is not None and not self._close_in_progress:
                        # Another attempt finished with failure; re-raise unless a
                        # new owner will retry. Loop to try claiming ownership.
                        if self._closed:
                            raise self._close_error
                    # Fall through / loop to claim ownership after a failed attempt.
                continue

            error: BaseException | None = None
            try:
                self._ordered_close(
                    terminate_children=terminate_children,
                    join_timeout=max(0.0, float(join_timeout)),
                    close_dal=close_dal,
                )
            except BaseException as exc:  # noqa: BLE001 - surface to concurrent waiters
                error = exc
                raise
            finally:
                with self._lifecycle_lock:
                    self._close_in_progress = False
                    if error is None:
                        self._closed = True
                        self._close_error = None
                    else:
                        # Leave retryable: background units may still finish.
                        self._closed = False
                        self._close_error = error
                self._close_done.set()
            return

    def _ordered_close(
        self,
        *,
        terminate_children: bool,
        join_timeout: float,
        close_dal: bool,
    ) -> None:
        # 1. Stop accepting work (spawn/launch/poll refuse via _require_open / _stop).
        self._stop.set()
        deadline = time.monotonic() + join_timeout
        current_thread = threading.current_thread()

        # 2. Drain in-flight launches (H-39 case 1). A launch that has already
        #    created its child but not yet registered its reader is invisible to
        #    the thread snapshot below; without this wait the child escapes the
        #    close entirely. `_begin_launch` already refuses NEW launches because
        #    `_stop` is set, so this set only shrinks.
        stranded_launches, orphans = self._drain_inflight_launches(deadline)
        for orphan in orphans:
            try:
                # Ownership of a stranded child transfers to close: its own
                # `_end_launch` has been told not to reap it twice.
                self._terminate_child_process(orphan)
            except Exception:  # noqa: BLE001 - keep draining the rest
                LOG.exception("shutdown could not terminate stranded launch child")

        # Snapshot background units before kill mutates _processes.
        # H-39: use _ever_owned (never shrinks) so crashed finalizers that
        # already removed themselves from _processes are still tracked.
        with self._lock:
            background = list(self._background_threads.items())
            session_ids = sorted(self._processes.keys() | self._adopted.keys())
            owned_session_ids = set(self._ever_owned)

        # 3. Signal producers / children so readers can reach finalization.
        if terminate_children:
            for session_id in session_ids:
                try:
                    if not self._dal_closed:
                        self.dal.request_kill(session_id)
                    self._kill_session(session_id)
                except Exception:  # noqa: BLE001 - continue draining the rest
                    LOG.exception("shutdown kill failed for session %s", session_id)

        # 4. Join readers/finalizers with a shared remaining budget.
        timed_out: list[str] = []
        # H-39 case 3: a close running ON a reader/finalizer cannot join itself.
        # Recording it (rather than silently continuing) is what makes the DAL
        # close defer instead of racing that thread.
        self_join_blocked: list[str] = []
        for session_id, thread in background:
            if thread is None:
                continue
            if thread is current_thread:
                self_join_blocked.append(session_id)
                continue
            # `is_alive()` is False for a registered-but-unstarted thread; such a
            # thread is about to run, so joining it is exactly what we want.
            if not thread.is_alive() and thread.ident is None:
                timed_out.append(session_id)
                continue
            if not thread.is_alive():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive():
                timed_out.append(session_id)

        # Also join any threads registered after the snapshot (auth-retry races
        # should be blocked by _stop, but be defensive).
        with self._lock:
            late = [
                (sid, thr)
                for sid, thr in self._background_threads.items()
                if thr is not None and thr.is_alive() and thr is not current_thread
            ]
        for session_id, thread in late:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive() and session_id not in timed_out:
                timed_out.append(session_id)

        # 5. Flush manifests / explicitly fail anything still non-terminal.
        failed_manifests = self._finalize_on_shutdown(timed_out, owned_session_ids)

        if timed_out or failed_manifests or stranded_launches:
            # Do not close the DAL while live threads may still touch it. The
            # supervisor stays retryable: `_ever_owned` never shrinks and
            # manifest writes are idempotent, so a later close re-attempts the
            # same flush rather than the evidence being lost here.
            raise SessionSupervisorCloseError(
                "session supervisor close did not finish all background work "
                f"(timed_out={timed_out!r}, failed_manifests={failed_manifests!r}, "
                f"stranded_launches={stranded_launches!r})",
                timed_out_sessions=timed_out,
                failed_manifests=failed_manifests,
                stranded_launches=stranded_launches,
            )

        # 6. Close the lazily-created longhaul engine after terminal-delivery
        #    readers have drained, but before the DAL it consults. A close from
        #    one of those readers cannot join itself; asyncio.run() makes its
        #    own terminal callback synchronous, but we still use the existing
        #    last-one-out hook so no later code on that reader can reach the
        #    engine after its store closes.
        if self_join_blocked:
            with self._lock:
                self._longhaul_close_pending = True
            LOG.debug(
                "close() invoked from background thread(s) %s: deferring longhaul close",
                self_join_blocked,
            )
        else:
            self._close_longhaul_engine()

        # 7. Close DAL only after every registered background unit finished --
        #    or defer it to the last one out when this close is itself running on
        #    a reader/finalizer thread that could not be joined (H-39 case 3).
        if close_dal:
            if self_join_blocked:
                LOG.debug(
                    "close() invoked from background thread(s) %s: deferring DAL close",
                    self_join_blocked,
                )
            self._close_dal_or_defer()

    def _drain_inflight_launches(self, deadline: float) -> tuple[list[str], list[Any]]:
        """Wait (bounded) for in-flight launches to finish registering.

        Returns ``(stranded_session_ids, orphan_children)``. An orphan is a child
        process a stranded launch had already created; ownership transfers to the
        caller, which must terminate it -- the launch's own ``_end_launch`` is
        told (by clearing ``ticket.child``) not to reap it a second time.
        """
        with self._launch_cv:
            while self._inflight_launches:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Short slices so a launch that finishes without notifying (it
                # always does notify, but be defensive) cannot pin the budget.
                self._launch_cv.wait(timeout=min(remaining, 0.05))
            stranded_tickets = list(self._inflight_launches.values())
            orphans = [t.child for t in stranded_tickets if t.child is not None]
            for ticket in stranded_tickets:
                ticket.child = None
            stranded = sorted({t.session_id for t in stranded_tickets})
        return stranded, orphans

    def _finalize_on_shutdown(self, timed_out: list[str], owned_session_ids: set[str]) -> list[str]:
        """Ensure terminal events/manifests are flushed or sessions are failed.

        Args:
            timed_out: Session IDs whose reader/finalizer threads did not join in time.
            owned_session_ids: Session IDs this supervisor owned at shutdown start
                (captured BEFORE joins so crashed finalizers are still tracked).

        Returns session ids whose manifest could not be written.

        H-39: explicitly fail any owned non-terminal session without a live finalizer.
        This covers the crashed-finalizer case where the reader thread exits without
        transitioning the session to a terminal state.

        When the DAL cannot be queried, the answer comes from the obligation
        ledger plus the filesystem instead. ``timed_out`` and the crashed-finalizer
        set alone are NOT that answer: they only describe sessions whose threads
        misbehaved, so an owned session that finished cleanly and then lost its
        manifest is absent from both -- which is precisely how a close over a
        closed DAL used to return success with no manifest on disk.
        """
        with self._lock:
            crashed_finalizers = sorted(self._finalizer_failures)
        unflushable = set(timed_out) | set(crashed_finalizers)

        if self._dal_closed:
            # Nothing here is possible without the DAL, and claiming success
            # would be the silent loss H-39 forbids.
            return self._unflushable_without_dal(unflushable)

        try:
            sessions = self.dal.list_sessions(limit=10_000)
        except Exception:  # noqa: BLE001 - DAL may already be unusable
            LOG.exception("shutdown could not list sessions for final flush")
            return self._unflushable_without_dal(unflushable)

        failed_manifests: set[str] = set()
        for session in sessions:
            session_id = str(session.get("id") or "")
            if not session_id:
                continue
            try:
                state = SessionState(str(session["state"]))
            except (KeyError, ValueError):
                continue
            # H-39: fail timed-out sessions, sessions whose finalizer raised, and
            # running sessions this supervisor owns that no longer have a live
            # finalizer -- all three are the crashed-finalizer shape.
            needs_explicit_failure = (
                session_id in timed_out
                or session_id in crashed_finalizers
                or (state == SessionState.RUNNING and session_id in owned_session_ids)
            )
            if needs_explicit_failure and state not in TERMINAL_SESSION_STATES:
                try:
                    self._finish(
                        session_id,
                        SessionState.FAILED,
                        killed_by=SHUTDOWN_KILLED_BY,
                    )
                except Exception:  # noqa: BLE001 - record explicit loss, keep draining
                    LOG.exception("shutdown could not terminalize session %s", session_id)
                    failed_manifests.add(session_id)
                    continue
            if not needs_explicit_failure and state not in TERMINAL_SESSION_STATES:
                continue
            # H-39 case 4: verify against a FRESH read, not `state`. `state` is
            # the pre-`_finish` snapshot -- a crashed-finalizer session was
            # `running` there and only became terminal a line ago. Gating the
            # manifest check on the stale value is exactly how close reported
            # success over a session that never got a manifest.
            if not self._write_manifest_checked(
                session_id,
                killed_by=session.get("killed_by") or SHUTDOWN_KILLED_BY,
            ):
                failed_manifests.add(session_id)

        # Sweep the obligation ledger last. An owned session whose row has gone
        # from the database never appears in the loop above, so a missing
        # manifest for it would go unreported. This makes the close contract one
        # statement instead of two: a successful close can prove every manifest
        # it owes is on disk.
        for session_id in sorted(self._unsatisfied_manifest_obligations() - failed_manifests):
            if not self._write_manifest_checked(session_id, killed_by=SHUTDOWN_KILLED_BY):
                failed_manifests.add(session_id)
        return sorted(failed_manifests)

    def _write_manifest_checked(self, session_id: str, *, killed_by: str | None = None) -> bool:
        """Write a terminal session's manifest and PROVE the file exists.

        :meth:`_ensure_manifest` deliberately isolates FS faults so one bad
        session cannot starve the poll loop. On the close path that isolation is
        wrong: a swallowed failure is a silently lost manifest. Here every
        failure mode -- row gone, row still non-terminal, write error, or file
        absent after an apparently successful write -- returns False so
        ``close`` raises :class:`SessionSupervisorCloseError`.

        Recoverable by construction: the row read is fresh and
        :meth:`SessionManifest.write` is idempotent, so a later ``close`` (or an
        operator retry) re-runs this and succeeds once the fault clears.
        """
        try:
            session = self.dal.get_session(session_id)
        except Exception:  # noqa: BLE001 - report, never mask
            LOG.exception("shutdown could not re-read session %s for manifest flush", session_id)
            return False
        if session is None:
            LOG.error("shutdown found no row for session %s; manifest lost", session_id)
            return False
        try:
            state = SessionState(str(session["state"]))
        except (KeyError, ValueError):
            LOG.error("shutdown found unreadable state for session %s", session_id)
            return False
        if state not in TERMINAL_SESSION_STATES:
            LOG.error(
                "shutdown left session %s non-terminal (%s); no manifest written",
                session_id,
                state.value,
            )
            return False
        self._record_manifest_obligation(session, killed_by=killed_by)
        try:
            self.manifest.write(
                session,
                self.dal.list_session_approvals(session_id),
                killed_by=session.get("killed_by") or killed_by,
            )
        except Exception:  # noqa: BLE001 - report, never mask
            LOG.exception("shutdown manifest write failed for session %s", session_id)
            return False
        try:
            return self.manifest.path_for(session_id).exists()
        except Exception:  # noqa: BLE001 - unverifiable is a failure
            LOG.exception("shutdown could not verify manifest for %s", session_id)
            return False

    def _close_dal(self) -> None:
        if self._dal_closed:
            return
        try:
            # Force-close even when the DAL was constructed with a caller-owned
            # connection: lifecycle ownership ends here.
            connection = getattr(self.dal, "_connection", None)
            owns = bool(getattr(self.dal, "_owns_connection", False))
            self.dal.close()
            if not owns and connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - already closed is fine
                    pass
        finally:
            self._dal_closed = True
            with _OPEN_SUPERVISORS_LOCK:
                _OPEN_SUPERVISORS.discard(self)


Supervisor = SessionSupervisor


def spawn(
    project_dir: str,
    model: str,
    prompt: str,
    budget_usd_max: float | None = None,
    title: str | None = None,
    *,
    db_path: str | None = None,
) -> str:
    """Enqueue a bridge session for the running supervisor to launch (T-OPS-003).

    This does NOT create an unpolled supervisor singleton. It creates the durable
    session row and a spawn-queue request; the single long-lived
    ``python -m omniagentos.sessions.supervisor`` drains the queue in its
    serve_forever loop, so the spawned session is owned by the polling runtime.
    Returns the durable session id.
    """

    dal = SessionsDal(db_path or default_db_path())
    try:
        session_id = new_id("ses")
        session_ref = str(uuid.uuid4())
        now = utc_now_iso()
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "project_dir": _recorded_project_dir(project_dir),
                "provider": "claude",
                "session_ref": session_ref,
                "state": SessionState.STARTING.value,
                "model": model,
                "title": title,
                "budget_usd_max": budget_usd_max,
                "cost_usd": 0.0,
                "kill_requested": 0,
                "last_activity_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        try:
            project = Path(project_dir).expanduser().resolve(strict=True)
            if not project.is_dir():
                raise ValueError("project_dir must be a directory")
            if not model.strip() or not prompt:
                raise ValueError("model and prompt are required")
            if budget_usd_max is not None and budget_usd_max < 0:
                raise ValueError("budget_usd_max cannot be negative")
            dal.enqueue_spawn(
                session_id=session_id,
                project_dir=str(project),
                model=model,
                prompt=prompt,
                budget_usd_max=budget_usd_max,
                title=title,
            )
        except BaseException as exc:
            try:
                _record_session_error(dal, session_id, str(exc) or type(exc).__name__)
            finally:
                dal.terminalize_session(
                    session_id,
                    SessionState.FAILED.value,
                    void_note="session spawn failed before enqueue",
                )
            if isinstance(exc, Exception):
                raise SessionSpawnError(session_id, exc) from exc
            raise
        return session_id
    finally:
        dal.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Supervise OmniAgentOS interactive sessions")
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--kill-grace", type=float, default=3.0)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--ssh-host",
        action="append",
        default=None,
        help=(
            "exact [user@]host to grant each supervised session (repeatable; "
            "default is empty, so operators must pass --ssh-host explicitly)"
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO))
    supervisor = SessionSupervisor(
        db_path=args.db,
        poll_interval=args.poll_interval,
        allowed_ssh_hosts=args.ssh_host,
        kill_grace=args.kill_grace,
    )

    def stop(_signum: int, _frame: object) -> None:
        # Signal path: stop+join only. Full DAL close runs after serve_forever
        # returns so the signal handler stays short and non-reentrant on the lock.
        try:
            supervisor.shutdown()
        except SessionSupervisorCloseError:
            LOG.exception("supervisor shutdown join exceeded budget")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        supervisor.serve_forever()
    finally:
        # L17 / production lifecycle: always finish through ordered close so
        # readers/finalizers join and the DAL is not closed underneath them.
        try:
            supervisor.close()
        except SessionSupervisorCloseError:
            LOG.exception("supervisor close failed after serve_forever")
            raise


if __name__ == "__main__":
    _main()


__all__ = [
    "SessionSupervisor",
    "SessionSupervisorCloseError",
    "Supervisor",
    "bridge_settings_path",
    "build_clean_env",
    "pid_is_alive",
    "spawn",
]
