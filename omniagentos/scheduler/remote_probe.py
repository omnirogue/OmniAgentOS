"""Read-only, cached, single-flight SSH probe of the Initech CRM box.

The ChargeBlast dispute auto-refund/reconcile loop and several remote crons
documented in ``HANDOFF/LOOPS-VISIBILITY.md`` run on the ``initech-roi-calculator``
host (an SSH alias already present in ``~/.ssh/config``), not on this Mac —
``omniagentos.scheduler.system_jobs`` has always had to report their health as a
hardcoded ``unknown``. This module gives that catalog real evidence, without ever
mutating anything on the remote host and without letting a slow/unreachable SSH
connection block an HTTP request.

Design constraints (security surface = zero):

* ONE fixed ``ssh`` argv, hardcoded end to end. No f-strings, no shell
  interpolation, no caller-supplied data ever reaches the command line or the
  remote script. The remote script itself is a single literal string.
* Bounded: ``ConnectTimeout=5`` + a ``subprocess`` timeout (``_SSH_TIMEOUT_S``)
  so a wedged/unreachable host can never hang a caller.
* Read-only: every remote command is a read (``crontab -l``, ``docker ps``,
  ``docker logs --tail``, ``stat``) — nothing loads/unloads/kicks/restarts
  anything.
* Cached to disk (``var/cache/system_jobs_remote.json``) with a TTL; a stale
  cache triggers a single-flight BACKGROUND refresh (a lock-guarded thread),
  so ``.get()`` always returns immediately. Until the first successful probe,
  callers get an honest "pending/failed" snapshot — never an invented health.
* Never raises: every failure mode (ssh missing, timeout, non-zero exit,
  unreadable cache) degrades to an unavailable/pending snapshot.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The remote host alias — must already exist in ~/.ssh/config. Never derived
# from user input or request data.
SSH_HOST_ALIAS = "initech-roi-calculator"

_SSH_TIMEOUT_S = 10
_CACHE_TTL_S = 600  # 10 minutes

CHARGEBLAST_AUTO_REFUND = "initech-crm-chargeblast-auto-refund-1"
CHARGEBLAST_RECONCILE = "initech-crm-chargeblast-reconcile-1"

# Section markers the remote script prints around each read, so stdout can be
# split deterministically regardless of what each command emits.
_SEC_CRON = "===CRON==="
_SEC_DOCKER_PS = "===DOCKER_PS==="
_SEC_LOG_REFUND = "===LOG_REFUND==="
_SEC_LOG_RECONCILE = "===LOG_RECONCILE==="
_SEC_CRON_MTIMES = "===CRON_LOG_MTIMES==="
_SEC_END = "===END==="
_SECTION_MARKERS = (
    _SEC_CRON,
    _SEC_DOCKER_PS,
    _SEC_LOG_REFUND,
    _SEC_LOG_RECONCILE,
    _SEC_CRON_MTIMES,
    _SEC_END,
)

# Remote cron logs whose exact path is grounded in HANDOFF/LOOPS-VISIBILITY.md
# ("kb-drift-check -> /srv/initech-crm/scripts/dev/kb-maintain.sh, logging to
# logs/kb-drift-cron.log") — best-effort mtime evidence only; absence is fine
# (`|| true` everywhere so a missing file never fails the whole probe). Only
# paths with a real recon citation belong here — an unverified guess here
# would read as authoritative evidence it is not.
_REMOTE_CRON_LOG_CANDIDATES = ("/srv/initech-crm/logs/kb-drift-cron.log",)

# The ENTIRE remote script, one fixed literal. Nothing is ever interpolated
# into this string — that is the whole point of the security review this
# module is expected to get (SSH-surface lens: read-only, hardcoded, bounded).
_REMOTE_SCRIPT = (
    f"echo '{_SEC_CRON}'; crontab -l 2>/dev/null || true; "
    f"echo '{_SEC_DOCKER_PS}'; docker ps --format '{{{{.Names}}}}\\t{{{{.Status}}}}' 2>/dev/null || true; "
    f"echo '{_SEC_LOG_REFUND}'; docker logs --tail 3 {CHARGEBLAST_AUTO_REFUND} 2>&1 || true; "
    f"echo '{_SEC_LOG_RECONCILE}'; docker logs --tail 3 {CHARGEBLAST_RECONCILE} 2>&1 || true; "
    f"echo '{_SEC_CRON_MTIMES}'; "
    + "".join(
        f"if [ -e {path} ]; then stat -c '%Y %n' {path} 2>/dev/null || stat -f '%m %N' {path} 2>/dev/null; fi; "
        for path in _REMOTE_CRON_LOG_CANDIDATES
    )
    + f"echo '{_SEC_END}'"
)

_SSH_ARGV: tuple[str, ...] = (
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    SSH_HOST_ALIAS,
    _REMOTE_SCRIPT,
)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Sanitizer — every string that originated on the remote host (or in a local
# crontab line) goes through this before it can reach last_result/health_reason
# on the UNAUTHENTICATED /api/system-jobs. Applied at the point each string is
# turned into evidence (docker_service_health / remote_cron_present), so it
# covers every caller — a live probe, a disk-persisted cache, or a snapshot
# constructed directly by a test — not just the live-SSH code path.
# ---------------------------------------------------------------------------

_MAX_REMOTE_STRING_LEN = 200
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk[-_]\w+"),
    re.compile(r"ghp_\w+"),
    re.compile(r"xoxb-[\w-]+"),
    re.compile(r"AKIA\w+"),
    re.compile(r"pit-\w+"),
    re.compile(r"password\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"api[_-]?token\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN[ \w]*PRIVATE KEY-----.*?-----END[ \w]*PRIVATE KEY-----", re.DOTALL),
)


def sanitize_remote_text(value: str) -> str:
    """Redact live-secret shapes and clamp length. Idempotent and total —
    never raises, always returns a str no matter what junk comes in."""
    out = value
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    if len(out) > _MAX_REMOTE_STRING_LEN:
        out = out[:_MAX_REMOTE_STRING_LEN] + "…[truncated]"
    return out


def _split_sections(stdout: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped in _SECTION_MARKERS:
            current = stripped
            sections[current] = []
            continue
        if current is not None and current != _SEC_END:
            sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


@dataclass(frozen=True)
class ParsedRemoteProbe:
    """The parsed outcome of one remote read. ``ok=False`` carries ``error``
    and nothing else — never invent evidence for a probe that didn't run."""

    ok: bool
    error: str
    probed_at: str
    crontab_lines: tuple[str, ...] = ()
    docker_status: dict[str, str] = field(default_factory=dict)
    log_tail: dict[str, list[str]] = field(default_factory=dict)
    cron_log_mtimes: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def failed(now: datetime, reason: str) -> ParsedRemoteProbe:
        return ParsedRemoteProbe(ok=False, error=reason, probed_at=_iso(now))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "probed_at": self.probed_at,
            "crontab_lines": list(self.crontab_lines),
            "docker_status": dict(self.docker_status),
            "log_tail": {k: list(v) for k, v in self.log_tail.items()},
            "cron_log_mtimes": dict(self.cron_log_mtimes),
        }

    @staticmethod
    def from_dict(data: Any) -> ParsedRemoteProbe | None:
        """Malformed/tampered persisted JSON must never raise into the route —
        every element that isn't the type it claims to be is DROPPED, not
        coerced/guessed (a str() on an attacker-controlled non-str would just
        re-open the same hole from a different angle)."""
        if not isinstance(data, dict):
            return None
        try:
            raw_crontab = data.get("crontab_lines") or ()
            crontab_lines = tuple(item for item in raw_crontab if isinstance(item, str))
            raw_docker = data.get("docker_status") or {}
            docker_status = {
                k: v for k, v in raw_docker.items() if isinstance(k, str) and isinstance(v, str)
            }
            raw_log_tail = data.get("log_tail") or {}
            log_tail: dict[str, list[str]] = {}
            for key, value in raw_log_tail.items():
                if isinstance(key, str) and isinstance(value, list):
                    log_tail[key] = [item for item in value if isinstance(item, str)]
            raw_mtimes = data.get("cron_log_mtimes") or {}
            cron_log_mtimes = {
                k: v for k, v in raw_mtimes.items() if isinstance(k, str) and isinstance(v, str)
            }
            return ParsedRemoteProbe(
                ok=bool(data.get("ok")),
                error=str(data.get("error", "")),
                probed_at=str(data.get("probed_at", "")),
                crontab_lines=crontab_lines,
                docker_status=docker_status,
                log_tail=log_tail,
                cron_log_mtimes=cron_log_mtimes,
            )
        except (TypeError, ValueError, AttributeError):
            return None


def run_probe(*, runner: Callable[..., Any] = subprocess.run, now: datetime | None = None) -> ParsedRemoteProbe:
    """Run the ONE fixed ssh invocation and parse its sections.

    Never raises — every failure mode maps to ``ParsedRemoteProbe.failed``."""
    now = now or datetime.now(UTC)
    try:
        result = runner(
            list(_SSH_ARGV),
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return ParsedRemoteProbe.failed(now, "ssh not found on this host.")
    except subprocess.TimeoutExpired:
        return ParsedRemoteProbe.failed(now, f"ssh probe to {SSH_HOST_ALIAS} timed out after {_SSH_TIMEOUT_S}s.")
    except Exception as exc:  # noqa: BLE001 - probe must never raise into the caller
        return ParsedRemoteProbe.failed(now, f"ssh probe could not be run: {type(exc).__name__}.")
    if result.returncode != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()[:200]
        return ParsedRemoteProbe.failed(
            now, f"ssh to {SSH_HOST_ALIAS} exited {result.returncode}" + (f": {stderr}" if stderr else ".")
        )
    stdout = result.stdout or ""
    if _SEC_END not in stdout:
        # A completed ssh process whose stdout never reached our own final
        # marker means the connection dropped mid-stream (or the remote shell
        # died partway through) — a partial receipt is not a measurement.
        return ParsedRemoteProbe.failed(now, "ssh probe stdout was truncated (missing ===END=== sentinel).")
    sections = _split_sections(stdout)
    crontab_lines = tuple(
        line for line in sections.get(_SEC_CRON, "").splitlines() if line.strip() and not line.strip().startswith("#")
    )
    docker_status: dict[str, str] = {}
    for line in sections.get(_SEC_DOCKER_PS, "").splitlines():
        if "\t" in line:
            name, status = line.split("\t", 1)
            if name.strip():
                docker_status[name.strip()] = status.strip()
    log_tail = {
        CHARGEBLAST_AUTO_REFUND: [line for line in sections.get(_SEC_LOG_REFUND, "").splitlines() if line.strip()],
        CHARGEBLAST_RECONCILE: [line for line in sections.get(_SEC_LOG_RECONCILE, "").splitlines() if line.strip()],
    }
    cron_log_mtimes: dict[str, str] = {}
    for line in sections.get(_SEC_CRON_MTIMES, "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lstrip("-").isdigit():
            try:
                stamp = datetime.fromtimestamp(int(parts[0]), UTC)
            except (OverflowError, OSError, ValueError):
                continue
            cron_log_mtimes[parts[1]] = _iso(stamp)
    if not (crontab_lines or docker_status or any(log_tail.values()) or cron_log_mtimes):
        # Every section came back completely empty. On the real host this
        # never happens (2 chargeblast containers always run) — indistinguishable
        # from every command in the script silently failing behind its own
        # `|| true`, so this is NOT confidently claimed as a successful
        # measurement of "nothing is scheduled here".
        return ParsedRemoteProbe.failed(
            now, "ssh probe completed but every section was empty — remote commands may not have run."
        )
    return ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at=_iso(now),
        crontab_lines=crontab_lines,
        docker_status=docker_status,
        log_tail=log_tail,
        cron_log_mtimes=cron_log_mtimes,
    )


# --------------------------------------------------------------------------- health helpers


_ERROR_MARKERS = ("traceback", "exception", "fatal", "error:")
# Evidence the probe itself couldn't actually READ the logs (docker socket
# permission, daemon unreachable, container gone between `ps` and `logs`) —
# distinct from an application-level error: the service might be fine, we
# just don't have proof, so this maps to `unknown`, not `failing`.
_LOG_ACCESS_FAILURE_MARKERS = (
    "permission denied",
    "cannot connect to the docker daemon",
    "no such container",
)


def docker_service_health(parsed: ParsedRemoteProbe, container: str) -> tuple[str, str, str | None]:
    """(health, reason, last_result) for a chargeblast docker service, derived
    ONLY from evidence actually captured by the probe. Every string embedded
    in the reason/last_result is sanitized — this is the only path raw remote
    docker output takes into the unauthenticated API response."""
    status = parsed.docker_status.get(container)
    tail = parsed.log_tail.get(container, [])
    last_line = tail[-1] if tail else None
    sanitized_status = sanitize_remote_text(status) if status is not None else None
    sanitized_last_line = sanitize_remote_text(last_line) if last_line is not None else None
    if status is None:
        return (
            "failing",
            f"Container {container} was not present in `docker ps` on {SSH_HOST_ALIAS}.",
            None,
        )
    status_lower = status.lower()
    if not status_lower.startswith("up") or "(unhealthy)" in status_lower:
        return (
            "failing",
            f"Container {container} status is {sanitized_status!r} (not running/healthy).",
            sanitized_status,
        )
    tail_text = "\n".join(tail).lower()
    if tail and any(marker in tail_text for marker in _LOG_ACCESS_FAILURE_MARKERS):
        return (
            "unknown",
            f"Container {container} is {sanitized_status!r} but its log tail could not be read to "
            f"confirm health: {sanitized_last_line!r}.",
            sanitized_last_line,
        )
    if tail and any(marker in tail_text for marker in _ERROR_MARKERS):
        return (
            "failing",
            f"Container {container} is {sanitized_status!r} but its recent log tail looks like an "
            f"error: {sanitized_last_line!r}.",
            sanitized_last_line,
        )
    if not tail:
        return (
            "unknown",
            f"Container {container} is {sanitized_status!r} but no recent log lines were captured to "
            "judge pass/fail.",
            sanitized_status,
        )
    return (
        "healthy",
        f"Container {container} is {sanitized_status!r}; recent log tail looks like normal pass output.",
        sanitized_last_line,
    )


def remote_cron_present(parsed: ParsedRemoteProbe, fragment: str) -> tuple[bool, str]:
    """Whether *fragment* appears in any captured remote crontab line — the
    minimum evidence HANDOFF asks for on the remote crons. The returned line
    is sanitized before it can reach an unauthenticated API response."""
    for line in parsed.crontab_lines:
        if fragment in line:
            return True, sanitize_remote_text(line.strip())
    return False, ""


# --------------------------------------------------------------------------- cache


@dataclass(frozen=True)
class RemoteProbeSnapshot:
    """What ``system_jobs.list_system_jobs`` actually consumes: whether real
    remote evidence is available right now, and if so the parsed probe."""

    available: bool
    reason: str
    probed_at: str | None
    parsed: ParsedRemoteProbe | None


_PENDING_REASON = "remote probe pending/failed: no successful probe yet."
# How long a SUCCESSFUL measurement is allowed to keep reporting `available`
# after refreshes start failing. Past this, "serve stale-but-once-good data"
# becomes "silently report a dead probe as healthy" — HEALTH-01.
_EVIDENCE_MAX_AGE_FACTOR = 2.0


class RemoteProbeCache:
    """TTL-cached, single-flight wrapper around :func:`run_probe`.

    ``get()`` never blocks on SSH: with ``background=True`` (the production
    default) it serves the last good (disk-persisted) result instantly and
    kicks off ONE background refresh when stale (lock-guarded — a second
    caller arriving mid-refresh is a no-op, not a second ssh process). Until
    the first successful probe ever completes, ``get()`` reports an honest
    "pending/failed" snapshot.

    A successful measurement does NOT stay `available` forever just because
    refreshes keep failing: once the newest successful probe is older than
    ``_EVIDENCE_MAX_AGE_FACTOR`` × ``ttl_s`` AND the most recent refresh
    attempt failed, the snapshot degrades to unavailable with a reason naming
    both when evidence was last good and why refreshing has been failing —
    see HEALTH-01.
    """

    def __init__(
        self,
        *,
        cache_path: Path,
        runner: Callable[..., Any] = subprocess.run,
        ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        background: bool = True,
    ) -> None:
        self._cache_path = cache_path
        self._runner = runner
        self._ttl_s = ttl_s
        self._clock = clock
        self._background = background
        self._lock = threading.Lock()
        self._refreshing = False
        self._memory: ParsedRemoteProbe | None = None
        self._last_error: str | None = None
        self._failing_since: str | None = None
        self._load_from_disk()

    # -- persistence --------------------------------------------------------

    def _load_from_disk(self) -> None:
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        parsed = ParsedRemoteProbe.from_dict(data)
        if parsed is not None and parsed.ok:
            self._memory = parsed

    def _save_to_disk(self, parsed: ParsedRemoteProbe) -> None:
        """Atomic (temp file + os.replace) write, private (0600) permissions.

        ``os.replace`` swaps the DIRECTORY ENTRY at ``cache_path`` rather than
        opening through it — if ``cache_path`` is itself a symlink (SEC-02:
        an attacker-planted symlink pointing at some other file this process
        can write), the symlink is atomically replaced by a regular file and
        whatever it pointed at is left untouched. Never opens ``cache_path``
        directly for writing, which is what would follow the symlink."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._cache_path.parent), prefix=".system_jobs_remote-", suffix=".tmp"
            )
        except OSError:
            return  # best-effort cache write; never let this fail the request
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(parsed.to_dict(), indent=2))
            os.chmod(tmp_name, 0o600)  # explicit — must not depend on umask
            os.replace(tmp_name, self._cache_path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)

    # -- freshness / evidence age ----------------------------------------------

    def _parse_stamp(self, stamp: str | None) -> datetime | None:
        if not stamp:
            return None
        try:
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None

    def _age_seconds(self, stamp: str | None) -> float | None:
        parsed_stamp = self._parse_stamp(stamp)
        if parsed_stamp is None:
            return None
        return (self._clock() - parsed_stamp).total_seconds()

    def _is_fresh(self, parsed: ParsedRemoteProbe) -> bool:
        age = self._age_seconds(parsed.probed_at)
        return age is not None and age < self._ttl_s

    # -- public API -----------------------------------------------------------

    def get(self) -> RemoteProbeSnapshot:
        cached = self._memory
        if cached is None or not cached.ok or not self._is_fresh(cached):
            # No cache, or it's stale — refresh (sync when background=False,
            # so the caller sees the outcome immediately; async otherwise).
            self._trigger_refresh()
            cached = self._memory
        if cached is not None and cached.ok:
            age = self._age_seconds(cached.probed_at) or 0.0
            if age > _EVIDENCE_MAX_AGE_FACTOR * self._ttl_s and self._last_error:
                # A once-good measurement that is now too old AND whose
                # refreshes are actively failing must NOT keep reading as
                # live evidence — that is exactly the stale-green bug.
                reason = (
                    f"last successful probe at {cached.probed_at}; refresh failing since "
                    f"{self._failing_since}: {self._last_error}"
                )
                return RemoteProbeSnapshot(False, reason, cached.probed_at, None)
            return RemoteProbeSnapshot(True, "", cached.probed_at, cached)
        reason = _PENDING_REASON if not self._last_error else f"remote probe pending/failed: {self._last_error}"
        return RemoteProbeSnapshot(False, reason, None, None)

    def refresh_sync(self) -> ParsedRemoteProbe:
        """Force a synchronous refresh (test/CLI use only — the HTTP path
        never calls this)."""
        with self._lock:
            self._refreshing = True
        try:
            return self._refresh()
        finally:
            with self._lock:
                self._refreshing = False

    # -- single-flight refresh ------------------------------------------------

    def _trigger_refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        if self._background:
            thread = threading.Thread(target=self._refresh_and_clear_flag, daemon=True)
            thread.start()
        else:
            self._refresh_and_clear_flag()

    def _refresh_and_clear_flag(self) -> None:
        try:
            self._refresh()
        finally:
            with self._lock:
                self._refreshing = False

    def _refresh(self) -> ParsedRemoteProbe:
        parsed = run_probe(runner=self._runner, now=self._clock())
        if parsed.ok:
            self._memory = parsed
            self._last_error = None
            self._failing_since = None
            self._save_to_disk(parsed)
        else:
            self._last_error = parsed.error
            if self._failing_since is None:
                self._failing_since = parsed.probed_at
        return parsed
