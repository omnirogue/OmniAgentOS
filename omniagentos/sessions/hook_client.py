"""Claude Code hook executable for Session Bridge policy evaluation."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger(__name__)

# Explicit operator/test override. Highest precedence, unchanged behavior.
SESSION_API_URL_ENV = "OMNI_SESSION_API_URL"
# Stamped into every bridge session's environment by the supervisor at spawn
# (``sessions.supervisor.resolved_api_base_url``), derived from the API this
# stack actually runs on. This is the source that makes the hook follow the
# stack instead of a compiled-in port.
STAMPED_API_URL_ENV = "OMNIAGENTOS_API_BASE_URL"
# LAST RESORT ONLY, and it must point at THIS product. It used to be :8484 --
# the SIBLING product's port -- with a comment accurately describing the danger.
# The danger was real: nothing listens on :8484 on this machine, so from
# 2026-07-27 to 2026-07-31 every hook POST that reached this default was
# refused, the exception was swallowed into a `deny`, and supervisor.py:1605
# then did exactly what it is designed to do -- decline to create an approval
# row for a session whose hook API is unreachable. Zero approvals were created
# in four days while sessions parked waiting for a human who was never asked.
# The knowledge was already written down here; only the value was wrong.
# OmniAgentOS serves :8485 (scripts/launch-omniagentos.sh:91,
# OMNIAGENTOS_API_PORT). Reaching this default still means neither env var was set,
# so every use logs a warning naming that risk.
API_BASE_URL = "http://127.0.0.1:8485"
TIMEOUT_SECONDS = 3.0
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})

# One warning per hook process: the hook runs per tool call, and a warning
# stream on stderr would be its own denial of service on the transcript.
_LAST_RESORT_WARNED = False


def _output(
    decision: str,
    reason: str | None = None,
    *,
    hook_event_name: str = "PreToolUse",
    additional_context: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
        }
    }
    if hook_event_name == "PreToolUse":
        payload["hookSpecificOutput"]["permissionDecision"] = decision
    if reason and hook_event_name == "PreToolUse":
        payload["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if additional_context:
        payload["hookSpecificOutput"]["additionalContext"] = additional_context
    print(json.dumps(payload, separators=(",", ":")))


def base_url() -> str:
    """Control-plane base URL for this hook, in strict precedence order.

    1. ``OMNI_SESSION_API_URL`` — explicit override, wins outright (unchanged).
    2. ``OMNIAGENTOS_API_BASE_URL`` — stamped by the supervisor at spawn from
       the API port this stack is actually serving (``OMNIAGENTOS_API_PORT``).
    3. ``API_BASE_URL`` (:8485) — last resort, WARNS. It now points at THIS
       product rather than the sibling's :8484, but reaching it still means the
       supervisor never stamped a URL, so the port is a guess that may not match
       the stack actually serving. History for why this warns loudly: while the
       constant read :8484, every request that reached it was refused, swallowed
       into a ``deny``, and produced zero approval rows for four days
       (2026-07-27 to 2026-07-31) while sessions parked for a human who was
       never asked. Bench run swr_8474e958870543388267 hung 47 minutes the same
       way.
    """
    global _LAST_RESORT_WARNED
    for name in (SESSION_API_URL_ENV, STAMPED_API_URL_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    if not _LAST_RESORT_WARNED:
        _LAST_RESORT_WARNED = True
        LOG.warning(
            "session hook falling back to the compiled-in control-plane URL %s: "
            "neither %s nor %s is set, so this port is a GUESS at the stack that is "
            "actually serving. If nothing is listening there, this POST (approval "
            "requests included) is refused and swallowed into a deny — and an "
            "undelivered approval parks the session with no approval row created.",
            API_BASE_URL,
            SESSION_API_URL_ENV,
            STAMPED_API_URL_ENV,
        )
    return API_BASE_URL


def _api_url(path: str) -> str:
    return f"{base_url()}{path}"


def _post(
    path: str, payload: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """POST to the local control-plane API.

    ``headers`` defaults to the full control-plane session token (unchanged
    behavior for the report-only ingest call below); the hook-eval call in
    ``main()`` passes its own session-scoped credential when one is available
    (AC-policy hook-auth -- see ``_hook_eval_headers``)."""
    from urllib.request import Request, urlopen

    if headers is None:
        from omniagentos.sessions.token import load_or_create_token

        headers = {"X-Session-Token": load_or_create_token()}
    request = Request(
        _api_url(path),
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = json.loads(response.read())
    if not isinstance(data, dict):
        raise ValueError("session API response was not an object")
    return data


def _hook_eval_headers() -> dict[str, str]:
    """Credential for THIS call to hook-eval (AC-policy hook-auth).

    Prefers this session's narrowly-scoped hook-eval credential
    (``OMNIAGENTOS_BRIDGE_SESSION_ID`` + ``sessions.hook_token``): it is readable
    from inside the OS sandbox that confines a bridge session's Bash/Read tools
    AND this hook subprocess (they share the same confinement), whereas the full
    control-plane token is NOT -- ``var/secrets`` is denied there on purpose (it
    authorizes every OTHER mutating route; a sandboxed agent must never read it).
    Falls back to the full token for report-only / manually-installed hooks,
    which run completely unsandboxed and can read it directly."""
    session_id = os.environ.get("OMNIAGENTOS_BRIDGE_SESSION_ID")
    if session_id:
        try:
            from omniagentos.sessions.hook_token import read_hook_token

            scoped = read_hook_token(session_id)
        except Exception:  # noqa: BLE001 -- any failure here just falls through below.
            scoped = None
        if scoped:
            return {"X-Session-Hook-Token": scoped}
    from omniagentos.sessions.token import load_or_create_token

    return {"X-Session-Token": load_or_create_token()}


def _report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(payload.get("session_id", "")),
        "cwd": str(payload.get("cwd", "")),
        "hook_event_name": payload.get("hook_event_name"),
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input")
        if isinstance(payload.get("tool_input"), dict)
        else {},
    }


def _send_report(payload: dict[str, Any]) -> None:
    try:
        _post("/api/sessions/ingest", _report_payload(payload))
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def _is_read_only(payload: dict[str, Any]) -> bool:
    return str(payload.get("tool_name", "")) in READ_ONLY_TOOLS


def _reads_secret(payload: dict[str, Any]) -> bool:
    """True when a read-only tool call targets a credential store (AC-policy fix4).

    Mirrors the classifier's secret check LOCALLY (same shared registry/resolver)
    so a native Read/Grep/Glob/LS/NotebookRead of ~/.ssh/id_rsa, connections.env,
    ~/.aws/credentials, etc. is NOT fast-path allowed here -- it falls through to
    the full policy eval, which classifies it IRREVERSIBLE and parks it. Fails
    CLOSED: any doubt routes to the full gate rather than a blanket allow."""
    try:
        # Import the lightweight registry directly: importing through the
        # ``policy`` package would initialize Pydantic/YAML on every read hook.
        from omniagentos.secret_registry import tool_input_references_secret

        tool_input = payload.get("tool_input")
        cwd = str(payload.get("cwd", "")) or None
        return tool_input_references_secret(tool_input if isinstance(tool_input, dict) else {}, cwd)
    except Exception:  # noqa: BLE001 -- unsure -> do not short-circuit to allow.
        return True


def _maybe_capture_pretool(payload: dict[str, Any]) -> None:
    """Best-effort PreToolUse experience capture (flag-gated, fail-open)."""
    try:
        from omniagentos.sessions.lifecycle_capture import capture_pretool_use

        capture_pretool_use(payload)
    except Exception:  # noqa: BLE001 — capture must never affect the gate
        pass


def main() -> None:
    """Read one Claude hook event and render policy or steering context."""
    try:
        parsed = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        _output("deny", "api-error")
        return
    if not isinstance(parsed, dict):
        _output("deny", "api-error")
        return

    hook_event_name = str(parsed.get("hook_event_name") or "PreToolUse")
    if hook_event_name == "PreToolUse":
        # Capture before any allow/deny path so experience memory sees the attempt.
        _maybe_capture_pretool(parsed)

    # Report mode is decided BEFORE any per-event dispatch below. It used to be
    # decided after the PostToolUse branch, which silently emptied the mode: the
    # report-only installer registers exactly PostToolUse + Stop
    # (``sessions.install.install``), so the one per-tool event it owns returned
    # from the steering branch and never reached ``_send_report``. Worse, the
    # branch it fell into WROTE to the observed session -- chain-read state on
    # disk and an ``additionalContext`` injection -- in a mode whose whole
    # contract is to watch a foreign CLI and touch nothing. Steering, chain-read
    # and policy eval belong to bridge sessions, which cannot reach this branch
    # at all: ``supervisor.build_clean_env`` is an allow-list and drops
    # OMNI_SESSION_HOOK_MODE from the child environment.
    if os.environ.get("OMNI_SESSION_HOOK_MODE") == "report":
        if not _is_read_only(parsed):
            import threading

            threading.Thread(target=_send_report, args=(parsed,), daemon=True).start()
        # Answer as the event we were actually called for. Reporting a Stop with
        # a PreToolUse-shaped permissionDecision described a gate this mode does
        # not run.
        _output("allow", hook_event_name=hook_event_name)
        return

    if hook_event_name == "PostToolUse":
        from omniagentos.sessions.steering_marker import steering_pending

        marker_session_id = os.environ.get("OMNIAGENTOS_BRIDGE_SESSION_ID") or str(
            parsed.get("session_id", "")
        )
        # T6.4 chain-read: accumulate relevant Read/Grep/Glob/LS paths (local,
        # cheap; never blocks the completed tool on network).
        chain_ctx = None
        try:
            from omniagentos.sessions.chain_read import record_post_read

            tool_name = str(parsed.get("tool_name", ""))
            raw_tool_input = parsed.get("tool_input")
            tool_input: dict[str, Any] = raw_tool_input if isinstance(raw_tool_input, dict) else {}
            chain_ctx = record_post_read(marker_session_id, tool_name, tool_input)
        except Exception:  # noqa: BLE001 — chain-read must never fail the hook
            chain_ctx = None
        # A steer landing between this stat and a skipped POST waits at most one
        # tool call. This small race is accepted because steering is also durable.
        if not steering_pending(marker_session_id):
            _output(
                "allow",
                hook_event_name="PostToolUse",
                additional_context=chain_ctx,
            )
            return
        try:
            response = _post(
                "/api/sessions/hook-eval",
                {
                    "eval_kind": "steering",
                    "session_id": str(parsed.get("session_id", "")),
                    "tool_name": str(parsed.get("tool_name", "")),
                    "tool_input": (
                        parsed.get("tool_input")
                        if isinstance(parsed.get("tool_input"), dict)
                        else {}
                    ),
                    "cwd": str(parsed.get("cwd", "")),
                },
                _hook_eval_headers(),
            )
        except (OSError, ValueError, json.JSONDecodeError):
            # PostToolUse steering is acceleration only.  Never turn a completed
            # tool call into a failure; pending guidance remains durable for the
            # next spawn prompt.
            _output(
                "allow",
                hook_event_name="PostToolUse",
                additional_context=chain_ctx,
            )
            return
        context = response.get("additional_context")
        parts = [p for p in (chain_ctx, str(context) if context else None) if p]
        _output(
            "allow",
            hook_event_name="PostToolUse",
            additional_context="\n\n".join(parts) if parts else None,
        )
        return

    # Fast-path allow ONLY for a read-only tool that is NOT reading a secret. A
    # secret read falls through to the full policy eval below (which hard-stops and
    # parks it), so the blanket read short-circuit can no longer exfiltrate a
    # credential store via the native Read/Grep tools (AC-policy fix4 BLOCKER 1).
    if _is_read_only(parsed) and not _reads_secret(parsed):
        _output("allow")
        return
    try:
        response = _post(
            "/api/sessions/hook-eval",
            {
                "session_id": str(parsed.get("session_id", "")),
                "tool_name": str(parsed.get("tool_name", "")),
                "tool_input": parsed.get("tool_input")
                if isinstance(parsed.get("tool_input"), dict)
                else {},
                "cwd": str(parsed.get("cwd", "")),
            },
            _hook_eval_headers(),
        )
    except (OSError, ValueError, json.JSONDecodeError):
        _output("deny", "api-error")
        return

    if response.get("decision") == "allow":
        _output("allow")
        return
    reason = response.get("reason")
    _output("deny", str(reason) if reason else "api-error")


if __name__ == "__main__":
    main()
