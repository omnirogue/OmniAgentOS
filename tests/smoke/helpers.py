"""Shared helpers for process-spawning smoke tests and the e2e script.

All helpers assume a tmp workspace pointed at via OMNIAGENTOS_* env vars and
never touch the repository's real var/ledger/vault trees.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Polling defaults for every state wait in this package.
POLL_START_S = 0.1
POLL_MAX_S = 5.0
DEFAULT_TIMEOUT_S = 60.0

TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})


class SmokeError(RuntimeError):
    """Raised when a smoke assertion or bootstrap step fails."""


def poll_until(
    predicate: Callable[[], Any],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    start_s: float = POLL_START_S,
    max_s: float = POLL_MAX_S,
    desc: str = "condition",
) -> Any:
    """Poll *predicate* with exponential backoff until truthy or timeout.

    Returns the truthy value from the predicate. Never uses a fixed sleep-only
    wait — delay grows from *start_s* up to *max_s*.
    """
    deadline = time.monotonic() + timeout_s
    delay = start_s
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(delay)
        delay = min(delay * 1.5, max_s)
    raise SmokeError(f"Timed out after {timeout_s:.1f}s waiting for {desc}; last={last!r}")


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def repo_root() -> Path:
    """Worktree root (parent of tests/ or scripts/)."""
    here = Path(__file__).resolve()
    # tests/smoke/helpers.py → parents[2] == worktree root
    return here.parents[2]


def apply_workspace_env(workspace: Path) -> dict[str, str]:
    """Build env mapping for a fully isolated tmp workspace."""
    db_path = workspace / "db" / "omniagentos.db"
    ledger_dir = workspace / "ledger"
    vault_dir = workspace / "vault"
    var_dir = workspace / "var"
    workspace_dir = workspace / "runs"  # trusted per-run workspace base (council R2 governance)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (var_dir / "smoke").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["OMNIAGENTOS_DB"] = str(db_path)
    env["OMNIAGENTOS_LEDGER_DIR"] = str(ledger_dir)
    env["OMNIAGENTOS_VAULT_DIR"] = str(vault_dir)
    env["OMNIAGENTOS_WORKSPACE_DIR"] = str(workspace_dir)
    env["OMNIAGENTOS_VAULT_AUTOCOMMIT"] = "false"
    # Keep child processes importing the worktree package.
    py_path = str(repo_root())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = py_path if not existing else f"{py_path}{os.pathsep}{existing}"
    return env


def migrate_db(db_path: str | Path) -> int:
    """Apply schema migrations via the frozen omniagentos.db.migrate entry point."""
    try:
        from omniagentos.db.migrate import migrate as migrate_fn
    except ImportError as exc:  # pragma: no cover - integrated system missing
        raise SmokeError(
            "omniagentos.db.migrate is not available; integrated system required"
        ) from exc
    return int(migrate_fn(str(db_path)))


def mock_agent_step(
    name: str = "mock-agent",
    *,
    reply: str = "mock-ok",
    delay_ms: int = 0,
    fail: bool = False,
    usage: Mapping[str, Any] | None = None,
    action_class: str = "sandboxed_creation",
) -> dict[str, Any]:
    """Build an agent step that scripts MockAdapter via params.mock → metadata['mock']."""
    mock: dict[str, Any] = {"reply": reply}
    if delay_ms:
        mock["delay_ms"] = delay_ms
    if fail:
        mock["fail"] = True
    if usage is not None:
        mock["usage"] = dict(usage)
    return {
        "name": name,
        "kind": "agent",
        "action_class": action_class,
        "params": {
            "adapter": "mock",
            "mock": mock,
        },
    }


def approval_gated_effect_step(
    name: str = "gated-effect",
    *,
    effect: str = "noop",
    action_class: str = "irreversible",
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Effect step whose action_class hard-stops for approval.

    Under the AUTO default (AC-policy) only ``irreversible`` requires approval, so
    that is the class used to exercise the parked-approval flow end-to-end."""
    params: dict[str, Any] = {"effect": effect}
    if extra_params:
        params.update(dict(extra_params))
    return {
        "name": name,
        "kind": "effect",
        "action_class": action_class,
        "params": params,
    }


EFFECT_TOOLS = ["file_write", "shell"]
"""A canonical append_file+probe run legitimately writes a file and runs a shell
probe, so its task must grant these tools (runner enforces this since council SEC/
PROD fixes). The smoke tests grant them; a run WITHOUT them is correctly denied."""


def canonical_append_receipt_step(
    run_id: str, *, path: str = "var/smoke/receipt.txt", working_dir: str | None = None
) -> dict[str, Any]:
    """CANONICAL append_file+probe effect step from contracts/statemachine.md (B2).

    working_dir sets the confinement root so the relative receipt path is allowed
    and the probe runs in the workspace (runner path-confines append_file and runs
    the probe as argv in working_dir since council SEC-002/SEC-001 fixes)."""
    line = f"effect-{run_id}"
    params: dict[str, Any] = {
        "effect": "append_file",
        "path": path,
        "line": line,
        "probe": f"grep -q '{line}' {path}",
    }
    if working_dir is not None:
        params["working_dir"] = working_dir
    return {
        "name": "append-receipt",
        "kind": "effect",
        "action_class": "internal_reversible",
        "params": params,
    }


@dataclass
class Workspace:
    """Isolated tmp workspace for one smoke scenario."""

    root: Path
    env: dict[str, str] = field(repr=False)

    @property
    def db_path(self) -> Path:
        return Path(self.env["OMNIAGENTOS_DB"])

    @property
    def ledger_dir(self) -> Path:
        return Path(self.env["OMNIAGENTOS_LEDGER_DIR"])

    @property
    def vault_dir(self) -> Path:
        return Path(self.env["OMNIAGENTOS_VAULT_DIR"])

    @property
    def workspace_dir(self) -> Path:
        """Trusted per-run workspace base; effects are confined to <this>/<run_id>."""
        return Path(self.env["OMNIAGENTOS_WORKSPACE_DIR"])

    @classmethod
    def create(cls, root: Path) -> Workspace:
        env = apply_workspace_env(root)
        migrate_db(env["OMNIAGENTOS_DB"])
        return cls(root=root, env=env)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def run_row(self, run_id: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))

    def task_row(self, task_id: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def steps_for(self, run_id: str) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM steps WHERE run_id = ? ORDER BY seq ASC", (run_id,))

    def approvals_for(self, run_id: str) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM approvals WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        )

    def idem_for(self, run_id: str) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM idempotency WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        )


def start_process(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    log_path: Path | None = None,
) -> subprocess.Popen[str]:
    """Start a child process with workspace env; optionally tee stdout/stderr to a log."""
    stdout: Any
    stderr: Any
    log_fh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("w", encoding="utf-8")
        stdout = log_fh
        stderr = subprocess.STDOUT
    else:
        stdout = subprocess.PIPE
        stderr = subprocess.STDOUT

    proc = subprocess.Popen(
        list(args),
        env=dict(env),
        cwd=str(cwd),
        stdout=stdout,
        stderr=stderr,
        text=True,
        # Own process group so we can kill the whole tree on teardown.
        start_new_session=True,
    )
    # Stash the log handle so callers can close it after kill.
    if log_fh is not None:
        proc._smoke_log_fh = log_fh  # type: ignore[attr-defined]
    return proc


def kill_process(proc: subprocess.Popen[Any] | None, *, force: bool = False) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        _close_proc_log(proc)
        return
    try:
        if force:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    _close_proc_log(proc)


def _close_proc_log(proc: subprocess.Popen[Any]) -> None:
    fh = getattr(proc, "_smoke_log_fh", None)
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass


def kill9(proc: subprocess.Popen[Any]) -> None:
    """Hard-kill the process group (SIGKILL / kill -9 semantics)."""
    kill_process(proc, force=True)


@dataclass
class SystemBundle:
    """API + runner processes bound to one Workspace."""

    workspace: Workspace
    host: str
    port: int
    api: subprocess.Popen[str]
    runner: subprocess.Popen[str]
    base_url: str

    def client(self, timeout: float = 10.0) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        kill_process(self.runner)
        kill_process(self.api)


def start_api(
    workspace: Workspace,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    log_path: Path | None = None,
) -> tuple[subprocess.Popen[str], int]:
    port = port if port is not None else free_port(host)
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "omniagentos.api:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    proc = start_process(args, env=workspace.env, cwd=workspace.root, log_path=log_path)
    return proc, port


def start_runner(
    workspace: Workspace,
    *,
    poll_ms: int = 500,
    once: bool = False,
    worker_id: str = "smoke-w1",
    log_path: Path | None = None,
    extra_args: Sequence[str] = (),
) -> subprocess.Popen[str]:
    args = [
        sys.executable,
        "-m",
        "omniagentos.runner",
        "--worker-id",
        worker_id,
        "--poll-ms",
        str(poll_ms),
    ]
    if once:
        args.append("--once")
    args.extend(extra_args)
    return start_process(args, env=workspace.env, cwd=workspace.root, log_path=log_path)


def wait_http_ok(
    base_url: str,
    *,
    path: str = "/api/health",
    timeout_s: float = 30.0,
    env_hint: str = "",
) -> dict[str, Any]:
    def _probe() -> dict[str, Any] | None:
        try:
            with httpx.Client(base_url=base_url, timeout=2.0) as client:
                resp = client.get(path)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except json.JSONDecodeError:
                        return {"status": "ok", "raw": resp.text}
        except (httpx.HTTPError, OSError):
            return None
        return None

    return poll_until(
        _probe,
        timeout_s=timeout_s,
        desc=f"HTTP {base_url}{path} ready{env_hint}",
    )


def start_system(
    workspace: Workspace,
    *,
    poll_ms: int = 500,
    host: str = "127.0.0.1",
    logs_dir: Path | None = None,
) -> SystemBundle:
    logs = logs_dir or (workspace.root / "logs")
    api, port = start_api(workspace, host=host, log_path=logs / "api.log")
    base = f"http://{host}:{port}"
    try:
        wait_http_ok(base, env_hint=f" (api pid={api.pid})")
    except SmokeError:
        kill_process(api)
        raise
    runner = start_runner(workspace, poll_ms=poll_ms, log_path=logs / "runner.log")
    return SystemBundle(
        workspace=workspace,
        host=host,
        port=port,
        api=api,
        runner=runner,
        base_url=base,
    )


def _raise_for_api(resp: httpx.Response, action: str) -> None:
    if resp.is_success:
        return
    body: Any
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = resp.text
    raise SmokeError(f"{action} failed: HTTP {resp.status_code}: {body}")


def _session_token_headers() -> dict[str, str]:
    """The X-Session-Token the fix6-gated mutating control-plane routes require.

    The smoke server subprocess and this test process share the repo's
    ``var/secrets/sessions-token`` (same ``omniagentos.sessions.token`` module,
    same repo_root), so minting it here matches what the server verifies against
    — the same token the dashboard's server-side proxy injects in production.
    """
    from omniagentos.sessions import token

    return {"X-Session-Token": token.load_or_create_token()}


def create_task(
    client: httpx.Client,
    *,
    title: str,
    discipline_id: str = "research-briefs",
    input_payload: Mapping[str, Any] | None = None,
    tools_allowed: Sequence[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"title": title, "discipline_id": discipline_id}
    if input_payload is not None:
        body["input"] = dict(input_payload)
    if tools_allowed is not None:
        body["tools_allowed"] = list(tools_allowed)
    resp = client.post("/api/tasks", json=body, headers=_session_token_headers())
    _raise_for_api(resp, "POST /api/tasks")
    return resp.json()


def create_run(
    client: httpx.Client,
    task_id: str,
    *,
    harness: str = "mock",
    plan: Sequence[Mapping[str, Any]] | None = None,
    budget: Mapping[str, Any] | None = None,
    prompt: str | None = None,
    arm: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"harness": harness}
    if plan is not None:
        body["plan"] = list(plan)
    if budget is not None:
        body["budget"] = dict(budget)
    if prompt is not None:
        body["prompt"] = prompt
    if arm is not None:
        body["arm"] = arm
    if model is not None:
        body["model"] = model
    resp = client.post(f"/api/tasks/{task_id}/runs", json=body, headers=_session_token_headers())
    _raise_for_api(resp, f"POST /api/tasks/{task_id}/runs")
    return resp.json()


def get_run(client: httpx.Client, run_id: str) -> dict[str, Any]:
    resp = client.get(f"/api/runs/{run_id}")
    _raise_for_api(resp, f"GET /api/runs/{run_id}")
    return resp.json()


def get_task(client: httpx.Client, task_id: str) -> dict[str, Any]:
    resp = client.get(f"/api/tasks/{task_id}")
    _raise_for_api(resp, f"GET /api/tasks/{task_id}")
    return resp.json()


def set_pause(client: httpx.Client, paused: bool, reason: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"paused": paused}
    if reason:
        body["reason"] = reason
    resp = client.put("/api/pause", json=body, headers=_session_token_headers())
    _raise_for_api(resp, "PUT /api/pause")
    return resp.json()


def get_pause(client: httpx.Client) -> dict[str, Any]:
    resp = client.get("/api/pause")
    _raise_for_api(resp, "GET /api/pause")
    return resp.json()


def decide_approval(
    client: httpx.Client,
    approval_id: str,
    *,
    decision: str = "approved",
    note: str | None = None,
    decided_by: str = "smoke",
) -> dict[str, Any]:
    body: dict[str, Any] = {"decision": decision, "decided_by": decided_by}
    if note is not None:
        body["note"] = note
    resp = client.post(
        f"/api/approvals/{approval_id}/decision", json=body, headers=_session_token_headers()
    )
    _raise_for_api(resp, f"POST /api/approvals/{approval_id}/decision")
    return resp.json()


def cancel_run(client: httpx.Client, run_id: str) -> dict[str, Any]:
    resp = client.post(f"/api/runs/{run_id}/cancel", json={}, headers=_session_token_headers())
    _raise_for_api(resp, f"POST /api/runs/{run_id}/cancel")
    return resp.json()


def run_state(payload: Mapping[str, Any]) -> str:
    """Extract run state from either a flat run or nested {run: ...} payload."""
    if "state" in payload and isinstance(payload.get("state"), str):
        return str(payload["state"])
    run = payload.get("run")
    if isinstance(run, Mapping) and "state" in run:
        return str(run["state"])
    raise SmokeError(f"Cannot find run state in payload keys={list(payload)}")


def run_id_of(payload: Mapping[str, Any]) -> str:
    if "id" in payload and str(payload["id"]).startswith("run_"):
        return str(payload["id"])
    if "id" in payload and "task_id" in payload:
        return str(payload["id"])
    run = payload.get("run")
    if isinstance(run, Mapping) and "id" in run:
        return str(run["id"])
    raise SmokeError(f"Cannot find run id in payload keys={list(payload)}")


def task_id_of(payload: Mapping[str, Any]) -> str:
    if "id" in payload:
        return str(payload["id"])
    raise SmokeError(f"Cannot find task id in payload keys={list(payload)}")


def approvals_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("approvals")
    if isinstance(raw, list):
        return [dict(x) for x in raw if isinstance(x, Mapping)]
    return []


def steps_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("steps")
    if isinstance(raw, list):
        return [dict(x) for x in raw if isinstance(x, Mapping)]
    return []


def wait_run_state(
    client: httpx.Client,
    run_id: str,
    states: Iterable[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    wanted = frozenset(states)

    def _probe() -> dict[str, Any] | None:
        try:
            payload = get_run(client, run_id)
        except SmokeError:
            return None
        if run_state(payload) in wanted:
            return payload
        return None

    return poll_until(
        _probe,
        timeout_s=timeout_s,
        desc=f"run {run_id} in {sorted(wanted)}",
    )


def wait_run_terminal(
    client: httpx.Client,
    run_id: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    return wait_run_state(client, run_id, TERMINAL_RUN_STATES, timeout_s=timeout_s)


def wait_db_run_state(
    workspace: Workspace,
    run_id: str,
    states: Iterable[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> sqlite3.Row:
    wanted = frozenset(states)

    def _probe() -> sqlite3.Row | None:
        row = workspace.run_row(run_id)
        if row is not None and row["state"] in wanted:
            return row
        return None

    return poll_until(
        _probe,
        timeout_s=timeout_s,
        desc=f"db run {run_id} in {sorted(wanted)}",
    )


def count_receipt_lines(path: Path, needle: str) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if needle in line)


def read_ledger_manifests(ledger_dir: str | Path, *, run_id: str | None = None) -> list[Any]:
    try:
        from omniagentos.ledger import read_manifests
    except ImportError as exc:  # pragma: no cover
        raise SmokeError("omniagentos.ledger.read_manifests unavailable") from exc
    return list(read_manifests(str(ledger_dir), run_id=run_id))


def find_vault_notes(vault_dir: Path, *needles: str) -> list[Path]:
    """Return vault markdown files that contain every needle."""
    if not vault_dir.exists():
        return []
    hits: list[Path] = []
    for path in vault_dir.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if all(n in text for n in needles):
            hits.append(path)
    return hits


def enqueue_run_via_store(
    workspace: Workspace,
    *,
    plan: Sequence[Mapping[str, Any]],
    title: str = "smoke-store-task",
    harness: str = "mock",
    budget: Mapping[str, Any] | None = None,
    prompt: str = "smoke prompt",
) -> tuple[str, str]:
    """Create task + queued run directly through SqliteStore (no API process).

    Returns (task_id, run_id). IDs come from contracts.new_id().
    """
    try:
        from omniagentos.contracts import new_id, utc_now_iso
        from omniagentos.db.store import SqliteStore
    except ImportError as exc:  # pragma: no cover
        raise SmokeError("SqliteStore/new_id unavailable; integrated system required") from exc

    store = SqliteStore(str(workspace.db_path))
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "research-briefs",
            "title": title,
            "input_json": json.dumps({"prompt": prompt, "tools_allowed": []}),
            "acceptance_json": "{}",
            "state": "ready",
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "research-briefs",
            "arm": None,
            "harness": harness,
            "harness_version": "",
            "env_hash": "",
            "harness_params": "{}",
            "agent": "mock",
            "model": None,
            "attempt": 1,
            "state": "queued",
            "worker_id": None,
            "plan_json": json.dumps(list(plan)),
            "budget_json": json.dumps(dict(budget or {})),
            "cancel_requested": 0,
            "trace_id": run_id,
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    # Task should be QUEUED when a run is enqueued (API does this; Store path
    # mirrors for runner-only smoke tests).
    try:
        store.update_task_state(task_id, "queued", expect=["ready", "draft"])
    except Exception:
        # Some stores may only accept exact expect lists; best-effort.
        store.update_task_state(task_id, "queued")
    return task_id, run_id
