"""Local API endpoints used by the Session Bridge hook and dashboard."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query

from omniagentos.api.deps import PolicyDep, StoreDep
from omniagentos.api.models import (
    SessionHookEvalRequest,
    SessionHookEvalResponse,
    SessionIngestRequest,
    SessionMessageRequest,
)
from omniagentos.api.services import ApiError, fail
from omniagentos.contracts import ActionClass, Events, default_db_path, new_id, utc_now_iso
from omniagentos.notifications.service import (
    notify_approval_requested,
    push_group,
    push_subtitle,
)
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.policy import approval_satisfies_gate, evaluate_action

# p1-core owns the conservative classifier + action_hash (single source of truth).
from omniagentos.policy.shell import classify_shell
from omniagentos.sessions.company_map import resolve_company
from omniagentos.sessions.dal import (
    TERMINAL_SESSION_STATES,
    SessionState,
    decode_granted_roots,
)
from omniagentos.sessions.hook_token import verify_hook_token
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.policy_map import action_hash, classify_tool
from omniagentos.sessions.steering_marker import mark_steering_pending
from omniagentos.sessions.token import verify_token
from omniagentos.sessions.transcript import live_transcript_path
from omniagentos.sessions.transcript_delta import read_transcript_delta

# L17 narrow adapter: observation only. Never participates in the gate decision.
from omniagentos.toolplane.session import emit_session_tool_call

# The Claude write-family tools whose single filesystem target the P3 scope
# widening may prove in-bounds. Kept in lock-step with
# ``omniagentos.sessions.policy_map._WRITE_TOOLS`` (the classifier's own set); a
# write tool absent here simply never widens (fail-closed).
_SESSION_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)


_SESSIONS_DAL: Any = None
_SESSIONS_DAL_LOCK = threading.Lock()


def get_sessions_dal() -> Any:
    """Return the process-lifetime SessionsDal for this API process.

    T-OPS-006 / DR-008: a SINGLE long-lived DAL (one SQLite connection with a
    5s activity-write coalesce map) is shared across requests. Per-request fresh
    DALs defeated coalescing (each had an empty throttle map) and multiplied
    SQLite writer contention with the runner. The DAL is internally serialized.
    """
    global _SESSIONS_DAL
    if _SESSIONS_DAL is None:
        with _SESSIONS_DAL_LOCK:
            if _SESSIONS_DAL is None:
                from omniagentos.sessions.dal import (  # type: ignore[import-untyped]
                    SessionsDal,
                )

                _SESSIONS_DAL = SessionsDal(default_db_path())
    return _SESSIONS_DAL


def _authorized(x_session_token: str | None = Header(None, alias="X-Session-Token")) -> None:
    if not verify_token(x_session_token):
        fail(401, "unauthorized", "invalid session token")


def _hook_eval_authorized(
    session: dict[str, Any] | None,
    x_session_token: str | None,
    x_session_hook_token: str | None,
) -> bool:
    """hook-eval-only authorization (AC-policy hook-auth).

    The full control-plane token still works, unchanged. A sandboxed session
    cannot read it (var/secrets is OS-sandbox-denied), so this ALSO accepts the
    exact scoped credential minted for THIS session's own row (see
    sessions.hook_token / SessionSupervisor._launch) -- the one thing a sandboxed
    session's PreToolUse hook CAN read. An unknown session has no row to compare
    a scoped credential against, so it can only ever be authorized via the full
    token -- fail closed, same as every other unauthenticated call.
    """
    if verify_token(x_session_token):
        return True
    if session is None:
        return False
    return verify_hook_token(str(session.get("id") or ""), x_session_hook_token)


def _progress_fields(todos_json: Any, files_json: Any) -> dict[str, Any]:
    """Derive the board's todos / progress / stage / files view from the captured
    JSON columns (migration 034). Malformed JSON degrades to empty, never raises."""
    todos: list[dict[str, str]] = []
    if isinstance(todos_json, str) and todos_json.strip():
        try:
            parsed = json.loads(todos_json)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            for todo in parsed:
                if isinstance(todo, dict) and str(todo.get("content") or "").strip():
                    todos.append(
                        {
                            "content": str(todo["content"]).strip(),
                            "status": str(todo.get("status") or "pending"),
                        }
                    )
    total = len(todos)
    done = sum(1 for todo in todos if todo["status"] == "completed")
    stage = next((todo["content"] for todo in todos if todo["status"] == "in_progress"), None)
    files: list[str] = []
    if isinstance(files_json, str) and files_json.strip():
        try:
            parsed_files = json.loads(files_json)
        except (json.JSONDecodeError, TypeError):
            parsed_files = None
        if isinstance(parsed_files, list):
            files = [str(f) for f in parsed_files if isinstance(f, str) and f.strip()]
    return {
        "todos": todos,
        "progress": {
            "done": done,
            "total": total,
            "pct": round(done * 100 / total) if total else 0,
        },
        "stage": stage,
        "files": files,
    }


def _session_row(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    if "kill_requested" in value and value["kill_requested"] is not None:
        value["kill_requested"] = bool(value["kill_requested"])
    # Surface the live progress captured from the session stream (migration 034) as
    # the board's todos/progress/stage/files, dropping the raw JSON columns.
    value.update(_progress_fields(value.pop("todos_json", None), value.pop("files_json", None)))
    value.setdefault("company_override", None)
    value["company"] = resolve_company(value.get("project_dir"), value["company_override"])
    for key in ("agent_name", "agent_status", "agent_profile", "agent_session_id"):
        value.setdefault(key, None)
    # Agent View itself uses this camel-cased name. Keep the database's
    # snake-cased carrier private while preserving the external-row contract.
    value["sessionId"] = value.pop("agent_session_id")
    return value


def _with_approval_counts(dal: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Attach per-session approval totals (T-CODE-005).

    The dashboard renders approvals_requested/granted/denied per session; without
    these the row always shows 0/0/0. Counts are aggregated from the shared
    approvals table by session_id. Failure is non-fatal (zeros), so a counting
    error never breaks the session list itself."""
    value = _session_row(row)
    counts = {"approvals_requested": 0, "approvals_granted": 0, "approvals_denied": 0}
    session_id = value.get("id")
    if session_id is not None:
        try:
            counts.update(dal.approval_counts(str(session_id)))
        except Exception:
            pass
    value.update(counts)
    return value


def _within(boundary: str, candidate: str) -> bool:
    """True when ``candidate`` equals or is contained by ``boundary`` (SEC-004).

    Both are resolved without requiring existence so a widened client cwd (e.g.
    '/') that does not descend the server-of-record project_dir is rejected."""
    if not boundary or not candidate:
        return False
    try:
        base = Path(boundary).expanduser().resolve(strict=False)
        target = Path(candidate).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return inode_relative_parts_anchored(target, base) is not None


def _expiry(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_proposed_action(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Human-evaluable approval summary (F1 / AUTO-APPROVE 5.1).

    Never the bare tool name alone — operators must see the command or target.
    """
    if not isinstance(tool_input, dict):
        tool_input = {}
    for key in ("command", "path", "file_path", "old_string", "new_string", "url", "query"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            text = val.strip()
            if len(text) > 400:
                text = text[:397] + "..."
            if key == "command":
                return text
            return f"{tool_name} {key}={text}"
    # Fallback: compact JSON of small inputs
    try:
        compact = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        compact = ""
    if compact and compact not in ("{}", "null"):
        if len(compact) > 400:
            compact = compact[:397] + "..."
        return f"{tool_name} {compact}"
    return tool_name


_TERMINAL_REPORTS = frozenset({"Stop", "SessionEnd", "stop", "session_end"})


def _is_terminal_report(hook_event_name: str | None) -> bool:
    return bool(hook_event_name) and str(hook_event_name) in _TERMINAL_REPORTS


@router.get("")
def list_sessions(
    _: Annotated[None, Depends(_authorized)],
    state: str | None = Query(None),
) -> list[dict[str, Any]]:
    del _
    dal = get_sessions_dal()
    return [_with_approval_counts(dal, row) for row in dal.list_sessions(state=state)]


@router.get("/discover")
def discover_external_sessions(
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    """List interactive multi-provider agent CLIs currently running on this host.

    Observational process scan (claude / codex / gemini / grok / kimi / qwen / aider /
    cursor-agent). Does not attach or control foreign processes.
    """
    del _
    from omniagentos.sessions.discover import discovery_enabled, list_discovered_processes

    processes = list_discovered_processes() if discovery_enabled() else []
    return {
        "enabled": discovery_enabled(),
        "count": len(processes),
        "processes": [p.as_dict() for p in processes],
        "providers": sorted({p.provider for p in processes}),
    }


@router.post("/discover/sync")
def sync_discovered_to_board(
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    """Force-discover external agent CLIs and project them onto the Kanban board."""
    del _
    from omniagentos.collab.store import CollabStore
    from omniagentos.sessions.external_board import (
        reset_external_board_throttle,
        sync_external_sessions_to_board,
    )

    dal = get_sessions_dal()
    db = default_db_path()
    collab = CollabStore(db)
    reset_external_board_throttle()
    stats = sync_external_sessions_to_board(
        dal,
        collab,
        force=True,
        db_key=db,
    )
    return {"ok": True, **stats}


@router.get("/{session_id}")
def get_session(session_id: str, _: Annotated[None, Depends(_authorized)]) -> dict[str, Any]:
    del _
    dal = get_sessions_dal()
    session = dal.get_session(session_id)
    if session is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    return _with_approval_counts(dal, session)


_SESSION_TITLE_MAX = 500
_COMPANY_OVERRIDE_MAX = 120


@router.post("/{session_id}/update")
def update_session(
    session_id: str,
    body: dict[str, Any],
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    """Update the operator-controlled title and company assignment of a session."""
    del _
    fields: dict[str, str | None] = {}
    if "title" in body:
        title = body["title"]
        if title is not None and (
            not isinstance(title, str) or len(title) > _SESSION_TITLE_MAX
        ):
            fail(422, "validation_error", "title must be a string up to 500 characters or null")
        fields["title"] = title
    if "company" in body:
        company = body["company"]
        if company is not None and (
            not isinstance(company, str) or len(company) > _COMPANY_OVERRIDE_MAX
        ):
            fail(422, "validation_error", "company must be a string up to 120 characters or null")
        fields["company_override"] = (
            company.strip() or None if isinstance(company, str) else company
        )

    dal = get_sessions_dal()
    if dal.get_session(session_id) is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    if fields and not dal.update_session_details(session_id, **fields):
        fail(409, "conflict", "session update could not be recorded", {"id": session_id})
    updated = dal.get_session(session_id)
    if updated is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    return _with_approval_counts(dal, updated)


# Synthetic-transcript sizing: whole prompts/outputs/workbooks are durable in
# SQLite / on disk; the transcript response only needs a readable excerpt.
_SYNTH_TEXT_LIMIT = 8000


def _clip_text(value: str, limit: int = _SYNTH_TEXT_LIMIT, *, keep_tail: bool = False) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    if keep_tail:
        return "[… truncated …]\n" + text[-limit:]
    return text[:limit] + "\n[… truncated …]"


def _is_provider_session(session: dict[str, Any]) -> bool:
    """True for non-claude provider-exec sessions."""
    provider = str(session.get("provider") or "").strip().lower()
    return bool(provider) and provider != "claude"


def _swarm_attempt_for_session(session_id: str) -> dict[str, Any] | None:
    """The swarm attempt bound to this session, or None (always best-effort)."""
    try:
        from omniagentos.api.routes.swarm import get_swarm_dal

        return get_swarm_dal().attempt_for_session(session_id)
    except Exception:
        return None


def _workbook_for_attempt(attempt: dict[str, Any]) -> tuple[str | None, str | None]:
    """(content, path) of the task's WP5b workbook, or (None, None)."""
    try:
        from omniagentos.swarm.spawn import swarm_workbook_path

        path = swarm_workbook_path(
            str(attempt.get("swarm_run_id") or ""), str(attempt.get("board_task_id") or "")
        )
        return path.read_text(encoding="utf-8"), str(path)
    except (OSError, ValueError):
        return None, None


def _synthesize_provider_transcript(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Transcript-shaped turns reconstructed from a provider session's durable data.

    Provider CLIs (``swarm/provider_exec.py``) never write the claude bridge
    JSONL activity file, so the drawer/task-detail views rendered nothing for
    them. The durable row fields (prompt/error/output_text — migrations
    040/041/047) plus the swarm attempt metadata and the task workbook (WP5b)
    are reshaped into entries ``lib/transcriptParser.ts`` already parses.
    Every synthesized entry carries ``synthetic: True`` so consumers can tell
    reconstruction from a recorded live transcript.
    """
    session_id = str(session.get("id") or "")
    created_at = str(session.get("created_at") or "")
    updated_at = str(session.get("updated_at") or created_at)
    state = str(session.get("state") or "")
    entries: list[dict[str, Any]] = []

    prompt = str(session.get("prompt") or "").strip()
    if prompt:
        entries.append(
            {
                "ts": created_at,
                "type": "message",
                "actor": "user",
                "message": _clip_text(prompt),
                "synthetic": True,
            }
        )

    attempt = _swarm_attempt_for_session(session_id)
    if attempt is not None:
        parts = [f"Swarm attempt {attempt.get('seq')}"]
        provider_model = "/".join(
            str(value) for value in (attempt.get("provider"), attempt.get("model")) if value
        )
        if provider_model:
            parts.append(provider_model)
        if attempt.get("tier"):
            parts.append(f"tier {attempt['tier']}")
        parts.append(
            f"end_reason {attempt['end_reason']}" if attempt.get("end_reason") else "attempt live"
        )
        entries.append(
            {
                "ts": str(attempt.get("started_at") or created_at),
                "type": "event",
                "actor": "swarm",
                "summary": " · ".join(parts),
                "attempt": {
                    key: attempt.get(key)
                    for key in (
                        "id",
                        "swarm_run_id",
                        "board_task_id",
                        "seq",
                        "provider",
                        "model",
                        "tier",
                        "end_reason",
                    )
                },
                "synthetic": True,
            }
        )
        workbook, workbook_path = _workbook_for_attempt(attempt)
        if workbook and workbook.strip():
            entries.append(
                {
                    "ts": str(attempt.get("ended_at") or updated_at),
                    "type": "event",
                    "actor": "swarm",
                    # Checkpoints append at the end, so keep the tail on clip.
                    "summary": "Task workbook:\n" + _clip_text(workbook, keep_tail=True),
                    "file_path": workbook_path,
                    "synthetic": True,
                }
            )

    output_text = str(session.get("output_text") or "").strip()
    try:
        terminal = SessionState(state) in TERMINAL_SESSION_STATES
    except ValueError:
        terminal = False
    if state == SessionState.COMPLETED.value:
        entries.append(
            {
                "ts": updated_at,
                "type": "message",
                "actor": "assistant",
                "message": _clip_text(output_text) if output_text else "(no output captured)",
                "synthetic": True,
            }
        )
    elif terminal:
        if output_text:
            entries.append(
                {
                    "ts": updated_at,
                    "type": "message",
                    "actor": "assistant",
                    "message": _clip_text(output_text),
                    "synthetic": True,
                }
            )
        error = str(session.get("error") or "").strip() or f"session ended {state}"
        entries.append(
            {
                "ts": updated_at,
                "type": "event",
                "actor": "system",
                "summary": _clip_text(f"Session {state}: {error}", 2000),
                "synthetic": True,
            }
        )
    else:
        entries.append(
            {
                "ts": str(session.get("last_activity_at") or updated_at),
                "type": "event",
                "actor": "system",
                "summary": "Session is running — provider output arrives at completion.",
                "synthetic": True,
            }
        )
    return entries


@router.get("/{session_id}/transcript")
def get_session_transcript(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),
    _: Annotated[None, Depends(_authorized)] = None,
) -> list[dict[str, Any]]:
    """Return the newest valid JSONL activity records for one session.

    Preference order (real recorded activity always beats reconstruction):

    1. LIVE CLI transcript (stream-json JSONL under the account's config_dir,
       resolved via :mod:`omniagentos.sessions.transcript`) — claude-bridge
       sessions only; provider sessions have no such path.
    2. Ledger / SessionManifest activity file (terminal audit record, or any
       real records written there) — kept verbatim when present.
    3. Provider synthesis from the durable session row + swarm attempt +
       workbook (entries marked ``synthetic: True``) — only when no real
       activity file yielded anything.
    """
    del _
    dal = get_sessions_dal()
    session = dal.get_session(session_id)
    if session is None:
        fail(404, "not_found", "session not found", {"id": session_id})

    def _jsonl_dicts(path: Path | None) -> list[dict[str, Any]]:
        if path is None:
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    account_lookup = getattr(dal, "get_claude_account", None)
    entries = _jsonl_dicts(live_transcript_path(session, account_lookup=account_lookup))
    if not entries:
        # Real activity on the ledger beats synthesized reconstruction.
        # (Terminal audit manifest, or any recorded JSONL written there.)
        entries = _jsonl_dicts(SessionManifest().path_for(session_id))
    if not entries and _is_provider_session(session):
        entries = _synthesize_provider_transcript(session)
    # Guidance is durable control-plane activity as well as useful operator
    # context, so surface it beside JSONL records while the bridge awaits the
    # next safe turn to consume it.
    for message in dal.list_session_messages(session_id, limit=limit):
        entries.append(
            {
                "ts": message["queued_at"],
                "type": "message",
                "actor": message.get("created_by") or "operator",
                "message": message["message"],
                "applied_at": message.get("applied_at"),
            }
        )
    entries.sort(key=lambda entry: str(entry.get("ts") or ""))
    return entries[-limit:]


@router.get("/{session_id}/transcript/delta")
def get_session_transcript_delta(
    session_id: str,
    offset: int = Query(0, ge=0),
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Return new complete lines/events from the session transcript since `offset`.

    Implements a byte-offset based delta read with complete-lines-only logic
    and a rotation guard that resets the offset to 0 if it exceeds the current
    file size. Reads the LIVE CLI transcript for claude-bridge sessions (the
    ledger manifest only remains for provider sessions, which have no live
    file).
    """
    del _
    dal = get_sessions_dal()
    session = dal.get_session(session_id)
    if session is None:
        fail(404, "not_found", "session not found", {"id": session_id})

    account_lookup = getattr(dal, "get_claude_account", None)
    path = live_transcript_path(session, account_lookup=account_lookup)
    if path is None:
        path = SessionManifest().path_for(session_id)
    # ONE implementation of the byte-offset + rotation-guard protocol lives in
    # omniagentos.sessions.transcript_delta (shared with the chat turn bridge).
    return read_transcript_delta(path, offset)


@router.post("/{session_id}/message")
def send_session_message(
    session_id: str,
    body: SessionMessageRequest,
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    """Queue operator guidance for the Session Bridge to apply on its next turn."""
    del _
    dal = get_sessions_dal()
    session = dal.get_session(session_id)
    if session is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    active_states = {
        "queued",
        "planning",
        "starting",
        "running",
        "awaiting_approval",
        "resuming",
    }
    if str(session.get("state")) not in active_states:
        fail(400, "invalid_state", "cannot steer a terminal session", {"id": session_id})
    queued = dal.enqueue_message(session_id, body.message.strip())
    mark_steering_pending(session_id)
    return {"ok": True, "message_id": queued["id"], "queued_at": queued["queued_at"]}


@router.post("/hook-eval", response_model=SessionHookEvalResponse)
def hook_eval(
    body: SessionHookEvalRequest,
    policy_cfg: PolicyDep,
    x_session_token: str | None = Header(None, alias="X-Session-Token"),
    x_session_hook_token: str | None = Header(None, alias="X-Session-Hook-Token"),
) -> dict[str, Any]:
    """Gate one session tool call, and observe the outcome.

    L17 NARROW ADAPTER (M-08/M-17). The gate decision is unchanged and lives
    entirely in ``_hook_eval_decide``. This wrapper adds exactly one thing:
    every outcome on this real sanctioned session tool path -- allow, deny, and
    the intentional 401 -- emits one scrubbed, durable toolplane observation.
    The observation is metadata-only (no tool input, cwd, or reason text) and is
    best-effort by construction: ``emit_session_tool_call`` never raises, so an
    observation failure can never change or block a gate decision.
    """
    started = time.monotonic()
    try:
        decision = _hook_eval_decide(body, policy_cfg, x_session_token, x_session_hook_token)
    except ApiError:
        emit_session_tool_call(
            tool_name=body.tool_name,
            session_id=body.session_id,
            decision="deny",
            duration_ms=int((time.monotonic() - started) * 1000),
            reason="unauthorized",
            eval_kind=body.eval_kind,
        )
        raise
    emit_session_tool_call(
        tool_name=body.tool_name,
        session_id=body.session_id,
        decision=str(decision.get("decision") or "error"),
        duration_ms=int((time.monotonic() - started) * 1000),
        reason=decision.get("reason"),
        eval_kind=body.eval_kind,
    )
    return decision


def _hook_eval_decide(
    body: SessionHookEvalRequest,
    policy_cfg: Any,
    x_session_token: str | None,
    x_session_hook_token: str | None,
) -> dict[str, Any]:
    """Classify a tool call, creating a shared approval when policy requires it.

    Authorization is intentionally NOT the shared ``_authorized``/app-level gate
    (AC-policy hook-auth): this route is listed in ``api.main._PUBLIC_MUTATION_PATHS``
    and evaluates its own credential instead, because a sandboxed bridge session's
    PreToolUse hook cannot read the control-plane session token but CAN read its own
    scoped one. See ``_hook_eval_authorized``.
    """
    try:
        now = utc_now_iso()
        call_hash = action_hash(body.tool_name, body.tool_input)
        dal = get_sessions_dal()

        # SEC-004: the containment boundary for classification is the AUTHORITATIVE
        # project_dir recorded on the session row, never the client-supplied cwd. An
        # unknown session fails closed (deny). If the client cwd does not equal or
        # descend the recorded project_dir it has been widened (e.g. '/'), so the
        # call is an out-of-scope operation and is forced IRREVERSIBLE -- the
        # hard-stop class -- so it cannot ride a widened boundary into unattended
        # auto-execution under AUTO mode (AC-policy).
        session = dal.get_session_by_ref(body.session_id)
        if not _hook_eval_authorized(session, x_session_token, x_session_hook_token):
            fail(401, "unauthorized", "invalid session token")
        if session is None:
            return {"decision": "deny", "action_hash": call_hash, "reason": "unknown-session"}

        # BLOCKER 1(a): steering eval_kind branch AT THE TOP — no policy classification
        if body.eval_kind == "steering":
            canonical_id = str(session.get("id") or body.session_id)
            messages = dal.claim_and_mark_session_messages(canonical_id)
            if not messages:
                return {
                    "decision": "allow",
                    "action_hash": call_hash,
                    "additional_context": None,
                }
            from omniagentos.longhaul.prompts import steering_wrap

            formatted = steering_wrap(messages)
            # Best-effort delivery receipts on the task conversation thread —
            # the guaranteed path is prompt re-injection, so failures here must
            # never break the hook flow.
            try:
                from omniagentos.contracts import default_db_path
                from omniagentos.longhaul.store import LonghaulStore

                lstore = LonghaulStore(default_db_path())
                try:
                    task_ref = lstore.task_id_for_session(canonical_id)
                    if task_ref:
                        # Receipt only the turns whose content was actually
                        # handed over in this claim — blanket-marking would
                        # defeat the finish-refusal net for turns whose live
                        # enqueue failed.
                        claimed = {str(m.get("message") or "") for m in messages}
                        for turn in lstore.pending_steering(task_ref):
                            if str(turn.get("content") or "") in claimed:
                                ref = turn.get("id") or (task_ref, int(turn["seq"]))
                                lstore.mark_turn_delivered(ref, canonical_id)
                finally:
                    lstore.close()
            except Exception:  # pragma: no cover - receipts are advisory
                pass
            return {
                "decision": "allow",
                "action_hash": call_hash,
                "additional_context": formatted if formatted else None,
            }

        # PKG-INSESSION-FANOUT: the Task tool (subagent spawn) is authorized
        # EXCLUSIVELY by a live coordinator grant — never by classification,
        # approvals, or ownership marks. The consume is one atomic UPDATE
        # bounded by the grant budget, its TTL, its void marker, AND the bound
        # attempt still being open, so the scheduler stays the only parallelism
        # authority even for a session whose CLI exposes Task. Fail-CLOSED on
        # every error path.
        if body.tool_name == "Task":
            canonical_session_id = str(session.get("id") or body.session_id)
            try:
                from omniagentos.swarm.insession import (
                    consume_child_slot,
                    insession_enabled,
                )

                if not insession_enabled():
                    return {
                        "decision": "deny",
                        "action_hash": call_hash,
                        "reason": "task-fanout-disabled",
                    }
                allowed, why = consume_child_slot(canonical_session_id)
            except Exception:  # noqa: BLE001 -- a broken gate must deny, never allow.
                logging.getLogger(__name__).exception(
                    "insession Task gate failed for %s", canonical_session_id
                )
                allowed, why = False, "task-gate-error"
            if allowed:
                return {"decision": "allow", "action_hash": call_hash}
            return {
                "decision": "deny",
                "action_hash": call_hash,
                "reason": f"task-fanout:{why}",
            }

        project_dir = str(session.get("project_dir") or "")

        # SEC-006 one-shot authorization: an APPROVED, human-gated, unexpired,
        # not-yet-consumed session approval for THIS session + action_hash
        # authorizes EXACTLY ONE execution. The consume is atomic and single-use,
        # so a second identical call re-classifies and re-parks (no replay). A call
        # whose action_hash matches no approved row takes the normal classify path.
        def _human_gate(row: dict[str, Any]) -> bool:
            try:
                approved_class = ActionClass(str(row.get("action_class")))
            except (TypeError, ValueError):
                return False
            gate = approval_satisfies_gate(
                row,
                evaluate_action(approved_class, policy_cfg),
                actor="session-supervisor",
                now_iso=now,
            )
            return gate.human_ok and not gate.expired

        consumed = dal.consume_authorized_approval(
            session_id=body.session_id,
            action_hash=call_hash,
            now_iso=now,
            gate=_human_gate,
        )
        if consumed is not None:
            return {"decision": "allow", "approval_id": consumed, "action_hash": call_hash}

        session_row_id = str(session.get("id") or body.session_id)
        action_class = classify_tool(body.tool_name, body.tool_input, project_dir, session_row_id)
        # P3 session-scope widening. The session's granted roots are the project's
        # validate_grant_dir-checked root_dirs + allowed_dirs, computed server-side at
        # dispatch and FROZEN on the session row -- read from the row here, NEVER from
        # the hook payload (SEC-004). They relax ONLY the in-project WRITE boundary: a
        # write proven inside a granted root is in-scope (INTERNAL_REVERSIBLE, auto)
        # rather than a hard stop. Delete/money/secret/remote stay exactly as classified
        # -- for Bash we re-run the SAME shared classifier (which checks those BEFORE the
        # write-scope test) with the roots added; for a Write/Edit we only downgrade a
        # single target proven inside a granted root (its classifier branch is pure
        # path-in-scope, and a granted root can never engulf a secret dir). A session
        # with no granted roots takes neither branch -> unchanged.
        granted_roots = decode_granted_roots(session)
        if granted_roots and action_class == ActionClass.IRREVERSIBLE:
            if body.tool_name == "Bash":
                action_class = classify_shell(
                    body.tool_input.get("command"),
                    project_dir,
                    session_row_id,
                    extra_roots=granted_roots,
                )
            elif body.tool_name in _SESSION_WRITE_TOOLS:
                # Reuse the classifier's own write-scope test by treating each granted
                # root as the project: a single in-root target yields INTERNAL_REVERSIBLE.
                for root in granted_roots:
                    if (
                        classify_tool(body.tool_name, body.tool_input, root, session_row_id)
                        == ActionClass.INTERNAL_REVERSIBLE
                    ):
                        action_class = ActionClass.INTERNAL_REVERSIBLE
                        break
        # SEC-004 containment: the client cwd must equal/descend the recorded
        # project_dir OR one of the (server-of-record) granted roots. A wider cwd
        # (e.g. '/') that descends none of them is out-of-scope -> hard stop.
        if not _within(project_dir, body.cwd) and not any(
            _within(root, body.cwd) for root in granted_roots
        ):
            action_class = ActionClass.IRREVERSIBLE
        decision = evaluate_action(action_class, policy_cfg)

        # F1 / AUTO-APPROVE 5.1: proposed_action must name the real command/target.
        tool_input = dict(body.tool_input or {})
        proposed_action = _format_proposed_action(body.tool_name, tool_input)
        if body.tool_name == "Bash" and decision.requires_approval:
            cmd = tool_input.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                return {
                    "decision": "deny",
                    "action_hash": call_hash,
                    "reason": "Bash approval requires a recorded command (AUTO-APPROVE 5.1)",
                }

        # AD-15 live wire. Orchestrator-owned calls all pass through the same
        # finance-only resolver, including calls whose ActionClass would otherwise
        # early-allow. This keeps bank/customer/money/delete classification authoritative
        # and gives risk-shaped auto paths (secret reads, local-temp deletes) a truthful
        # audit reason. Human-owned sessions retain the ordinary policy behavior.
        _orchestrator_escalation: str | None = None
        _orchestrator_reason: str | None = None
        from omniagentos.orchestrator.approvals import resolve_approval
        from omniagentos.orchestrator.contracts import ApprovalRequest

        verdict = resolve_approval(
            ApprovalRequest(
                proposed_action=proposed_action,
                action_class=str(action_class),
                tool_name=body.tool_name,
                tool_input=tool_input,
                session_id=session_row_id,
            ),
            notifier=None,  # decision oracle only; escalation reuses the park+notify path below.
        )
        if verdict.category == "bank":
            # Permanent refusal applies to every session owner. Never create an
            # approval row that could later become bank-write authority.
            return {
                "decision": "deny",
                "action_hash": call_hash,
                "reason": verdict.reason,
            }
        if dal.is_orchestrator_session(session_row_id):
            if verdict.approved:
                return {
                    "decision": "allow",
                    "action_hash": call_hash,
                    "reason": verdict.reason,
                }
            # Money/customer/production-delete fall through to the existing durable
            # approval + notification path.
            _orchestrator_escalation = verdict.category
            _orchestrator_reason = verdict.reason
        elif not decision.requires_approval:
            return {"decision": "allow", "action_hash": call_hash}

        # Any orchestrator hard stop is high risk; otherwise the existing gate stands.
        _high = decision.always_human or _orchestrator_escalation is not None
        approval_id = dal.create_session_approval(
            session_id=body.session_id,
            action_hash=call_hash,
            action_class=action_class,
            proposed_action=proposed_action,
            params_json=json.dumps(tool_input, sort_keys=True, separators=(",", ":")),
            risk="high" if _high else "medium",
            evidence=f"Session hook requested {body.tool_name}",
            expires_at=_expiry(policy_cfg.approval_expiry_hours),
        )
        # NT-notify: persist an approval-kind notification linked to the real
        # approval row (dedupe guards the retried-hook-eval case). Writes through
        # the SAME connection the approval was created on so it stays isolated;
        # best-effort so a notification issue never turns an eval into a deny. For an
        # orchestrator escalation the source is "orchestrator" so the operator feed
        # shows the hard stop came from an auto-run session.
        approval_connection = getattr(dal, "_connection", None)
        if approval_connection is not None:
            notify_approval_requested(
                approval_id=approval_id,
                proposed_action=proposed_action,
                action_class=str(action_class),
                source="orchestrator" if _orchestrator_escalation is not None else "session",
                severity="high" if _high else "warning",
                risk="high" if _high else "medium",
                session_id=str(session.get("id") or body.session_id),
                connection=approval_connection,
                push=False,
            )
        # The durable feed helper intentionally has no control-flow delivery
        # result.  This approval path must nevertheless stamp its new row with
        # the same remote-only carrier used by the supervisor's park alert --
        # and, because it pushes directly instead of through record_notification,
        # it has to supply that seam's banner labels itself: the ref-keyed
        # coalescing group (so a repeat approval REPLACES its banner rather than
        # stacking another copy in Notification Center) and the severity
        # subtitle. Both come from the shared helpers so the two writers about
        # one approval cannot mint different keys.
        try:
            from omniagentos.sessions import notify

            _severity = "high" if _high else "warning"
            delivery = notify.push_outcome(
                "Approval required",
                f"{action_class}: {proposed_action}",
                kind="approval",
                severity=_severity,
                subtitle=push_subtitle(_severity),
                group=push_group("approval", approval_id),
            ).remote_accepted
            record_delivery = getattr(dal, "record_session_approval_delivery", None)
            if callable(record_delivery):
                record_delivery(approval_id, delivered=delivery)
        except Exception:  # noqa: BLE001 - best-effort notification cannot deny a hook evaluation
            logger.debug("could not record session approval delivery", exc_info=True)
        if _orchestrator_escalation is not None:
            return {
                "decision": "deny",
                "approval_id": approval_id,
                "action_hash": call_hash,
                "reason": _orchestrator_reason,
            }
        return {
            "decision": "deny",
            "approval_id": approval_id,
            "action_hash": call_hash,
            "reason": f"parked: approval {approval_id}",
        }
    except ApiError:
        # An intentional 401 from _hook_eval_authorized above must propagate as a
        # real unauthorized response, not be swallowed into a graceful "deny" --
        # the broad except below is for genuinely UNEXPECTED failures only.
        raise
    except Exception:
        return {"decision": "deny", "action_hash": "", "reason": "api-error"}


@router.post("/ingest")
def ingest_session(
    body: SessionIngestRequest,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)],
) -> dict[str, Any]:
    """Best-effort report ingestion for non-bridge Claude sessions; never gates."""
    del _
    dal = get_sessions_dal()
    now = utc_now_iso()
    # T-DESIGN-006: resolve by canonical id OR provider session_ref, so a plain
    # provider UUID reported on start resolves the same row on later reports.
    session = dal.get_session_by_ref(body.session_id)
    if session is None:
        # Accept a normal provider UUID: mint a canonical ses_ id and keep the
        # provider ref in session_ref for subsequent lookups.
        canonical_id = body.session_id if str(body.session_id).startswith("ses_") else new_id("ses")
        dal.create_session(
            {
                "id": canonical_id,
                "source": "external",
                "project_dir": body.cwd,
                "provider": body.provider,
                "session_ref": body.session_ref or body.session_id,
                "state": body.state,
                "pid": body.pid,
                "model": body.model,
                "title": body.title,
                "budget_usd_max": None,
                "cost_usd": 0.0,
                "kill_requested": 0,
                "last_activity_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    else:
        canonical_id = str(session["id"])
        dal.touch_activity(canonical_id, now)
    # T-OPS-004 / T-DESIGN-006: a Stop/result report maps to a terminal transition so
    # external sessions can complete (no-op if already terminal or not applicable).
    if _is_terminal_report(body.hook_event_name):
        dal.terminalize_session(canonical_id, "completed", void_note="external session stop report")
    store.insert_event(
        Events.AUDIT,
        "session-hook",
        "session.ingested",
        target_type="session",
        target_id=canonical_id,
        payload={
            "session_id": canonical_id,
            "provider_ref": body.session_id,
            "hook_event_name": body.hook_event_name,
            "tool_name": body.tool_name,
        },
    )
    return {"ok": True, "session_id": canonical_id}


@router.post("/{session_id}/kill")
def request_kill(session_id: str, _: Annotated[None, Depends(_authorized)]) -> dict[str, Any]:
    del _
    dal = get_sessions_dal()
    if dal.get_session(session_id) is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    if not dal.request_kill(session_id):
        fail(409, "invalid_state", "kill request could not be recorded", {"id": session_id})
    updated = dal.get_session(session_id)
    if updated is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    return _session_row(updated)


@router.post("/{session_id}/cancel")
def request_cancel(session_id: str, _: Annotated[None, Depends(_authorized)]) -> dict[str, Any]:
    """Durably request cancellation; the supervisor stops the process first."""
    del _
    dal = get_sessions_dal()
    session = dal.get_session(session_id)
    if session is None:
        fail(404, "not_found", "session not found", {"id": session_id})
    state = str(session["state"])
    if state == "cancelled":
        return {"session_id": session_id, "status": "cancelled"}
    if state in {"completed", "failed", "killed"}:
        return {"session_id": session_id, "status": state}
    if not dal.request_cancel(session_id):
        fail(409, "invalid_state", "cancel request could not be recorded", {"id": session_id})
    return {"session_id": session_id, "status": "cancel_requested"}
