"""Read-only collectors for the estate's live compute surfaces.

Three independent sources, each returning the same shared envelope::

    {"available": bool, "reason": str | None, ...facts}

Honesty rules, same as ``omniagentos/testobs``:

* absent config, an unreachable server, a missing binary, a non-200/malformed
  response — all report ``available: false`` with a reason. There is no
  fallback zero for a source that could not be read;
* reasons name no filesystem path and no exception class — a caller-facing
  reason is not the place to describe local layout or leak an error type;
* one broken source never breaks another: each collector is called
  independently and a raise inside one never reaches the caller as a 500.

Every collector accepts its I/O as an injectable callable (``fetch`` for the
pool's HTTP call, ``runner`` for the ``gh`` subprocess) so tests exercise the
parsing/validation logic without a live server or the ``gh`` binary.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard

__all__ = ["CONNECTIONS_ENV", "read_local", "read_pool", "read_runners"]

# --------------------------------------------------------------------------- envelope


def _absent(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _ok(**facts: Any) -> dict[str, Any]:
    return {"available": True, "reason": None, **facts}


def _finite_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- pool

POOL_URL = "http://127.0.0.1:8487/v1/status"
_POOL_TIMEOUT_S = 3.0

#: ~/.config/omni/connections.env — plain ``NAME=value`` lines, no exports.
#: Same file and precedence as scripts/ops/wq_offload.py's ``load_token()``;
#: reimplemented here (rather than imported) to keep this read-only package
#: free of a dependency on the dev-scripts tree.
CONNECTIONS_ENV = Path.home() / ".config" / "omni" / "connections.env"

# Exhaustive — every field this reader relays for a machine, verified live
# against the pool's /v1/status response. A field not in this list is never
# invented; a field the server dropped comes back as None, never a 0.
_MACHINE_FIELDS: tuple[str, ...] = (
    "machine_id", "hostname", "os", "labels", "max_concurrent", "in_flight",
    "drain", "last_seen_at", "last_seen_age_s", "ncpu", "perf_cores", "mem_gb",
    "load1", "load5", "mem_free_gb", "done_1h", "done_6h",
)

FetchFn = Callable[[str, dict[str, str], float], bytes]


def _default_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _load_pool_token() -> str:
    """WQ_TOKEN — env var first, else CONNECTIONS_ENV. Empty if neither has one."""
    token = os.environ.get("WQ_TOKEN", "").strip()
    if token:
        return token
    try:
        for line in CONNECTIONS_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :]
            if line.startswith("WQ_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _pool_machine(raw: dict[str, Any]) -> dict[str, Any]:
    out = {field: raw.get(field) for field in _MACHINE_FIELDS}
    load1, ncpu = raw.get("load1"), raw.get("ncpu")
    ratio = None
    if _finite_number(load1) and _finite_number(ncpu) and ncpu > 0:
        ratio = round(load1 / ncpu, 2)
    out["load_ratio"] = ratio
    return out


def read_pool(*, fetch: FetchFn | None = None, token: str | None = None) -> dict[str, Any]:
    """Pool machines + queue depth/capacity/refusals, from ``GET /v1/status``."""
    resolved_token = token if token is not None else _load_pool_token()
    if not resolved_token:
        return _absent("compute pool token is not configured")

    headers = {"Authorization": f"Bearer {resolved_token}", "Accept": "application/json"}
    try:
        raw = (fetch or _default_fetch)(POOL_URL, headers, _POOL_TIMEOUT_S)
    except (OSError, urllib.error.URLError):
        return _absent("compute pool is not reachable right now")

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return _absent("compute pool returned an unreadable response")
    if not isinstance(payload, dict) or not _valid_pool_shape(payload):
        return _absent("compute pool returned an unexpected response shape")

    machines = [_pool_machine(m) for m in payload["machines"]]
    return _ok(
        machines=machines,
        depth=payload.get("depth") or {},
        capacity=payload.get("capacity") or {},
        refusals_24h=payload.get("refusals_24h") or {},
    )


def _valid_pool_shape(payload: dict[str, Any]) -> bool:
    """Minimum honest shape of a real ``/v1/status`` body.

    A 200 with a JSON object body is not the same claim as a healthy pool — a
    ``{"error": "starting"}`` response is a 200 too. ``machines`` must be a
    list of objects (a list of non-objects, like a list of ints, is not "zero
    machines" either — it invalidates the whole shape rather than silently
    vanishing to an empty fleet), and ``depth``/``capacity``/``refusals_24h``,
    if present at all, must be the dicts the rest of this reader assumes.
    """
    machines = payload.get("machines")
    if not isinstance(machines, list) or any(not isinstance(m, dict) for m in machines):
        return False
    return all(
        key not in payload or isinstance(payload[key], dict)
        for key in ("depth", "capacity", "refusals_24h")
    )


# --------------------------------------------------------------------------- runners

_RUNNERS_REPO = "Globex/OmniAgentOS"
_RUNNERS_TIMEOUT_S = 10.0
_GH_CANDIDATES: tuple[str, ...] = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
# Flattens gh's {labels: [{id,name,type},...]} to a plain name array server-side
# (in the --jq filter, not in Python), so the reader only ever validates JSON.
_RUNNERS_JQ = "[.runners[] | {name: .name, labels: [.labels[].name], status: .status, busy: .busy}]"

RunnerFn = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _resolve_gh() -> str | None:
    for candidate in _GH_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("gh")


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=_RUNNERS_TIMEOUT_S, check=False
    )


_RUNNERS_UNAVAILABLE = "runner status unavailable right now"


def read_runners(*, runner: RunnerFn | None = None) -> dict[str, Any]:
    """Self-hosted GH Actions runner status, via ``gh api .../actions/runners``.

    Any failure — no ``gh`` binary, no auth, rate limit, a timeout — answers
    the same generic reason. Which of those it was is an operator's problem to
    diagnose on the box, never a caller-facing detail.
    """
    gh = _resolve_gh()
    if gh is None:
        return _absent(_RUNNERS_UNAVAILABLE)

    cmd = [gh, "api", f"repos/{_RUNNERS_REPO}/actions/runners", "--jq", _RUNNERS_JQ]
    try:
        result = (runner or _default_runner)(cmd)
    except (OSError, subprocess.SubprocessError):
        return _absent(_RUNNERS_UNAVAILABLE)
    if result.returncode != 0:
        return _absent(_RUNNERS_UNAVAILABLE)

    try:
        runners = json.loads(result.stdout)
    except (TypeError, ValueError):
        return _absent(_RUNNERS_UNAVAILABLE)
    if not isinstance(runners, list) or any(not isinstance(item, dict) for item in runners):
        # A malformed listing must refuse, not read as a healthy empty fleet —
        # same favourable-absence class _valid_pool_shape rejects for machines.
        return _absent(_RUNNERS_UNAVAILABLE)
    return _ok(runners=list(runners))


# --------------------------------------------------------------------------- local


def read_local() -> dict[str, Any]:
    """This box's own load — pure stdlib, always available."""
    hostname = socket.gethostname()
    load1, load5, _load15 = os.getloadavg()
    ncpu = os.cpu_count()
    ratio = round(load1 / ncpu, 2) if ncpu else None
    return _ok(hostname=hostname, load1=load1, load5=load5, ncpu=ncpu, load_ratio=ratio)
