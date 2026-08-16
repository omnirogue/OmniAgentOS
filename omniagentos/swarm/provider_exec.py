"""Supervised subscription-CLI sessions for non-Claude swarm workers.

This is deliberately a wrapper around the existing adapters, not a second
SessionSupervisor.  It gives codex/grok/gemini/kimi/qwen the same durable sessions
row, kill flag, process-group, and await-terminal contract as Claude while the
swarm scheduler remains the sole retry/respawn owner.

``provider_doctor`` is a LIVE auth/process check.  It launches installed CLIs
and is never part of the unit-test suite.
"""

from __future__ import annotations

import logging
import os
import queue
import shlex
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from omniagentos.adapters.common import disable_inner_sandbox as _disable_inner_sandbox
from omniagentos.contracts import (
    AgentInput,
    BudgetSpec,
    HarnessType,
    default_db_path,
    new_id,
    utc_now_iso,
)
from omniagentos.sessions.dal import (
    TERMINAL_SESSION_STATES,
    SessionsDal,
    SessionState,
)
from omniagentos.toolplane.scrub import scrub_optional_text, scrub_text

LOG = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = frozenset({"codex", "grok", "gemini", "kimi", "qwen"})
DENIED_RISK_CLASSES = frozenset({"external", "deploy", "destructive"})

PROVIDER_CONFIG_ENV: dict[str, str] = {
    "codex": "CODEX_HOME",
    "grok": "GROK_HOME",
    "gemini": "GEMINI_CLI_HOME",
    "kimi": "KIMI_CODE_HOME",
    "qwen": "QWEN_HOME",
}
_DEFAULT_CONFIG_DIRS: dict[str, str] = {
    "codex": "~/.codex",
    "grok": "~/.grok",
    "gemini": "~/.gemini",
    "kimi": "~/.kimi-code",
    "qwen": "~/.qwen",
}
_SAFE_ENV_NAMES = ("PATH", "HOME", "TMPDIR", "LANG", "TERM")
_OUTPUT_DONE = object()
_OUTPUT_FAILED = object()

# O-8: router intent (swarm_attempts.effort at open_attempt) can diverge from
# the value that actually reaches argv after CBM override in swarm/spawn.py.
# Log key is a module constant so tests can assert the exact string.
EFFORT_DIVERGENCE_WARNING = "dispatched effort diverges from requested effort"


def _provider_probe_error_detail(error: Any) -> str:
    """Return provider detail without ``await_terminal``'s session scaffold."""
    text = str(error or "").strip()
    if not text.startswith("session "):
        return text
    _prefix, ended, terminal = text.partition(" ended in ")
    if ended:
        _state, separator, detail = terminal.partition(": ")
        return detail if separator else ""
    harness_markers = (" is missing from ", " has invalid state ", " timed out after ")
    if any(marker in text for marker in harness_markers):
        return ""
    return text


def provider_doctor_error_text(status: dict[str, Any]) -> str:
    """Combine exception text with provider-supplied terminal detail only."""
    values = (status.get("error"), _provider_probe_error_detail(status.get("probe_error")))
    return "\n".join(str(value) for value in values if value)


def classify_provider_doctor_outcome(provider: str, status: dict[str, Any]) -> str:
    """Classify one doctor result into a known outcome, never ok by default.

    ``provider_doctor`` is intentionally a structural process check, so a
    false ``clean_exit`` alone cannot distinguish a provider auth failure from
    a harness quirk.  The preserved terminal result supplies that missing
    evidence while this helper keeps the classification reusable by consumers
    and directly unit-testable without running a live CLI.  Limit-shaped errors
    preserve ``auth_error``, ``quota_exhausted``, ``transient_rate_limit``, or
    ``overloaded``; remaining concrete failures are ``harness_error`` and an
    absent probe is ``unavailable``.
    """
    probe_status = status.get("probe_status")
    if status.get("ok") is True and probe_status in (None, "ok"):
        return "ok"

    error_text = provider_doctor_error_text(status)
    if error_text:
        from omniagentos.longhaul.limits import classify_limit_text

        limit_outcome = classify_limit_text(provider, error_text)
        if limit_outcome is not None:
            return limit_outcome

    if error_text or probe_status is not None:
        return "harness_error"
    return "unavailable"


# Inner-sandbox disable for self-sandboxing CLIs (codex/grok) now lives in
# omniagentos.adapters.common (shared with the adapter-direct reviewer path).
# Nested Seatbelt is impossible on macOS (sandbox_apply is denied inside an
# already-sandboxed process), so under provider-exec's outer profile the CLI's
# internal sandbox must be off: codex is rewritten to danger-full-access
# (live-verified); grok's --sandbox pair is REMOVED entirely — grok is NOT
# codex-derived vocabulary-wise (it refuses 'danger-full-access' at startup)
# and its documented default profile is off (lead decision D2).


def _strip_surrounding_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _argv_effort_from_flag(
    command: Sequence[str],
    *,
    flags: Sequence[str],
) -> str | None:
    """Last occurrence of any flag in ``flags`` wins (CLI repeated-flag rule)."""

    found: str | None = None
    i = 0
    n = len(command)
    while i < n:
        token = str(command[i])
        matched = False
        for flag in flags:
            if token == flag:
                # Spaced form: --flag <value>. Trailing flag alone -> None.
                if i + 1 < n:
                    found = _strip_surrounding_quotes(str(command[i + 1]))
                    i += 2
                else:
                    found = None
                    i += 1
                matched = True
                break
            prefix = f"{flag}="
            if token.startswith(prefix):
                # Joined form: --flag=<value>
                found = _strip_surrounding_quotes(token[len(prefix) :])
                i += 1
                matched = True
                break
        if not matched:
            i += 1
    return found if found else None


def _argv_effort_codex(command: Sequence[str]) -> str | None:
    """Extract model_reasoning_effort from codex -c / --config tokens."""

    found: str | None = None
    i = 0
    n = len(command)
    key = "model_reasoning_effort="
    while i < n:
        token = str(command[i])
        if token in ("-c", "--config"):
            if i + 1 >= n:
                found = None
                i += 1
                continue
            payload = str(command[i + 1])
            if key in payload:
                raw = payload.split(key, 1)[1]
                value = _strip_surrounding_quotes(raw)
                found = value if value else None
            i += 2
            continue
        for flag in ("-c=", "--config="):
            if token.startswith(flag):
                payload = token[len(flag) :]
                if key in payload:
                    raw = payload.split(key, 1)[1]
                    value = _strip_surrounding_quotes(raw)
                    found = value if value else None
                else:
                    # Joined form without the key is not an effort token.
                    pass
                break
        i += 1
    return found


def _argv_effort_grok(command: Sequence[str]) -> str | None:
    return _argv_effort_from_flag(
        command,
        flags=("--reasoning-effort", "--effort"),
    )


def _argv_effort_claude(command: Sequence[str]) -> str | None:
    return _argv_effort_from_flag(command, flags=("--effort",))


# Single source of truth: which providers expose a reasoning-effort CLI knob,
# and how to read the value back out of a constructed argv. Providers absent
# from this table (gemini/kimi/qwen, unknowns) have no knob.
_ARGV_EFFORT_EXTRACTORS: dict[str, Callable[[Sequence[str]], str | None]] = {
    "codex": _argv_effort_codex,
    "grok": _argv_effort_grok,
    "claude": _argv_effort_claude,
}


# WHY parse effort back out of argv rather than recording the request field:
# ``request.effort`` / the spawn ``effort`` parameter is ROUTER INTENT. A later
# step (CBM allocation in swarm/spawn.py::resolve_spawn_effort) is free to
# override it before the adapter builds argv. open_attempt writes the intent
# first (scheduler.py), then CBM replaces request.effort, and nothing used to
# write the dispatched value back — live run swr_0bc26f3a43694e099e4c attempt
# swa_d54257b1044b4f91bfc6 recorded effort='xhigh' while argv carried
# model_reasoning_effort="high" (board_tasks.swarm_json applied_effort=high).
# Single-sourcing the recorded value from the final provider CLI argv is the
# only way the attempt/session rows can match what the process actually ran.
def argv_reasoning_effort(command: Sequence[str], provider: str) -> str | None:
    """Return the reasoning-effort value literally present in provider CLI argv.

    The value is read out of an already-constructed argv — never recomputed
    from the request. When a flag appears more than once, the LAST occurrence
    wins (matching how these CLIs resolve repeated flags). Malformed argv and
    providers with no effort knob return None; this never raises.
    """

    try:
        key = str(provider or "").strip().lower()
        extractor = _ARGV_EFFORT_EXTRACTORS.get(key)
        if extractor is None:
            return None
        return extractor(command)
    except Exception:  # noqa: BLE001 -- argv parse is best-effort telemetry
        return None


def provider_has_effort_knob(provider: str) -> bool:
    """True iff the provider's CLI exposes a reasoning-effort knob.

    Membership in ``_ARGV_EFFORT_EXTRACTORS`` is the sole definition so the
    "has a knob" check cannot drift from the extractors.
    """

    return str(provider or "").strip().lower() in _ARGV_EFFORT_EXTRACTORS


class ProviderExecPolicyError(RuntimeError):
    """A provider-exec launch refused by a hard enforcement rule."""


class ProviderExecSpawnError(RuntimeError):
    """A launch failure whose durable session row remains inspectable."""

    def __init__(self, session_id: str, error: BaseException) -> None:
        detail = str(error) or type(error).__name__
        super().__init__(f"provider session {session_id} failed to spawn: {detail}")
        self.session_id = session_id


class _Adapter(Protocol):
    name: str
    cli: str

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]: ...

    def _sandboxed_launch(
        self,
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        *,
        provider: str | None = None,
        provider_config_dir: str | None = None,
    ) -> list[str]: ...

    def _parse(self, stdout: str) -> Any: ...


@dataclass
class _ManagedProviderProcess:
    session_id: str
    provider: str
    account_id: str | None
    adapter: _Adapter
    process: Any
    command: list[str]
    started_monotonic: float
    wall_timeout_seconds: float | None
    thread: threading.Thread | None = None
    stream_events: int = 0


def _resolve_adapter(provider: str) -> _Adapter:
    from omniagentos.adapters.registry import resolve_adapter

    return resolve_adapter(HarnessType(f"cli-{provider}"))  # type: ignore[return-value]


def _pgid_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _process_command(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


class ProviderSessionRunner:
    """Launch and supervise provider adapters as durable bridge sessions."""

    def __init__(
        self,
        dal: SessionsDal | None = None,
        *,
        db_path: str | None = None,
        process_factory: Callable[..., Any] = subprocess.Popen,
        adapter_resolver: Callable[[str], _Adapter] = _resolve_adapter,
        poll_interval: float = 0.25,
        kill_grace: float = 3.0,
        wall_timeout_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        getpgid: Callable[[int], int] = os.getpgid,
        killpg: Callable[[int, int], None] = os.killpg,
        pgid_liveness: Callable[[int], bool] = _pgid_alive,
        command_reader: Callable[[int], str] = _process_command,
        reserve_account: Callable[..., Any] | None = None,
        convert_reservation: Callable[..., bool] | None = None,
        report_outcome: Callable[..., Any] | None = None,
    ) -> None:
        self.db_path = db_path or default_db_path()
        self.dal = dal or SessionsDal(self.db_path)
        self.poll_interval = max(0.05, float(poll_interval))
        self.kill_grace = max(0.0, float(kill_grace))
        self.wall_timeout_seconds = (
            None if wall_timeout_seconds is None else max(0.001, float(wall_timeout_seconds))
        )
        self._process_factory = process_factory
        self._adapter_resolver = adapter_resolver
        self._monotonic = monotonic
        self._sleep = sleep
        self._getpgid = getpgid
        self._killpg = killpg
        self._pgid_liveness = pgid_liveness
        self._command_reader = command_reader

        if reserve_account is None or convert_reservation is None or report_outcome is None:
            from omniagentos.routing import limit_state

            reserve_account = reserve_account or limit_state.reserve_account
            convert_reservation = convert_reservation or limit_state.convert_reservation
            report_outcome = report_outcome or limit_state.report_outcome
        self._reserve_account = reserve_account
        self._convert_reservation = convert_reservation
        self._report_outcome = report_outcome

        self._lock = threading.RLock()
        self._processes: dict[str, _ManagedProviderProcess] = {}
        self._stream_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Spawn / process lifecycle
    # ------------------------------------------------------------------

    def spawn(
        self,
        provider: str,
        model: str,
        prompt: str,
        working_dir: str,
        board_task_id: str,
        swarm_run_id: str,
        budget_usd_max: float | None,
        idle_minutes: float | None,
        risk_class: str,
        account_id: str | None = None,
        wall_timeout_seconds: float | None = None,
        effort: str | None = None,
        extra_write_roots: Sequence[str] | None = None,
    ) -> str:
        """Create a real session row, launch the adapter, and return its id.

        ``effort`` (048) is the router-decided reasoning effort; it rides
        ``AgentInput.metadata["reasoning_effort"]`` so the adapters that have a
        CLI knob thread it (codex: ``-c model_reasoning_effort="..."``, grok:
        ``--reasoning-effort``) and the ones without one (kimi/gemini/qwen) skip it
        cleanly at their own ``_command``.

        ``extra_write_roots`` (merge-model Phase 2) adds Seatbelt write roots
        beyond the working dir + provider state dir — worktree mode passes the
        main repo's git COMMON dir so worker commits can write objects/refs
        (the profile still denies ``.git/hooks`` + ``.git/config`` inside a
        granted git dir)."""

        provider = provider.strip().lower()
        risk = risk_class.strip().lower()
        # P0 enforcement: this must remain local to the spawner.  A router bug,
        # stale plan, or direct caller cannot bypass it.
        if provider != "claude" and risk in DENIED_RISK_CLASSES:
            raise ProviderExecPolicyError(
                f"provider {provider!r} may not execute risk_class {risk!r}"
            )
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"provider-exec supports {', '.join(sorted(SUPPORTED_PROVIDERS))}; got {provider!r}"
            )
        if not prompt:
            raise ValueError("prompt is required")
        project = Path(working_dir).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError("working_dir must be a directory")
        if budget_usd_max is not None and budget_usd_max < 0:
            raise ValueError("budget_usd_max cannot be negative")
        if idle_minutes is not None and idle_minutes <= 0:
            raise ValueError("idle_minutes must be positive")
        timeout = (
            self.wall_timeout_seconds
            if wall_timeout_seconds is None
            else max(0.001, float(wall_timeout_seconds))
        )

        reservation: Any | None = None
        selected_account_id = account_id
        config_dir: str | None = None
        if selected_account_id is None:
            try:
                reservation = self._reserve_account(provider, db_path=self.db_path)
            except Exception:  # noqa: BLE001 - default CLI login remains usable.
                LOG.debug("provider account reservation failed", exc_info=True)
            if reservation is not None:
                selected_account_id = str(reservation.account.account_id)
                config_dir = reservation.account.config_dir
        if config_dir is None and selected_account_id is not None:
            config_dir = self._account_config_dir(selected_account_id, provider)
        config_dir = os.path.abspath(
            os.path.expanduser(config_dir or _DEFAULT_CONFIG_DIRS[provider])
        )
        config_env_dir, config_state_dir = self._provider_config_paths(provider, config_dir)

        session_id = new_id("ses")
        marker = f"[swarm:{board_task_id}]"
        now = utc_now_iso()
        row = {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(project),
            "provider": provider,
            "session_ref": None,
            "state": SessionState.STARTING.value,
            "pid": None,
            "model": model or None,
            "title": f"{marker} {provider}:{model or 'default'}",
            "budget_usd_max": budget_usd_max,
            # No provider report exists yet. NULL is unknown; 0.0 would claim
            # this live CLI invocation is free and defeat dollar caps.
            "cost_usd": None,
            "kill_requested": 0,
            "last_activity_at": now,
            "created_at": now,
            "updated_at": now,
            "prompt": prompt,
            "account_id": selected_account_id,
            "idle_minutes": idle_minutes,
        }
        process: Any | None = None
        try:
            self.dal.create_session(row)
            mark_owned = getattr(self.dal, "mark_orchestrator_session", None)
            if callable(mark_owned):
                mark_owned(session_id, swarm_run_id)
            if reservation is not None:
                self._convert_reservation(str(reservation.id), session_id, db_path=self.db_path)

            adapter = self._adapter_resolver(provider)
            agent_input = AgentInput(
                run_id=swarm_run_id,
                task_id=board_task_id,
                prompt=prompt,
                working_dir=str(project),
                model=model or None,
                budget=BudgetSpec(
                    cost_usd_max=budget_usd_max,
                    wall_ms_max=int(timeout * 1000) if timeout is not None else None,
                ),
                metadata={
                    "sandbox": {"level": "workspace_write"},
                    "cli_unattended_elevated": True,
                    **(
                        {"reasoning_effort": effort.strip().lower()}
                        if isinstance(effort, str) and effort.strip()
                        else {}
                    ),
                },
            )
            command = adapter._command(agent_input, prompt, None)
            # The OUTER Seatbelt profile (wrap below) is the enforcement layer
            # for provider CLIs (plan: cross-cutting decision #2). A CLI's OWN
            # sandbox cannot nest inside it -- codex's sandbox_apply gets
            # 'Operation not permitted' and every write silently dies (live
            # failure swr_d3855d2d/swr_5b42d209: zero files, review_denied).
            # So the inner sandbox is disabled for the CLIs that self-sandbox,
            # but ONLY when the outer wrap will actually engage
            # (runner.sandbox.wrap_available -- the same predicate
            # CliAdapter._invoke gates on). Provider-exec is an unattended
            # surface, so an unavailable or unproven outer sandbox must refuse
            # before process creation rather than relying on CLI-local flags.
            from omniagentos.runner import sandbox as _runner_sandbox

            if not _runner_sandbox.wrap_available(command, str(project)):
                raise RuntimeError(
                    "provider_exec_no_sandbox_refused: sandbox unavailable or "
                    "unproven; refusing unattended provider launch"
                )
            command = _disable_inner_sandbox(command, provider)
            launch = adapter._sandboxed_launch(
                command,
                str(project),
                [config_state_dir, *(str(r) for r in (extra_write_roots or []))],
                provider=provider,
                provider_config_dir=config_state_dir,
            )
            env = self._provider_env(provider, config_env_dir, session_id)
            process = self._process_factory(
                launch,
                cwd=str(project),
                env=env,
                stdin=subprocess.PIPE if provider == "codex" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.dal.set_pid(session_id, int(process.pid))
            if not self.dal.update_session_state(
                session_id,
                SessionState.RUNNING.value,
                expect=SessionState.STARTING.value,
            ):
                raise RuntimeError("lost starting -> running transition")

            # O-8: record dispatched effort from the FINAL provider CLI argv
            # (``command``), not from ``launch``. ``_sandboxed_launch`` wraps
            # the provider argv in a Seatbelt invocation and may requote, so
            # ``command`` is what the CLI actually executes inside the wrap.
            # Positioned after STARTING->RUNNING so "dispatched" means the
            # process has actually launched.
            self._record_dispatched_effort(
                session_id=session_id,
                board_task_id=board_task_id,
                provider=provider,
                command=command,
                requested_effort=effort,
            )

            handle = _ManagedProviderProcess(
                session_id=session_id,
                provider=provider,
                account_id=selected_account_id,
                adapter=adapter,
                process=process,
                command=launch,
                started_monotonic=self._monotonic(),
                wall_timeout_seconds=timeout,
            )
            with self._lock:
                self._processes[session_id] = handle
                self._stream_counts[session_id] = 0
            if provider == "codex":
                self._write_codex_prompt(process, prompt)
            reader = threading.Thread(
                target=self._read_process,
                args=(handle,),
                name=f"provider-session-{session_id}",
                daemon=True,
            )
            handle.thread = reader
            reader.start()
            return session_id
        except Exception as exc:
            if process is not None:
                self._terminate_group(process)
            with self._lock:
                self._processes.pop(session_id, None)
            if reservation is not None:
                # If create/convert failed before the normal handoff, best effort
                # deletion keeps the short-lived reservation from double-counting.
                try:
                    self._convert_reservation(str(reservation.id), session_id, db_path=self.db_path)
                except Exception:
                    pass
            existing = self.dal.get_session(session_id)
            if existing is not None:
                self._persist_error(session_id, f"spawn failed: {exc}")
                self.dal.terminalize_session(
                    session_id,
                    SessionState.FAILED.value,
                    void_note="provider process failed to spawn",
                )
            raise ProviderExecSpawnError(session_id, exc) from exc

    @staticmethod
    def _write_codex_prompt(process: Any, prompt: str) -> None:
        stream = getattr(process, "stdin", None)
        if stream is None:
            return
        try:
            stream.write(prompt)
            stream.flush()
            stream.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _provider_env(self, provider: str, config_dir: str, session_id: str) -> dict[str, str]:
        """Explicit allowlist plus exactly one provider config-directory var."""

        env = {name: os.environ[name] for name in _SAFE_ENV_NAMES if name in os.environ}
        env[PROVIDER_CONFIG_ENV[provider]] = config_dir
        # This is an opaque loopback/session capability marker, not an API key.
        env["OMNIAGENTOS_BRIDGE_SESSION_ID"] = session_id
        # Gemini headless (provider-exec / CI): without this the CLI exits 55
        # for untrusted folders. Safe for automation — the outer Seatbelt
        # profile is still the write confinement. See:
        # https://geminicli.com/docs/cli/trusted-folders/
        if provider == "gemini":
            env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
            # Prefer ambient API key when present; otherwise the CLI falls
            # back to Keychain / GEMINI_CLI_HOME/.gemini/.env.
            for key_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                value = os.environ.get(key_name)
                if value:
                    env[key_name] = value
                    break
            else:
                # Load key from the account's .env without requiring the parent
                # process to have sourced it (common for launchd / sessions daemon).
                for candidate in (
                    Path(config_dir) / ".gemini" / ".env",
                    Path(config_dir) / ".env",
                    Path.home() / ".gemini" / ".env",
                ):
                    try:
                        text = candidate.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    for line in text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        name, raw = line.split("=", 1)
                        if name.strip() in {"GEMINI_API_KEY", "GOOGLE_API_KEY"}:
                            env[name.strip()] = raw.strip().strip('"').strip("'")
                            break
                    if "GEMINI_API_KEY" in env or "GOOGLE_API_KEY" in env:
                        break
        return env

    @staticmethod
    def _provider_config_paths(provider: str, config_dir: str) -> tuple[str, str]:
        """Return (env value, actual state root) for provider-specific semantics."""

        # GEMINI_CLI_HOME replaces os.homedir(); Gemini then appends ".gemini".
        # Account rows may reasonably store either that synthetic home or the
        # concrete .gemini directory, so normalize both without widening the
        # sandbox to the real HOME directory.
        if provider == "gemini":
            path = Path(config_dir)
            if path.name == ".gemini":
                return str(path.parent), str(path)
            return str(path), str(path / ".gemini")
        return config_dir, config_dir

    def _account_config_dir(self, account_id: str, provider: str) -> str | None:
        try:
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT config_dir FROM claude_accounts WHERE id = ? AND provider = ? LIMIT 1",
                (account_id, provider),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        value = row["config_dir"] if row is not None else None
        return str(value) if value else None

    @staticmethod
    def _pump_output(process: Any, output: queue.Queue[Any]) -> None:
        try:
            stream = getattr(process, "stdout", None)
            if stream is not None:
                for line in stream:
                    output.put(line)
        except BaseException as exc:  # reader errors become terminal failures
            output.put((_OUTPUT_FAILED, exc))
        finally:
            output.put(_OUTPUT_DONE)

    def _read_process(self, handle: _ManagedProviderProcess) -> None:
        output: queue.Queue[Any] = queue.Queue()
        pump = threading.Thread(
            target=self._pump_output,
            args=(handle.process, output),
            name=f"provider-output-{handle.session_id}",
            daemon=True,
        )
        pump.start()

        chunks: list[str] = []
        reader_error: BaseException | None = None
        output_done = False
        timed_out = False
        killed = False
        killed_confirmed = False
        try:
            while True:
                now = self._monotonic()
                row = self.dal.get_session(handle.session_id)
                if row is None:
                    self._terminate_group(handle.process)
                    return
                if bool(row.get("kill_requested")):
                    killed = True
                    killed_confirmed = self._terminate_group(handle.process)
                    break
                timeout = handle.wall_timeout_seconds
                if timeout is not None and now - handle.started_monotonic >= timeout:
                    timed_out = True
                    self._terminate_group(handle.process)
                    break

                try:
                    item = output.get(timeout=self.poll_interval)
                except queue.Empty:
                    item = None
                if item is _OUTPUT_DONE:
                    output_done = True
                elif isinstance(item, tuple) and len(item) == 2 and item[0] is _OUTPUT_FAILED:
                    reader_error = item[1]
                elif item is not None:
                    text = (
                        item.decode("utf-8", errors="replace")
                        if isinstance(item, bytes)
                        else str(item)
                    )
                    chunks.append(text)
                    handle.stream_events += 1
                    with self._lock:
                        self._stream_counts[handle.session_id] = handle.stream_events
                    self.dal.touch_activity(handle.session_id, utc_now_iso())

                return_code = self._poll(handle.process)
                if output_done and return_code is not None:
                    break

            raw_output = "".join(chunks)
            if killed:
                self._persist_output(handle.session_id, raw_output)
                if not killed_confirmed:
                    # F001 (round-3 repair): _terminate_group could not confirm
                    # the process actually died -- terminalizing to KILLED here
                    # would report a confirmed kill for a process we never
                    # proved was dead. Leave the row non-terminal (kill_requested
                    # stays set) so a later cancel read-back reports
                    # kill_pending / kill_complete=false, fail closed.
                    LOG.warning(
                        "provider kill for %s unverifiable -- leaving row "
                        "non-terminal (fail closed)",
                        handle.session_id,
                    )
                    self._persist_error(
                        handle.session_id, "kill requested; process death unconfirmed"
                    )
                    return
                self._persist_error(handle.session_id, "kill requested")
                self.dal.terminalize_session(
                    handle.session_id,
                    SessionState.KILLED.value,
                    killed_by=self._killed_by(handle.session_id),
                    void_note="provider session killed",
                )
                return
            if timed_out:
                detail = f"provider session timed out after {handle.wall_timeout_seconds:g}s"
                self._persist_output(handle.session_id, raw_output)
                self._persist_error(handle.session_id, detail)
                self.dal.terminalize_session(
                    handle.session_id,
                    SessionState.FAILED.value,
                    killed_by="timeout",
                    void_note=detail,
                )
                return

            return_code = self._wait_returncode(handle.process)
            if reader_error is not None and return_code == 0:
                return_code = 1
            self._finish_process(handle, return_code, raw_output, reader_error)
        except BaseException as exc:  # noqa: BLE001 - always produce a terminal row.
            LOG.exception("provider reader failed for %s", handle.session_id)
            self._terminate_group(handle.process)
            self._persist_error(handle.session_id, f"provider reader failed: {exc}")
            self.dal.terminalize_session(
                handle.session_id,
                SessionState.FAILED.value,
                void_note="provider reader failed",
            )
        finally:
            with self._lock:
                self._processes.pop(handle.session_id, None)

    @staticmethod
    def _poll(process: Any) -> int | None:
        try:
            value = process.poll()
        except (AttributeError, OSError):
            value = getattr(process, "returncode", None)
        return int(value) if value is not None else None

    def _wait_returncode(self, process: Any) -> int:
        current = self._poll(process)
        if current is not None:
            return current
        try:
            value = process.wait(timeout=max(1.0, self.poll_interval * 2))
        except (AttributeError, subprocess.TimeoutExpired, OSError):
            value = self._poll(process)
        return int(value) if value is not None else 1

    def _finish_process(
        self,
        handle: _ManagedProviderProcess,
        return_code: int,
        raw_output: str,
        reader_error: BaseException | None,
    ) -> None:
        session_id = handle.session_id
        wall_ms = int(max(0.0, time.monotonic() - handle.started_monotonic) * 1000)
        if return_code == 0 and reader_error is None:
            try:
                parsed = handle.adapter._parse(raw_output)
                text = str(getattr(parsed, "text", "") or "")
                session_ref = getattr(parsed, "session_ref", None)
                if isinstance(session_ref, str) and session_ref:
                    self.dal.set_session_ref(session_id, session_ref)
                self._record_usage(session_id, parsed, wall_ms)
                self._persist_output(session_id, text)
                self._persist_error(session_id, None)
                if handle.account_id:
                    self._report_limit_outcome(handle.provider, handle.account_id, "ok", "")
                self.dal.terminalize_session(
                    session_id,
                    SessionState.COMPLETED.value,
                    void_note="provider process completed",
                )
                return
            except BaseException as exc:
                return_code = 1
                reader_error = exc

        error_text = (
            f"{type(reader_error).__name__}: {reader_error}"
            if reader_error is not None
            else raw_output.strip()[-2000:] or f"{handle.provider} exited {return_code}"
        )
        self._persist_output(session_id, raw_output)
        self._persist_error(session_id, error_text)
        self._record_wall_only(session_id, wall_ms)
        if handle.account_id:
            from omniagentos.longhaul.limits import classify_limit_text, parse_reset_time

            outcome = classify_limit_text(handle.provider, f"{raw_output}\n{error_text}")
            if outcome is not None:
                reset_at = (
                    parse_reset_time(f"{raw_output}\n{error_text}", utc_now_iso())
                    if outcome == "quota_exhausted"
                    else None
                )
                self._report_limit_outcome(
                    handle.provider,
                    handle.account_id,
                    outcome,
                    error_text,
                    reset_at=reset_at,
                )
        self.dal.terminalize_session(
            session_id,
            SessionState.FAILED.value,
            void_note=f"provider process exited {return_code}",
        )

    def _record_dispatched_effort(
        self,
        *,
        session_id: str,
        board_task_id: str,
        provider: str,
        command: Sequence[str],
        requested_effort: str | None,
    ) -> str | None:
        """Write the argv-derived effort onto session + live attempt rows.

        Best-effort by construction: a telemetry write must never fail a spawn.
        The value is single-sourced from the constructed provider CLI argv
        (see ``argv_reasoning_effort``); the request field is only used for the
        divergence warning when a provider that has a knob disagrees.
        """
        try:
            dispatched = argv_reasoning_effort(command, provider)
            requested_norm = str(requested_effort or "").strip().lower() or None
            if provider_has_effort_knob(provider) and requested_norm != dispatched:
                LOG.warning(
                    "%s provider=%s board_task_id=%s session_id=%s requested=%r dispatched=%r",
                    EFFORT_DIVERGENCE_WARNING,
                    provider,
                    board_task_id,
                    session_id,
                    requested_norm,
                    dispatched,
                )
            if dispatched is not None:
                self.dal.record_session_usage(session_id, effort=dispatched)
                from omniagentos.swarm.dal import SwarmDal

                dal = SwarmDal(self.db_path)
                try:
                    attempt = dal.current_attempt(board_task_id)
                    if attempt is not None:
                        dal.record_attempt_usage(str(attempt["id"]), effort=dispatched)
                finally:
                    closer = getattr(dal, "close", None)
                    if callable(closer):
                        closer()
            return dispatched
        except Exception:  # noqa: BLE001 -- telemetry is never load-bearing
            LOG.debug(
                "could not record dispatched effort for session %s",
                session_id,
                exc_info=True,
            )
            return None

    def _record_usage(self, session_id: str, parsed: Any, wall_ms: int) -> None:
        """Persist provider-reported usage for the effort/cost-to-green dataset.

        Best-effort by construction: telemetry must never be able to fail the
        run that produced it, so every error here is swallowed. Token counts are
        written only when the CLI actually reported them (see
        ``swarm.usage_capture``) -- an estimate recorded here would be
        indistinguishable from a measurement later and would bias the effort
        comparison this data exists to settle.
        """
        try:
            from omniagentos.swarm.usage_capture import extract

            reported = extract(parsed, wall_ms=wall_ms)
            recorder = getattr(self.dal, "record_session_usage", None)
            if not callable(recorder):
                return
            recorder(
                session_id,
                cost_usd=reported.cost_usd,
                input_tokens=reported.input_tokens,
                output_tokens=reported.output_tokens,
                wall_ms=reported.wall_ms,
                usage_source=reported.source,
            )
        except Exception:  # noqa: BLE001 -- telemetry is never load-bearing
            LOG.debug("could not record usage for session %s", session_id, exc_info=True)

    def _record_wall_only(self, session_id: str, wall_ms: int) -> None:
        """Wall-clock for a run that produced no parseable usage (a failure).

        Failed attempts are exactly the ones cost-to-green must not drop: a
        cheap effort level that fails often looks free if its failures cost
        nothing, which is the specific way this dataset could lie.
        """
        try:
            from omniagentos.swarm.usage_capture import SOURCE_NONE

            recorder = getattr(self.dal, "record_session_usage", None)
            if callable(recorder):
                recorder(session_id, wall_ms=wall_ms, usage_source=SOURCE_NONE)
        except Exception:  # noqa: BLE001 -- telemetry is never load-bearing
            LOG.debug("could not record wall time for session %s", session_id, exc_info=True)

    def _report_limit_outcome(
        self,
        provider: str,
        account_id: str,
        outcome: str,
        detail: str,
        *,
        reset_at: str | None = None,
    ) -> None:
        try:
            self._report_outcome(
                provider,
                account_id,
                outcome,
                detail,
                reset_at=reset_at,
                db_path=self.db_path,
            )
        except Exception:  # outcome reporting cannot strand a session nonterminal
            LOG.exception(
                "could not report %s outcome for %s account %s",
                outcome,
                provider,
                account_id,
            )

    def _persist_output(self, session_id: str, text: str | None) -> None:
        # SEAM 2 (agent-output redaction). Everything this class persists as
        # session output is RAW provider stdout -- it never becomes an
        # AgentResult, so the runner's receipt seam cannot cover it. Every
        # output writer in this module funnels through here, so this is where
        # the redaction belongs; a per-call-site scrub was the shape review
        # proved incomplete.
        text = scrub_optional_text(text)
        setter = getattr(self.dal, "set_session_output", None)
        if callable(setter):
            setter(session_id, text)

    def _persist_error(self, session_id: str, text: str | None) -> None:
        # SEAM 2, error half: provider error text is stdout tails and exception
        # strings built from them, so it carries the same credential exposure.
        text = scrub_optional_text(text)
        setter = getattr(self.dal, "set_session_error", None)
        if callable(setter):
            setter(session_id, text)
        elif text:
            recorder = getattr(self.dal, "record_session_error", None)
            if callable(recorder):
                recorder(session_id, text)

    def _killed_by(self, session_id: str) -> str:
        row = self.dal.get_session(session_id) or {}
        return str(row.get("killed_by") or "operator")

    # ------------------------------------------------------------------
    # Kill, await, and restart reconciliation
    # ------------------------------------------------------------------

    def _signal_group(self, process: Any, sig: int) -> None:
        try:
            pgid = self._getpgid(int(process.pid))
            self._killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError, ValueError):
            pass
        try:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (AttributeError, ProcessLookupError, OSError):
            pass

    def _terminate_group(self, process: Any) -> bool:
        """Signal the process group dead and report whether death is confirmed.

        Returns True only when a post-signal ``poll()`` shows the process has
        actually exited. ``_signal_group`` swallows signal-delivery failures
        (``ProcessLookupError``/``PermissionError``/``OSError``) by design --
        those are not evidence the process is dead, so a caller that wants to
        terminalize a row as a confirmed kill must check this return value
        rather than assume the signal landed.
        """
        if self._poll(process) is not None:
            return True
        self._signal_group(process, signal.SIGTERM)
        deadline = self._monotonic() + self.kill_grace
        while self._poll(process) is None and self._monotonic() < deadline:
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))
        if self._poll(process) is None:
            self._signal_group(process, signal.SIGKILL)
            deadline = self._monotonic() + self.kill_grace
            while self._poll(process) is None and self._monotonic() < deadline:
                self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))
        return self._poll(process) is not None

    def _terminate_pgid(self, pgid: int) -> None:
        try:
            self._killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = self._monotonic() + self.kill_grace
        while self._pgid_liveness(pgid) and self._monotonic() < deadline:
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))
        if self._pgid_liveness(pgid):
            try:
                self._killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def await_terminal(self, session_id: str, timeout_seconds: float, poll: float = 0.5) -> Any:
        """Return the orchestrator ExecutorResult contract for this session."""

        from omniagentos.orchestrator.contracts import ExecutorResult

        if timeout_seconds <= 0 or poll <= 0:
            raise ValueError("timeout_seconds and poll must be positive")
        deadline = self._monotonic() + timeout_seconds
        while True:
            row = self.dal.get_session(session_id)
            if row is None:
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=f"session {session_id} is missing from the session store",
                )
            try:
                state = SessionState(str(row.get("state") or ""))
            except ValueError:
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=f"session {session_id} has invalid state {row.get('state')!r}",
                )
            if state in TERMINAL_SESSION_STATES:
                output_text = str(row.get("output_text") or "")
                if state is SessionState.COMPLETED:
                    return ExecutorResult(
                        status="ok",
                        session_id=session_id,
                        output_text=output_text,
                    )
                detail = str(row.get("error") or "").strip()
                suffix = f": {detail}" if detail else ""
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    output_text=output_text,
                    error=f"session {session_id} ended in {state.value}{suffix}",
                )
            now = self._monotonic()
            if now >= deadline:
                # Match orchestrator/executor.py exactly: injected status stores
                # are only expected to accept the session id.
                self.dal.request_kill(session_id)
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=(
                        f"session {session_id} timed out after {timeout_seconds:g}s "
                        f"in state {state.value}; kill requested"
                    ),
                )
            self._sleep(min(poll, deadline - now))

    def reconcile_orphans(self) -> dict[str, str]:
        """Reconcile nonterminal provider rows using pgid AND command identity."""

        reconciled: dict[str, str] = {}
        for row in self.dal.list_sessions(limit=10_000):
            provider = str(row.get("provider") or "")
            if row.get("source") != "bridge" or provider not in SUPPORTED_PROVIDERS:
                continue
            try:
                state = SessionState(str(row.get("state") or ""))
            except ValueError:
                continue
            if state in TERMINAL_SESSION_STATES:
                continue
            session_id = str(row["id"])
            raw_pid = row.get("pid")
            pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else 0
            adapter = self._adapter_resolver(provider)
            if pid > 0 and self._pgid_liveness(pid) and self._command_matches(pid, adapter):
                if bool(row.get("kill_requested")):
                    self._terminate_pgid(pid)
                    self._persist_error(session_id, "kill requested after restart")
                    self.dal.terminalize_session(
                        session_id,
                        SessionState.KILLED.value,
                        killed_by=self._killed_by(session_id),
                        void_note="orphaned provider process killed",
                    )
                    reconciled[session_id] = "killed"
                else:
                    reconciled[session_id] = "live"
                continue

            detail = (
                f"{provider} provider process is dead or pid identity changed; "
                "classified as crashed"
            )
            self._persist_error(session_id, detail)
            self.dal.terminalize_session(
                session_id,
                SessionState.FAILED.value,
                void_note="provider process crashed",
            )
            reconciled[session_id] = "crashed"
        return reconciled

    def _command_matches(self, pid: int, adapter: _Adapter) -> bool:
        command_line = self._command_reader(pid)
        if not command_line:
            return False
        try:
            tokens = shlex.split(command_line)
        except ValueError:
            tokens = command_line.split()
        expected = Path(str(adapter.cli)).name
        return any(Path(token).name == expected for token in tokens)

    # ------------------------------------------------------------------
    # LIVE preflight (never unit-tested)
    # ------------------------------------------------------------------

    def provider_doctor(self, providers: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
        """LIVE CLI/auth smoke check: output, clean exit, and group kill."""

        selected = [value.strip().lower() for value in (providers or sorted(SUPPORTED_PROVIDERS))]
        results: dict[str, dict[str, Any]] = {}
        for provider in selected:
            if provider not in SUPPORTED_PROVIDERS:
                results[provider] = {
                    "ok": False,
                    "error": "unsupported provider",
                    "probe_status": None,
                    "probe_error": None,
                    "outcome": "unavailable",
                }
                continue
            enabled_accounts = self._enabled_accounts(provider)
            accounts: list[str | None] = list(enabled_accounts) if enabled_accounts else [None]
            for account_id in accounts:
                key = f"{provider}:{account_id or 'default'}"
                status: dict[str, Any] = {
                    "provider": provider,
                    "account_id": account_id,
                    "live_check": True,
                    "stream_events": 0,
                    "clean_exit": False,
                    "kill_within_5s": False,
                }
                try:
                    with tempfile.TemporaryDirectory(
                        prefix=f"omni-provider-doctor-{provider}-"
                    ) as working_dir:
                        first = self.spawn(
                            provider,
                            "",
                            "Reply with exactly: ok",
                            working_dir,
                            f"doctor-{provider}",
                            "provider-doctor",
                            0.05,
                            2,
                            "none",
                            account_id,
                            wall_timeout_seconds=60,
                        )
                        first_result = self.await_terminal(first, 65, poll=0.1)
                        status["stream_events"] = self._stream_counts.pop(first, 0)
                        status["probe_status"] = first_result.status
                        probe_error = first_result.error
                        if probe_error is not None:
                            probe_error = scrub_text(str(probe_error))[:500]
                        status["probe_error"] = probe_error
                        status["clean_exit"] = first_result.status == "ok"

                        second = self.spawn(
                            provider,
                            "",
                            "Print `started`, then wait for 30 seconds before exiting.",
                            working_dir,
                            f"doctor-kill-{provider}",
                            "provider-doctor",
                            0.05,
                            2,
                            "none",
                            account_id,
                            wall_timeout_seconds=60,
                        )
                        stream_deadline = self._monotonic() + 2
                        while (
                            self._stream_counts.get(second, 0) < 1
                            and self._monotonic() < stream_deadline
                        ):
                            self._sleep(0.05)
                        killed_at = self._monotonic()
                        self.dal.request_kill(second, killed_by="provider-doctor")
                        self.await_terminal(second, 5, poll=0.05)
                        second_row = self.dal.get_session(second) or {}
                        status["kill_within_5s"] = (
                            second_row.get("state") == SessionState.KILLED.value
                            and self._monotonic() - killed_at <= 5
                        )
                        self._stream_counts.pop(second, None)
                    status["ok"] = bool(
                        status["stream_events"] >= 1
                        and status["clean_exit"]
                        and status["kill_within_5s"]
                    )
                except BaseException as exc:
                    status["ok"] = False
                    status["error"] = f"{type(exc).__name__}: {exc}"
                status["outcome"] = classify_provider_doctor_outcome(provider, status)
                results[key] = status
        return results

    def _enabled_accounts(self, provider: str) -> list[str]:
        try:
            connection = sqlite3.connect(self.db_path)
            rows = connection.execute(
                "SELECT id FROM claude_accounts WHERE provider = ? AND enabled = 1 ORDER BY id",
                (provider,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        return [str(row[0]) for row in rows]


def _iso_to_epoch(iso_str: str | None) -> float | None:
    """Convert ISO 8601 timestamp to Unix epoch seconds.

    Returns None if the input is None or malformed.
    """
    if not iso_str:
        return None
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def is_making_progress(
    session_id: str,
    idle_threshold_seconds: float,
    *,
    dal: SessionsDal | None = None,
    db_path: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Determine if a provider session is making progress or is stalled.

    A session is considered to be making progress if it has emitted output
    within the idle threshold window. This distinguishes SLOW (still emitting,
    but taking a long time) from STALLED (no output within the threshold,
    warranting a kill decision).

    Args:
        session_id: The session ID to check.
        idle_threshold_seconds: The maximum seconds of inactivity before
            classifying as STALLED. Must be positive.
        dal: SessionsDal instance (optional; created if not provided).
        db_path: Database path (optional; uses default if not provided).
        now_iso: Current timestamp in ISO 8601 format (optional; uses utc_now_iso if not provided).

    Returns:
        A dict with keys:
        - session_id: The session ID checked.
        - is_making_progress: True if the session is actively emitting or recently was.
        - status: One of "slow", "stalled", or "unknown".
            - "slow": Session is emitting but within the idle window (keep it alive).
            - "stalled": Session has not emitted within the idle window (kill is justified).
            - "unknown": Session not found or last_activity_at is NULL/unknown.
        - last_activity_seconds_ago: Elapsed seconds since last activity, or None if unknown.
        - idle_threshold_seconds: The threshold passed in (for reference).
        - details: Human-readable explanation of the status.

    How the coordinator should consume this:
        The coordinator should call is_making_progress() periodically as part of its
        timeout/liveness-check loop (e.g., in scheduler.py or a dedicated watchdog).
        For each running session:
        1. Call is_making_progress(session_id, idle_threshold_seconds)
        2. If status=="stalled", kill the session (it has timed out with no progress)
        3. If status=="slow", keep it alive (work is still happening, just slow)
        4. If status=="unknown", handle gracefully (session missing or activity unknown)

        This allows the scheduler to distinguish between genuinely wedged/hung processes
        (which should be killed to free resources and escalate to a costlier tier) and
        slow-but-healthy processes (which should be allowed to continue).
    """
    if idle_threshold_seconds <= 0:
        raise ValueError("idle_threshold_seconds must be positive")

    try:
        if dal is None:
            dal = SessionsDal(db_path or default_db_path())

        now_timestamp = _iso_to_epoch(now_iso or utc_now_iso())
        if now_timestamp is None:
            return {
                "session_id": session_id,
                "is_making_progress": False,
                "status": "unknown",
                "last_activity_seconds_ago": None,
                "idle_threshold_seconds": idle_threshold_seconds,
                "details": "Could not determine current time",
            }

        row = dal.get_session(session_id)
        if row is None:
            return {
                "session_id": session_id,
                "is_making_progress": False,
                "status": "unknown",
                "last_activity_seconds_ago": None,
                "idle_threshold_seconds": idle_threshold_seconds,
                "details": f"Session {session_id} not found in session store",
            }

        last_activity_iso = row.get("last_activity_at")
        last_activity_timestamp = _iso_to_epoch(last_activity_iso)

        if last_activity_timestamp is None:
            return {
                "session_id": session_id,
                "is_making_progress": False,
                "status": "unknown",
                "last_activity_seconds_ago": None,
                "idle_threshold_seconds": idle_threshold_seconds,
                "details": f"Session {session_id} has no recorded activity timestamp",
            }

        seconds_since_activity = now_timestamp - last_activity_timestamp

        # Negative or very small values indicate a clock issue or immediate activity
        # Treat as within threshold (still making progress).
        if seconds_since_activity < 0:
            seconds_since_activity = 0.0

        is_within_threshold = seconds_since_activity <= idle_threshold_seconds

        return {
            "session_id": session_id,
            "is_making_progress": is_within_threshold,
            "status": "slow" if is_within_threshold else "stalled",
            "last_activity_seconds_ago": seconds_since_activity,
            "idle_threshold_seconds": idle_threshold_seconds,
            "details": (
                f"Session {session_id} last active {seconds_since_activity:.1f}s ago; "
                f"threshold is {idle_threshold_seconds:.1f}s; "
                f"status={'slow (keep alive)' if is_within_threshold else 'stalled (kill justified)'}"
            ),
        }
    except Exception as exc:  # noqa: BLE001 -- liveness check must be robust
        LOG.debug("liveness check failed for session %s: %s", session_id, exc, exc_info=True)
        return {
            "session_id": session_id,
            "is_making_progress": False,
            "status": "unknown",
            "last_activity_seconds_ago": None,
            "idle_threshold_seconds": idle_threshold_seconds,
            "details": f"Liveness check failed: {type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Periodic liveness classification (the coordinator's pre-deadline tick).
#
# ``is_making_progress`` answers a binary question against ONE threshold, which
# is all a deadline-time kill decision needs. A periodic pre-deadline tick needs
# a vocabulary instead: it runs while the attempt still has budget left, so its
# job is to SAY WHAT IS HAPPENING, not to decide a kill. ``classify_liveness``
# is a strict refinement of ``is_making_progress`` — same freshness source (the
# per-chunk ``touch_activity`` heartbeat written by ``_read_process``), same
# fail-safe behaviour, plus:
#   * "active" split out of "slow" — activity fresher than
#     LIVENESS_ACTIVE_FRACTION of the idle threshold is not merely "not stalled",
#     it is healthy, and a supervisor wants those distinguished.
#   * "approval_waiting" — a session parked on an approval is NOT idle work; the
#     silence is expected and must never be read as a stall.
# ---------------------------------------------------------------------------

#: Activity fresher than this fraction of the idle threshold classifies as
#: "active" rather than "slow". Everything at or past it (but within the
#: threshold) is "slow": still alive, but worth watching.
LIVENESS_ACTIVE_FRACTION: float = 0.25

#: The closed classification vocabulary. Anything outside it is a bug; the
#: classifier coerces to "unknown" rather than inventing a status.
LIVENESS_STATUSES: tuple[str, ...] = (
    "active",
    "slow",
    "approval_waiting",
    "stalled",
    "unknown",
)

#: Session state (``sessions.state``) that means "silence is expected".
_AWAITING_APPROVAL_STATE = "awaiting_approval"


def classify_liveness(
    session_id: str,
    idle_threshold_seconds: float,
    *,
    dal: SessionsDal | None = None,
    db_path: str | None = None,
    now_iso: str | None = None,
    session_state: str | None = None,
) -> dict[str, Any]:
    """Classify an in-flight session into :data:`LIVENESS_STATUSES`.

    Args:
        session_id: The session to classify.
        idle_threshold_seconds: Seconds of silence that make a session stalled.
            A non-positive threshold is NOT an error here (unlike
            :func:`is_making_progress`): a periodic observer must never raise
            into a scheduler poll loop, so it returns "unknown" instead.
        dal / db_path / now_iso: as :func:`is_making_progress`.
        session_state: The session's current ``state`` column, when the caller
            already has the row. ``awaiting_approval`` short-circuits to
            "approval_waiting" WITHOUT consulting the activity mark — a parked
            session is silent by design and must not read as stalled.

    Returns:
        The :func:`is_making_progress` dict, with ``status`` narrowed to the
        classification vocabulary. Never raises.
    """
    if str(session_state or "").strip().lower() == _AWAITING_APPROVAL_STATE:
        return {
            "session_id": session_id,
            "is_making_progress": True,
            "status": "approval_waiting",
            "last_activity_seconds_ago": None,
            "idle_threshold_seconds": idle_threshold_seconds,
            "details": f"Session {session_id} is parked awaiting an approval",
        }
    if not idle_threshold_seconds > 0:
        return {
            "session_id": session_id,
            "is_making_progress": False,
            "status": "unknown",
            "last_activity_seconds_ago": None,
            "idle_threshold_seconds": idle_threshold_seconds,
            "details": "Non-positive idle threshold — liveness is not measurable",
        }
    try:
        result = dict(
            is_making_progress(
                session_id,
                idle_threshold_seconds,
                dal=dal,
                db_path=db_path,
                now_iso=now_iso,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- observation must never raise into a poll loop
        LOG.debug("liveness classification failed for %s: %s", session_id, exc, exc_info=True)
        return {
            "session_id": session_id,
            "is_making_progress": False,
            "status": "unknown",
            "last_activity_seconds_ago": None,
            "idle_threshold_seconds": idle_threshold_seconds,
            "details": f"Liveness classification failed: {type(exc).__name__}: {exc}",
        }
    status = str(result.get("status") or "unknown")
    if status == "slow":
        elapsed = result.get("last_activity_seconds_ago")
        if (
            isinstance(elapsed, int | float)
            and float(elapsed) < idle_threshold_seconds * LIVENESS_ACTIVE_FRACTION
        ):
            status = "active"
    if status not in LIVENESS_STATUSES:
        status = "unknown"
    result["status"] = status
    return result


__all__ = [
    "DENIED_RISK_CLASSES",
    "LIVENESS_ACTIVE_FRACTION",
    "LIVENESS_STATUSES",
    "PROVIDER_CONFIG_ENV",
    "SUPPORTED_PROVIDERS",
    "ProviderExecPolicyError",
    "ProviderExecSpawnError",
    "ProviderSessionRunner",
    "classify_liveness",
    "is_making_progress",
]
