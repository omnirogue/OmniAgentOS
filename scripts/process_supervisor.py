"""Fail-closed process supervisor for ``launch-omniagentos.sh``.

Every child starts in its own process group.  Startup succeeds only after both
HTTP health probes pass while all children remain alive.  A timeout, early
exit, later unexpected exit, or signal tears down every process group with a
bounded TERM/KILL sequence.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

_LOCK_SCHEMA = "omniagentos.supervisor-lock.v1"

# These commands are deliberately limited to the receive-only poll CLI.  Keep
# the allow-list here rather than deriving it from connector configuration:
# comms poll sources are loop-local and no send capability is registered here.
_RECEIVE_ONLY_POLLERS: dict[str, tuple[str, ...]] = {
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "slack": ("SLACK_BOT_TOKEN",),
}

#: The dashboard's trusted-hop front door (LS-003 / D-1). Caddy is the ONLY
#: component that injects ``X-Omni-Trusted-Hop``; without it every ``/api/**``
#: request the dashboard serves is refused and the UI is dark. It is therefore
#: a core fleet member (``restart_budget=0``, fail closed), not a poller.
CADDY_CONFIG_RELPATH = Path("configs/dashboard-caddy/Caddyfile")
CADDY_DEFAULT_PORT = 3013


def caddy_port(environment: Mapping[str, object] | None = None) -> int:
    """Resolve the front-door port the operator browses (``OMNIAGENTOS_CADDY_PORT``)."""
    env = os.environ if environment is None else environment
    raw = str(env.get("OMNIAGENTOS_CADDY_PORT", "") or "").strip()
    if not raw:
        return CADDY_DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return CADDY_DEFAULT_PORT
    return port if 1 <= port <= 65535 else CADDY_DEFAULT_PORT


#: How long the front-door check waits for caddy to bind and the dashboard to
#: finish compiling before it reports the boundary as not serving.
FRONT_DOOR_PROBE_TIMEOUT_S = 20.0


def _safe_log_fragment(value: object, limit: int = 120) -> str:
    """Render an untrusted detail fit for an operator's terminal.

    The text here can come straight off a socket — a malformed status line is
    literally attacker-chosen bytes — so newlines, control characters and ANSI
    escapes are flattened before they can forge log lines or repaint a console.
    """
    cleaned = "".join(char if char.isprintable() else " " for char in str(value))
    return " ".join(cleaned.split())[:limit] or "no detail"


def front_door_reason(
    root: Path,
    environment: Mapping[str, object] | None = None,
    *,
    # Resolved in the body, not bound as a default: `http_healthy` is defined
    # below this point, and a default argument is evaluated at import time.
    probe: Callable[[str], bool] | None = None,
    timeout_s: float = 0.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """``None`` when the dashboard's front door is serving, else a named reason.

    `/api/health` through the caddy port traverses the WHOLE chain — caddy
    injects the hop header, `middleware.ts` compares it, the catch-all route
    proxies to FastAPI — so this is the acceptance check for LS-003 and the
    detector for the drift the repair plan names as that fix's residual risk.

    DELIBERATELY NOT A FLEET HEALTH URL. A failure here is scoped to the
    dashboard boundary and never tears the fleet down, because the two outages
    are not comparable: a dark dashboard is an observability outage while
    sessions still complete and work still lands, whereas a fleet that will not
    start is a total one. Trading the second for the first would be a bad deal
    even though the misconfiguration causing it is loud.

    What replaces the fleet-fatal gate is loudness that cannot decay: the
    reason is printed at start AND recomputed live by `status`, so this cannot
    become another defect that survives for weeks because nobody was looking.
    """
    skipped = caddy_skip_reason(root, environment)
    if skipped is not None:
        return f"caddy is not supervised ({skipped}); the dashboard will refuse every /api/** request"

    port = caddy_port(environment)
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = monotonic() + timeout_s
    failure = ""
    #: `None` until an HTTP answer arrives, then the numeric status. This is
    #: the THIRD way to be unhealthy, not a second: 403 is the hop-secret
    #: comparison in `middleware.ts` failing; any OTHER non-2xx/3xx status is
    #: an application/downstream fault (a crash, a missing dependency, an
    #: unhandled route exception) with nothing to do with the hop secret, and
    #: reporting it with the same "grep trusted-hop DENIED" pointer sends the
    #: operator to a log line that a 500 will never write. Collapsing those
    #: two into one bool is the same misattribution-of-an-unknown this lane
    #: already removed once for the squatted-port case, surviving here.
    refused_status: int | None = None

    def answered() -> bool:
        """Total, and it records WHY. A probe that RAISES must become a named
        reason like any other refusal — never an exception escaping a diagnostic.

        This function's whole contract is "returns None or a reason", and the
        caller (`_run`) has an unconditional `finally: stop_all()`, so anything
        thrown here reaches it BEFORE `supervise()` runs and takes the fleet
        down — the precise containment failure this check was rewritten to
        avoid, arriving by a path that leaves no named reason and no log behind.
        `except Exception` is deliberate breadth: the caller may inject `probe`,
        and no failure of an observation may ever outrank the thing observed.

        The default path deliberately does NOT reuse `http_healthy`: that
        function correctly reduces every failure to False, and a bare False
        would make this report "the dashboard is refusing" for a port that is
        merely squatted by some other process — sending an operator to grep for
        `trusted-hop DENIED` lines that will never exist. Which failure it is,
        is the whole product of this lane.
        """
        nonlocal failure, refused_status
        try:
            if probe is not None:
                return bool(probe(url))
            with urllib.request.urlopen(url, timeout=0.5) as response:  # noqa: S310
                if 200 <= response.status < 400:
                    return True
                refused_status = response.status
                failure = f" (HTTP {response.status})"
                return False
        except urllib.error.HTTPError as exc:
            # An HTTP answer, just not a healthy one. Which fault it is
            # depends on the status, decided below — 403 IS the hop failure
            # this lane exists to make visible; anything else is not.
            refused_status = exc.code
            failure = f" (HTTP {exc.code})"
            return False
        except Exception as exc:  # noqa: BLE001 - see docstring
            failure = f" ({type(exc).__name__}: {_safe_log_fragment(exc)})"
            return False

    while True:
        if answered():
            return None
        if monotonic() >= deadline:
            break
        sleep(0.5)

    if refused_status == 403:
        return (
            f"{url} answered{failure}: the dashboard's API surface is refusing. "
            "The dashboard log names which failure it is (grep 'trusted-hop DENIED'); "
            "`launch-omniagentos.sh status` prints the secret's fingerprint to compare against it."
        )
    if refused_status is not None:
        # An HTTP answer that is neither healthy nor a 403: the hop secret
        # comparison in `middleware.ts` never runs for this status, so there
        # is no `trusted-hop DENIED` line to grep for and pointing the
        # operator at one would be the same misattribution as the 403 case
        # — just aimed at the wrong boundary. Name the status so the operator
        # goes to the dashboard's own process log instead.
        return (
            f"{url} answered{failure}: the dashboard's API surface is unhealthy, but not at "
            f"the hop boundary — HTTP {refused_status} is an application/downstream fault (a crash, "
            "a missing dependency, an unhandled route exception), not the trusted-hop secret "
            "comparison. Check the dashboard process log for that status."
        )
    # NOT attributed to the hop boundary: nothing spoke HTTP here, so the
    # dashboard may be entirely healthy behind a port that caddy never bound.
    return (
        f"{url} did not answer{failure}: nothing is serving HTTP on the front-door port. "
        "Check that the caddy child is running and owns that port before suspecting the hop secret."
    )


def caddy_skip_reason(root: Path, environment: Mapping[str, object] | None = None) -> str | None:
    """Why caddy is not supervised, or ``None`` when it is.

    ONE decision, consulted by both :func:`build_process_specs` and the health
    URL list. If those two could disagree the supervisor would either probe a
    door nobody opened or open one nobody checks — and "nobody checks" is how
    LS-003 stayed invisible until a LiveSim run found it.

    A skip is always NAMED on stderr by the caller. A machine with no caddy
    installed must not be silently indistinguishable from a working front door.
    """
    env = os.environ if environment is None else environment
    if str(env.get("OMNIAGENTOS_CADDY_DISABLE", "") or "").strip() == "1":
        return "OMNIAGENTOS_CADDY_DISABLE=1"
    if shutil.which("caddy") is None:
        return "no 'caddy' binary on PATH (brew install caddy)"
    if not (root / CADDY_CONFIG_RELPATH).is_file():
        return f"missing {CADDY_CONFIG_RELPATH}"
    return None


#: A poller may die and come back without taking the fleet down — a provider
#: outage is not an OmniAgentOS failure — but only this many times inside
#: :data:`POLLER_RESTART_WINDOW_S`. Past that it is crash-looping, which IS a
#: failure and escalates through the same fail-closed teardown as any other
#: child. See ``ProcessSpec.restart_budget`` for why this is not a bash loop.
POLLER_RESTART_BUDGET = 5
POLLER_RESTART_WINDOW_S = 300.0


class ChildProcess(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path
    log_path: Path
    #: Restarts this child may be given inside ``restart_window_s`` before the
    #: supervisor treats it as failed. ZERO — the default, and what every core
    #: fleet member keeps — means an exit is immediately fail-closed.
    #:
    #: A non-zero budget exists for children that are legitimately allowed to
    #: die without taking the fleet with them (a receive-only poller whose
    #: provider went away). It is deliberately a BUDGET and not a bash
    #: ``while true``: an unsupervised retry loop never exits, so the exit the
    #: supervisor is watching for never arrives and a permanently broken child
    #: is indistinguishable from a healthy one. Crash-looping past the budget
    #: is a real failure and is escalated like any other.
    restart_budget: int = 0
    restart_window_s: float = 300.0


class SupervisionError(RuntimeError):
    """A child or health boundary failed and coordinated shutdown is required."""


def http_healthy(url: str, timeout: float = 0.5) -> bool:
    """Whether ``url`` answers healthily. NEVER raises.

    The enumerated tuple this used to catch (OSError/URLError/ValueError) misses
    `http.client.HTTPException` — a listener that answers with a malformed
    status line raises `BadStatusLine`, which is neither. That escaped every
    caller: out of `wait_until_healthy` and out of `front_door_reason`, into
    `_run`'s unconditional `finally: supervisor.stop_all()`, tearing the fleet
    down before `supervise()` ever ran. A wrong process squatting a port is an
    ordinary deployment mistake and must read as "not healthy", not as a crash.
    `except Exception` because the answer to "is this URL healthy?" is False for
    every failure mode; BaseException (Ctrl-C, SystemExit) still propagates.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 400
    except Exception:  # noqa: BLE001 - a health probe answers, it does not raise
        return False


def port_available(port: int) -> bool:
    """Probe ownership by binding loopback; false means another listener owns it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def process_group_alive(process_group_id: int) -> bool:
    """Return whether a process group still has at least one live member.

    The group leader can exit before one of its descendants.  Checking only
    ``Popen.poll()`` therefore cannot prove coordinated shutdown.  Signal 0
    targets the whole group without changing it and remains valid after the
    original leader has exited.
    """
    if not isinstance(process_group_id, int) or process_group_id <= 1:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # An extant group that became unsignalable is still live and must keep
        # cleanup in the fail-closed path.
        return True
    except OSError:
        # Unknown probe failures must never be interpreted as proof of exit.
        return True
    return True


class ProcessSupervisor:
    def __init__(
        self,
        specs: list[ProcessSpec],
        health_urls: list[str],
        *,
        health_timeout: float,
        stop_timeout: float,
        poll_interval: float = 0.1,
        popen: Callable[..., ChildProcess] = subprocess.Popen,
        health_probe: Callable[[str], bool] = http_healthy,
        kill_group: Callable[[int, int], None] = os.killpg,
        group_alive: Callable[[int], bool] = process_group_alive,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if health_timeout <= 0 or stop_timeout <= 0 or poll_interval <= 0:
            raise ValueError("supervisor timeouts must be positive")
        self.specs = specs
        self.health_urls = health_urls
        self.health_timeout = health_timeout
        self.stop_timeout = stop_timeout
        self.poll_interval = poll_interval
        self._popen = popen
        self._health_probe = health_probe
        self._kill_group = kill_group
        self._group_alive = group_alive
        self._monotonic = monotonic
        self._sleep = sleep
        self.processes: list[tuple[ProcessSpec, ChildProcess]] = []
        self._log_handles: dict[str, BinaryIO] = {}
        self._restarts: dict[str, list[float]] = {}
        self.restart_log: list[tuple[str, int]] = []
        self.received_signal: int | None = None
        #: Invoked after the child roster changes so the durable record of it
        #: (the pid file) can be rewritten. A restart that leaves a dead pid on
        #: disk makes the runtime record say something untrue.
        self.on_roster_change: Callable[[], None] | None = None

    def request_stop(self, signum: int, _frame: object | None = None) -> None:
        self.received_signal = signum

    def _spawn(self, spec: ProcessSpec) -> ChildProcess:
        log_handle = self._log_handles.get(spec.name)
        if log_handle is None:
            spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = spec.log_path.open("ab", buffering=0)
            self._log_handles[spec.name] = log_handle
        return self._popen(
            list(spec.command),
            cwd=str(spec.cwd),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    def start(self) -> None:
        try:
            for spec in self.specs:
                self.processes.append((spec, self._spawn(spec)))
        except Exception:
            self.stop_all()
            raise

    def _restart_allowed(self, spec: ProcessSpec) -> bool:
        """Whether ``spec`` may be respawned now, inside its rolling budget.

        The window is rolling on purpose: a poller that loses its provider once
        a week forever is healthy supervision working, while one that dies four
        times in five minutes is a broken child and must escalate rather than
        be restarted into the same wall indefinitely.
        """
        if spec.restart_budget <= 0:
            return False
        now = self._monotonic()
        history = [at for at in self._restarts.get(spec.name, []) if now - at < spec.restart_window_s]
        self._restarts[spec.name] = history
        return len(history) < spec.restart_budget

    def _reconcile_children(self) -> None:
        """Observe every child's exit; restart inside budget, escalate outside it.

        This is the whole of supervision: a child that exits is SEEN. A wrapper
        that never exits (``while true; do ...; done``) makes this function
        structurally unable to do its job, which is why the pollers no longer
        have one.
        """
        for index, (spec, process) in enumerate(list(self.processes)):
            returncode = process.poll()
            if returncode is None:
                continue
            if not self._restart_allowed(spec):
                budget = spec.restart_budget
                detail = (
                    f"{spec.name} exited unexpectedly with status {returncode}"
                    if budget <= 0
                    else (
                        f"{spec.name} exited with status {returncode} after exhausting its "
                        f"restart budget ({budget} in {spec.restart_window_s:g}s)"
                    )
                )
                raise SupervisionError(detail)
            self._restarts.setdefault(spec.name, []).append(self._monotonic())
            self.restart_log.append((spec.name, returncode))
            print(
                f"{spec.name} exited with status {returncode}; restarting "
                f"({len(self._restarts[spec.name])}/{spec.restart_budget} "
                f"within {spec.restart_window_s:g}s)",
                file=sys.stderr,
            )
            # The leader is gone, but a descendant it spawned may not be, and
            # once the roster entry is replaced nothing will ever signal that
            # group again. Sweep it before losing the handle.
            try:
                self._kill_group(process.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            self.processes[index] = (spec, self._spawn(spec))
            if self.on_roster_change is not None:
                self.on_roster_change()

    def wait_until_healthy(self) -> None:
        deadline = self._monotonic() + self.health_timeout
        while self._monotonic() < deadline:
            if self.received_signal is not None:
                raise SupervisionError(f"received signal {self.received_signal} during startup")
            self._reconcile_children()
            if all(self._health_probe(url) for url in self.health_urls):
                return
            self._sleep(self.poll_interval)
        raise SupervisionError(f"health checks did not pass within {self.health_timeout:g}s")

    def supervise(self) -> None:
        while self.received_signal is None:
            self._reconcile_children()
            self._sleep(self.poll_interval)

    def stop_all(self) -> None:
        """Terminate and verify every child process group within bounded waits.

        Group existence, not leader ``poll()``, controls escalation.  This is
        essential for wrappers such as npm that can exit before a descendant.
        A failure signaling one group is recorded while cleanup continues for
        every other group; the method then raises instead of reporting a
        partial shutdown as success.
        """
        errors: dict[str, str] = {}

        def group_is_alive(spec: ProcessSpec, process: ChildProcess) -> bool:
            try:
                return self._group_alive(process.pid)
            except Exception as exc:  # noqa: BLE001 - unknown means not proven stopped.
                errors.setdefault(
                    f"probe:{process.pid}",
                    f"{spec.name} process-group probe failed: {type(exc).__name__}: {exc}",
                )
                return True

        def signal_group(spec: ProcessSpec, process: ChildProcess, signum: int) -> None:
            try:
                self._kill_group(process.pid, signum)
            except ProcessLookupError:
                return
            except OSError as exc:
                errors.setdefault(
                    f"signal:{process.pid}:{signum}",
                    f"{spec.name} process-group signal {signum} failed: "
                    f"{type(exc).__name__}: {exc}",
                )

        def reap_exited_leaders() -> None:
            # Popen.poll() performs a non-blocking waitpid.  Reaping leaders
            # prevents a zombie leader from making a now-empty group appear
            # live while descendant membership is checked independently.
            for spec, process in self.processes:
                try:
                    process.poll()
                except Exception as exc:  # noqa: BLE001 - continue every cleanup.
                    errors.setdefault(
                        f"poll:{process.pid}",
                        f"{spec.name} leader poll failed: {type(exc).__name__}: {exc}",
                    )

        for _spec, process in reversed(self.processes):
            signal_group(_spec, process, signal.SIGTERM)

        deadline = self._monotonic() + self.stop_timeout
        while self._monotonic() < deadline:
            reap_exited_leaders()
            if not any(group_is_alive(spec, process) for spec, process in self.processes):
                break
            self._sleep(self.poll_interval)

        survivors = [
            (spec, process) for spec, process in self.processes if group_is_alive(spec, process)
        ]
        for spec, process in reversed(survivors):
            signal_group(spec, process, signal.SIGKILL)

        kill_deadline = self._monotonic() + self.stop_timeout
        while survivors and self._monotonic() < kill_deadline:
            reap_exited_leaders()
            survivors = [
                (spec, process) for spec, process in survivors if group_is_alive(spec, process)
            ]
            if survivors:
                self._sleep(self.poll_interval)

        try:
            for spec, process in self.processes:
                try:
                    process.wait(timeout=self.poll_interval)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    errors.setdefault(
                        f"reap:{process.pid}",
                        f"{spec.name} leader was not reaped: {type(exc).__name__}: {exc}",
                    )
            # A group may disappear during the last bounded sleep or while its
            # leader is reaped. Probe once more before recording survivors.
            survivors = [
                (spec, process) for spec, process in survivors if group_is_alive(spec, process)
            ]
            for spec, process in survivors:
                errors.setdefault(
                    f"survivor:{process.pid}",
                    f"{spec.name} process group {process.pid} survived SIGKILL",
                )
        finally:
            for name, handle in self._log_handles.items():
                try:
                    handle.close()
                except OSError as exc:
                    errors.setdefault(
                        f"log-close:{name}",
                        f"log close failed: {type(exc).__name__}: {exc}",
                    )
            self._log_handles.clear()

        if errors:
            raise SupervisionError("; ".join(errors.values()))


def _write_pid_file(
    path: Path,
    *,
    repository_sha: str,
    api_port: int,
    dashboard_port: int,
    processes: list[tuple[ProcessSpec, ChildProcess]],
) -> None:
    payload = {
        "schema": "omniagentos.supervisor.v1",
        "supervisor_pid": os.getpid(),
        "repository_sha": repository_sha,
        "api_port": api_port,
        "dashboard_port": dashboard_port,
        "started_at": datetime.now(UTC).isoformat(),
        "children": {spec.name: process.pid for spec, process in processes},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _remove_own_pid_file(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("supervisor_pid") == os.getpid():
            path.unlink()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _live_ownership(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != "omniagentos.supervisor.v1":
        return None
    if not _pid_alive(payload.get("supervisor_pid")):
        return None
    children = payload.get("children")
    if (
        not isinstance(children, dict)
        or not children
        or not all(_pid_alive(pid) for pid in children.values())
    ):
        return None
    return payload


def owned_status(path: Path, api_port: int, dashboard_port: int) -> tuple[bool, str]:
    payload = _live_ownership(path)
    if payload is None:
        return False, "no live supervisor ownership record"
    if payload.get("api_port") != api_port or payload.get("dashboard_port") != dashboard_port:
        return False, "ownership file ports do not match requested ports"
    return True, f"owned by supervisor pid {payload['supervisor_pid']}"


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _port(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return value


def _repository_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def acquire_runtime_lock(path: Path) -> bool:
    """Atomically claim one runtime so concurrent starts cannot race the PID file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # The owner may be between O_EXCL creation and its first write.
                # Never unlink an unreadable lock and race an active startup.
                return False
            if not isinstance(payload, dict) or payload.get("schema") != _LOCK_SCHEMA:
                return False
            if _pid_alive(payload.get("supervisor_pid")):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
            continue

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"schema": _LOCK_SCHEMA, "supervisor_pid": os.getpid()},
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
            return True
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
    return False


def release_runtime_lock(path: Path) -> None:
    """Release only the lock owned by this supervisor process."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") == _LOCK_SCHEMA and payload.get("supervisor_pid") == os.getpid():
            path.unlink()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass


def _should_start_poller(
    poller_name: str,
    env: Mapping[str, object] | None = None,
) -> bool:
    """Return whether a receive-only poller has all required env *names*.

    This is intentionally a names-only diagnostic like connectors.doctor: it
    tests membership, never reads, interpolates, or logs credential values.
    ``SLACK_TEAM_ID`` is optional for the Slack poller and therefore does not
    gate its startup.  IMAP is intentionally absent until a canonical named
    mailbox list exists; it must never be guessed from environment variables.
    """
    required_names = _RECEIVE_ONLY_POLLERS.get(poller_name)
    if required_names is None:
        return False
    environment = os.environ if env is None else env
    return all(name in environment for name in required_names)


def _poller_skip_reason(poller_name: str, env: Mapping[str, object]) -> str:
    """Build a credential-name-only explanation for an intentionally skipped poller."""
    required_names = _RECEIVE_ONLY_POLLERS[poller_name]
    missing_names = [name for name in required_names if name not in env]
    return f"missing credential name(s): {', '.join(missing_names)}"


def build_process_specs(
    root: Path,
    launcher: Path,
    runtime_dir: Path,
    *,
    env: Mapping[str, object] | None = None,
) -> list[ProcessSpec]:
    """Build child commands while keeping every log inside the selected runtime.

    Telegram and Slack are launched only when their credential *names* are in
    the inherited launcher environment.  IMAP is deliberately not launched:
    its sources are named mailboxes and no canonical mailbox list exists yet.
    """
    log_dir = runtime_dir / "logs"
    environment = os.environ if env is None else env
    sim_mode = str(environment.get("OMNIAGENTOS_SIM_MODE", ""))
    if sim_mode and sim_mode != "1":
        raise ValueError(f"OMNIAGENTOS_SIM_MODE must be exactly '1' when set; got {sim_mode!r}")
    sim_prefix: tuple[str, ...] = ()
    if sim_mode == "1":
        campaign = str(environment.get("OMNIAGENTOS_SIM_CAMPAIGN", ""))
        if not campaign:
            raise ValueError("OMNIAGENTOS_SIM_CAMPAIGN is required when simulation mode is enabled")
        sim_prefix = ("--simulate", "--campaign", campaign)

    def command(component: str) -> tuple[str, ...]:
        return (str(launcher), *sim_prefix, component)

    specs = [
        ProcessSpec("api", command("api"), root, log_dir / "api.log"),
        ProcessSpec("runner", command("runner"), root, log_dir / "runner.log"),
        ProcessSpec(
            "sessions",
            command("sessions"),
            root,
            log_dir / "sessions.log",
        ),
        ProcessSpec(
            "dashboard",
            command("dashboard"),
            root,
            log_dir / "dashboard.log",
        ),
    ]
    caddy_skipped = caddy_skip_reason(root, environment)
    if caddy_skipped is None:
        specs.append(ProcessSpec("caddy", command("caddy"), root, log_dir / "caddy.log"))
    else:
        print(
            f"dashboard trusted-hop proxy (caddy) skipped ({caddy_skipped}); "
            f"every /api/** request to the dashboard will be refused until it runs",
            file=sys.stderr,
        )
    for poller_name in _RECEIVE_ONLY_POLLERS:
        if _should_start_poller(poller_name, environment):
            specs.append(
                ProcessSpec(
                    f"comms-poll-{poller_name}",
                    command(f"comms-poll-{poller_name}"),
                    root,
                    log_dir / f"comms-poll-{poller_name}.log",
                    restart_budget=POLLER_RESTART_BUDGET,
                    restart_window_s=POLLER_RESTART_WINDOW_S,
                )
            )
        else:
            print(
                f"comms poller {poller_name} skipped ({_poller_skip_reason(poller_name, environment)})",
                file=sys.stderr,
            )
    return specs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "status"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--pid-file", type=Path, required=True)
        sub.add_argument("--api-port", type=_port, required=True)
        sub.add_argument("--dashboard-port", type=_port, required=True)
        if command == "run":
            sub.add_argument("--launcher", type=Path, required=True)
            sub.add_argument("--health-timeout", type=_positive_float, default=30.0)
            sub.add_argument("--stop-timeout", type=_positive_float, default=10.0)
    return parser


def _run(args: Any) -> int:
    live_owner = _live_ownership(args.pid_file)
    if live_owner is not None:
        print(
            "refusing duplicate start: "
            f"supervisor pid {live_owner['supervisor_pid']} already owns this runtime",
            file=sys.stderr,
        )
        return 1
    if not port_available(args.api_port):
        print(f"API port {args.api_port} is already owned by another process", file=sys.stderr)
        return 1
    if not port_available(args.dashboard_port):
        print(
            f"dashboard port {args.dashboard_port} is already owned by another process",
            file=sys.stderr,
        )
        return 1

    specs = build_process_specs(args.root, args.launcher, args.pid_file.parent)
    supervisor = ProcessSupervisor(
        specs,
        [
            f"http://127.0.0.1:{args.api_port}/api/health",
            f"http://127.0.0.1:{args.dashboard_port}/",
        ],
        health_timeout=args.health_timeout,
        stop_timeout=args.stop_timeout,
    )
    signal.signal(signal.SIGINT, supervisor.request_stop)
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, supervisor.request_stop)

    def write_pid_file() -> None:
        _write_pid_file(
            args.pid_file,
            repository_sha=_repository_sha(args.root),
            api_port=args.api_port,
            dashboard_port=args.dashboard_port,
            processes=supervisor.processes,
        )

    try:
        supervisor.start()
        supervisor.wait_until_healthy()
        write_pid_file()
        # From here a restartable child may be replaced, so the record of which
        # pids this supervisor owns has to be rewritten when that happens.
        supervisor.on_roster_change = write_pid_file
        print("OmniAgentOS is healthy; supervising all process groups.")
        # LS-003 front door, checked AFTER the fleet is up and declared healthy
        # so that it cannot gate the start. A refusal here means the dashboard
        # boundary is misconfigured — the fleet keeps running and doing work,
        # and this says exactly what is wrong. `status` recomputes it live, so
        # the warning cannot decay into another silent-for-weeks defect.
        # Belt at the cascade boundary itself, not only inside the callee.
        # `finally: supervisor.stop_all()` below is unconditional, so ANYTHING
        # raised between here and `supervise()` is a total fleet outage caused
        # by a dashboard-boundary observation. The invariant "a front-door
        # problem never gates the fleet" belongs where it can be violated.
        try:
            front_door = front_door_reason(args.root, timeout_s=FRONT_DOOR_PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - never let a diagnostic stop the fleet
            front_door = f"front-door check itself failed ({_safe_log_fragment(exc)})"
        if front_door is None:
            print(f"dashboard front door serving on http://127.0.0.1:{caddy_port()}")
        else:
            print(f"WARNING(trusted-hop): dashboard front door NOT serving — {front_door}")
            print(
                "WARNING(trusted-hop): the rest of the fleet is unaffected and still supervising.",
                file=sys.stderr,
            )
        supervisor.supervise()
        return 128 + (supervisor.received_signal or 0)
    except (OSError, SupervisionError) as exc:
        print(f"coordinated launch failed: {exc}", file=sys.stderr)
        if supervisor.received_signal is not None:
            return 128 + supervisor.received_signal
        return 1
    finally:
        try:
            supervisor.stop_all()
        finally:
            _remove_own_pid_file(args.pid_file)


def main() -> int:
    args = _parser().parse_args()
    if args.api_port == args.dashboard_port:
        print("API and dashboard ports must differ", file=sys.stderr)
        return 2
    if args.command == "status":
        owned, message = owned_status(args.pid_file, args.api_port, args.dashboard_port)
        print(message)
        # Probed live, never read back from the ownership record: a recorded
        # verdict would go stale the moment somebody rotated the secret, and a
        # stale "serving" is exactly the favourable-absence shape this whole
        # change exists to remove. Deliberately does not affect the exit code —
        # the dashboard boundary is not the fleet's liveness.
        try:
            front_door = front_door_reason(args.root)
        except Exception as exc:  # noqa: BLE001 - status must always report something
            front_door = f"front-door check itself failed ({_safe_log_fragment(exc)})"
        if front_door is None:
            print(f"front door: serving on http://127.0.0.1:{caddy_port()}")
        else:
            print(f"front door: NOT SERVING — {front_door}")
        return 0 if owned else 1

    lock_path = args.pid_file.with_name(f".{args.pid_file.name}.lock")
    if not acquire_runtime_lock(lock_path):
        print("refusing duplicate start: runtime supervisor lock is held", file=sys.stderr)
        return 1
    try:
        return _run(args)
    finally:
        release_runtime_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
