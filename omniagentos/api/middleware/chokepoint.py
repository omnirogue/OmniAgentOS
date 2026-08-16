"""Global request chokepoint middleware: observation plus spend-breaker enforcement.

Structural / body-depth checks remain OBSERVE-ONLY: the middleware records
request metrics and would-be policy violations to a JSONL ledger but does not
reject requests solely because body depth or array length would exceed the
proposed caps (those still feed offline policy analysis).

Spend circuit breaker is LIVE: when ``spend-breaker-state.json`` reports
``tripped: true`` (written by the Steward alert monitor on a confirmed spend
spike), this middleware rejects state-changing requests (POST/PUT/PATCH/DELETE)
with HTTP 503. GET/HEAD/OPTIONS pass through so dashboards and health checks
keep working during a pause. Fail-safe: if the breaker state file exists but
cannot be read or parsed, state-changing requests are refused rather than
allowed through. A missing state file (fresh install / never tripped) is not
an error — requests pass.

Un-trip / reset: a real, AUTHENTICATED route exists at
``POST /api/system/spend-breaker/reset`` (omniagentos/api/routes/system.py),
gated by the SAME session-token check every other mutating route on this app
uses. This middleware's ONLY role in that flow is to exempt that one exact
path from the 503 block below WHILE TRIPPED so the request can reach routing
at all -- it does NOT itself clear anything and does NOT skip auth. Earlier
revisions of this file special-cased the reset path entirely INSIDE the
middleware (clearing state and returning 200 before ``call_next`` ever ran),
which bypassed ``require_session_token`` since Starlette middleware runs
outside route dependencies -- a tokenless caller could self-clear the exact
breaker meant to contain a runaway agent. That is why the exemption below is
narrowed to "do not 503 this one path" rather than "handle this path here".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from omniagentos import runtime_paths, simgate

LOG = logging.getLogger(__name__)

# Structural caps for observation (not enforcement)
_MAX_BODY_DEPTH = 20
_MAX_ARRAY_LENGTH = 1000

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# F2: the ONE path this middleware exempts from the tripped-503 block so it can
# reach the real, authenticated reset route in api/routes/system.py. Exempting
# a path here means "do not synthesize a 503 for it" -- it still goes through
# the app's normal require_session_token gate on POST like every other
# mutating route; this middleware never authenticates or authorizes anything.
_BREAKER_RESET_PATH = "/api/system/spend-breaker/reset"


def _compute_structural_depth(obj: object, max_depth: int = _MAX_BODY_DEPTH) -> tuple[int, bool]:
    """Compute structural depth of JSON object.

    Returns (depth, exceeded) where exceeded=True if depth > max_depth.
    Stops traversal if depth is exceeded to avoid excessive computation.
    """
    def traverse(o: object, current_depth: int) -> int:
        if current_depth > max_depth:
            return current_depth
        if isinstance(o, dict):
            if not o:
                return current_depth
            return max(traverse(v, current_depth + 1) for v in o.values())
        elif isinstance(o, (list, tuple)):
            if not o:
                return current_depth
            return max(traverse(item, current_depth + 1) for item in o)
        else:
            return current_depth

    depth = traverse(obj, 0)
    return depth, depth > max_depth


def _check_array_lengths(obj: object, max_length: int = _MAX_ARRAY_LENGTH) -> tuple[int, bool]:
    """Check maximum array length in JSON object.

    Returns (max_observed_length, any_exceeded) where any_exceeded=True if any array
    exceeds max_length. Stops checking arrays once one is found to exceed.
    """
    max_observed = 0
    exceeded = False

    def traverse(o: object) -> None:
        nonlocal max_observed, exceeded
        if exceeded:
            return
        if isinstance(o, dict):
            for v in o.values():
                traverse(v)
        elif isinstance(o, (list, tuple)):
            length = len(o)
            if length > max_observed:
                max_observed = length
            if length > max_length:
                exceeded = True
                return
            for item in o:
                traverse(item)

    traverse(obj)
    return max_observed, exceeded


def _analyze_request_body(
    body: bytes, content_type: str | None
) -> dict[str, int | bool | None]:
    """Analyze request body for structural violations.

    Returns a dict with keys:
    - body_depth: int or None if not JSON
    - body_depth_exceeded: bool
    - max_array_length: int or None if not JSON
    - array_length_exceeded: bool
    """
    if not body or not content_type or "application/json" not in content_type:
        return {
            "body_depth": None,
            "body_depth_exceeded": False,
            "max_array_length": None,
            "array_length_exceeded": False,
        }

    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {
            "body_depth": None,
            "body_depth_exceeded": False,
            "max_array_length": None,
            "array_length_exceeded": False,
        }

    depth, depth_exceeded = _compute_structural_depth(obj)
    max_array_len, array_exceeded = _check_array_lengths(obj)

    return {
        "body_depth": depth,
        "body_depth_exceeded": depth_exceeded,
        "max_array_length": max_array_len if max_array_len > 0 else None,
        "array_length_exceeded": array_exceeded,
    }


def _get_ledger_path() -> Path:
    """Get path to the API chokepoint ledger.

    Resolves the ledger path using the runtime VAR root configuration, which
    respects OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR environment variables when set.
    This ensures the ledger is never written inside the package directory and is
    properly isolated under tests or when a campaign root is specified.
    """
    try:
        var_root = runtime_paths.resolve_var_root(
            env_keys=runtime_paths.TOKEN_VAR_ENV_KEYS,
            leaf=("api-chokepoint-ledger.jsonl",),
        )
        return var_root
    except runtime_paths.RuntimePathError:
        # Under simulation mode without a configured var root, refuse to write.
        # Log the error but do not block the request (ledger path only).
        LOG.warning(
            "Cannot resolve ledger path under simulation: OMNIAGENTOS_VAR_DIR or "
            "OMNIAGENTOS_VAR must be set. Observations will not be recorded."
        )
        # Return a path that will never be written to (a dummy path that makes
        # the write attempt fail silently in _record_observation).
        return Path("/dev/null") if __name__ != "__main__" else Path()


def _breaker_state_path() -> Path:
    """Resolve the shared spend-breaker state file (same leaf as the monitor)."""
    return runtime_paths.resolve_var_root(
        env_keys=runtime_paths.TOKEN_VAR_ENV_KEYS,
        leaf=("spend-breaker-state.json",),
    )


def _read_breaker_state() -> dict[str, Any]:
    """Read spend circuit-breaker state; fail closed on unreadable/corrupt state.

    - File absent → ``{"tripped": False}`` (fresh install / never tripped).
    - Path unresolvable, unreadable, invalid JSON, or missing ``tripped`` key →
      ``{"tripped": True, "reason": "breaker_state_unreadable"}`` (fail safe).
    """
    try:
        path = _breaker_state_path()
    except simgate.SimGateError:
        # resolve_var_root resolves the sim context FIRST, so simgate refuses
        # here BEFORE any real breaker state can be located. TWO very different
        # situations reach this branch, and a spend breaker must treat them
        # oppositely (mis-collapsing them is a fail-open hazard):
        #
        #   1. LEGIT SIMULATION, incoherent (OMNIAGENTOS_SIM_MODE == "1" but,
        #      e.g., no campaign set). The app's OWN simgate refuses every route
        #      at routing time, so the breaker must stay TRANSPARENT — not
        #      synthesize a 503 (which would mask the coherent route-level 403)
        #      and not let the error escape (a 500). Report not-tripped so the
        #      request reaches routing and the app-level simgate governs.
        #
        #   2. PRODUCTION MISCONFIG (SIM_MODE set to anything other than "1" or
        #      unset — e.g. "0"/"false"/stray whitespace). simgate fail-closes
        #      LOUDLY on these on purpose. This IS production-reachable (an
        #      operator "disabling" sim by setting 0 instead of unsetting), so
        #      the breaker must FAIL CLOSED (tripped), exactly like the
        #      corrupt/unreadable state below — never fail open on a config error.
        #
        # Distinguish by the exact SIM_MODE token (simgate's own "== '1'" rule);
        # unset/empty never raises, so it never reaches here.
        if os.environ.get("OMNIAGENTOS_SIM_MODE") == "1":
            LOG.warning(
                "Incoherent simulation env (SIM_MODE=1) while resolving "
                "spend-breaker state; leaving the breaker transparent so "
                "route-level simgate governs"
            )
            return {"tripped": False, "reason": "sim_env_incoherent"}
        LOG.warning(
            "OMNIAGENTOS_SIM_MODE is set to an invalid value while resolving "
            "spend-breaker state; treating as tripped (fail-safe)"
        )
        return {"tripped": True, "reason": "sim_env_incoherent"}
    except runtime_paths.RuntimePathError:
        LOG.warning(
            "Cannot resolve spend-breaker state path; treating as tripped (fail-safe)"
        )
        return {"tripped": True, "reason": "breaker_state_unreadable"}

    if not path.exists():
        return {"tripped": False}

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or "tripped" not in data:
            LOG.warning(
                "Spend-breaker state at %s is corrupt (missing tripped key); "
                "treating as tripped (fail-safe)",
                path,
            )
            return {"tripped": True, "reason": "breaker_state_unreadable"}
        return data
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        LOG.warning(
            "Spend-breaker state at %s unreadable (%s); treating as tripped (fail-safe)",
            path,
            exc,
        )
        return {"tripped": True, "reason": "breaker_state_unreadable"}


def clear_breaker_state() -> bool:
    """Idempotently un-trip the breaker (F2 minimal reset mechanism).

    Writes ``{"tripped": False}`` atomically (temp file + os.replace, same
    pattern the monitor uses to trip it) so a concurrent read never observes a
    partial write. Safe to call when already not-tripped or when no state file
    exists yet. Returns True on success, False if the write failed (path
    unresolvable or filesystem error) — a failed clear leaves the fail-safe
    tripped/unreadable state in place rather than silently pretending success.
    """
    try:
        path = _breaker_state_path()
    except runtime_paths.RuntimePathError:
        LOG.warning("Cannot resolve spend-breaker state path; reset failed")
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"tripped": False}, ensure_ascii=False, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".spend-breaker-state.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return True
    except OSError:
        LOG.exception("Failed to clear spend-breaker state at %s", path)
        return False


# Public (no leading underscore): called cross-module from
# api/routes/system.py's AUTHENTICATED reset route. This module never calls
# it itself anymore -- see the F2 note in the class docstring below for why
# the earlier self-clearing-inside-middleware design was an auth bypass.


def _record_observation(observation: Mapping[str, object]) -> None:
    """Write observation to JSONL ledger.

    The observation dict is serialized as a single JSON line in the ledger.
    Failures are logged but never block request processing.

    ``dispatch`` builds the record with ``"timestamp": None`` and defers the
    instant to this function, which is the only place that knows when the row
    was actually appended. An undated row is unusable for the offline policy
    analysis this ledger exists to feed, so stamp it here rather than leaving
    the field the caller reserved permanently null.
    """
    try:
        ledger_path = _get_ledger_path()
        # Ensure parent directory exists (except for /dev/null)
        if ledger_path.parent != Path("/dev/null").parent:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(observation)
        if record.get("timestamp") is None:
            record["timestamp"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with ledger_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        LOG.exception("Failed to write chokepoint ledger: %s", exc)


class ChokePointMiddleware(BaseHTTPMiddleware):
    """Global request chokepoint: observe all requests; enforce spend breaker.

    Structural caps (body depth, array length) stay observe-only. When the
    spend circuit breaker is tripped, mutating methods are refused with 503
    before ``call_next``; safe methods (GET/HEAD/OPTIONS) still pass through.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Observe the request; refuse mutating work when the spend breaker is up.

        Records observable request metadata (path, method, Content-Length header)
        without consuming the request body stream. This preserves the ability of
        downstream handlers to reject oversized bodies before buffering, and
        ensures streaming responses are not broken.
        """
        # Build observation record from headers only (never consume the stream).
        # Structural analysis (body depth, array lengths) requires consuming the
        # body, which breaks pre-buffering size guards and streaming responses.
        # Structural checks remain observe-only; only the spend breaker rejects.
        observation: dict[str, Any] = {
            "timestamp": None,  # Will be recorded on write
            "path": request.url.path,
            "method": request.method,
            "content_length": None,  # Not computed without consuming the body
            "headers_content_length": request.headers.get("content-length"),
            "body_depth": None,
            "body_depth_exceeded": False,
            "max_array_length": None,
            "array_length_exceeded": False,
            "would_reject_under_proposed_policy": False,
            "breaker_tripped": False,
            "rejected": False,
        }

        breaker = _read_breaker_state()
        tripped = bool(breaker.get("tripped"))
        observation["breaker_tripped"] = tripped

        # F2: the ONLY exemption from the 503 block is this exact path -- and
        # even for it, this middleware does NOT authenticate, authorize, or
        # clear anything itself. It just declines to synthesize a 503 here so
        # the request reaches routing, where api/routes/system.py's real
        # reset route runs behind the SAME require_session_token gate every
        # other mutating route on this app already goes through. A tokenless
        # POST to this path is therefore refused 401 by the ROUTE, not waved
        # through by the middleware -- see clear_breaker_state()'s call site.
        is_reset_path = (
            request.url.path == _BREAKER_RESET_PATH and request.method.upper() == "POST"
        )

        if tripped and request.method.upper() in _MUTATING_METHODS and not is_reset_path:
            observation["would_reject_under_proposed_policy"] = True
            observation["rejected"] = True
            observation["breaker_reason"] = breaker.get("reason")
            _record_observation(observation)
            reason = breaker.get("reason") or "spend circuit breaker tripped"
            body = json.dumps(
                {
                    "detail": "Spend circuit breaker is tripped; state-changing "
                    "requests are paused.",
                    "reason": reason,
                },
                ensure_ascii=False,
            )
            return Response(
                content=body,
                status_code=503,
                media_type="application/json",
            )

        _record_observation(observation)

        response = await call_next(request)

        # Ensure streaming responses are not buffered/broken
        if isinstance(response, StreamingResponse):
            return response

        return response
