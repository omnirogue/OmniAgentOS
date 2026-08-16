"""Project every live agent session onto the Kanban board.

Covers:
1. Process-discovered interactive CLIs (all providers) that OmniAgentOS
   did not start — ``source=external``, ``session_ref=extproc:…``.
2. Any already-tracked non-terminal session (bridge or external) that lacks a
   board card — so the Kanban always mirrors active terminals.

Safe / observational: never kills foreign processes; only mirrors state.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.sessions.dal import SessionsDal, SessionState
from omniagentos.sessions.discover import (
    EXTPROC_REF_PREFIX,
    DiscoveredProcess,
    discovery_enabled,
    list_discovered_processes,
)

LOG = logging.getLogger(__name__)

_SYNC_LOCK = threading.Lock()
_LAST_SYNC_AT: dict[str, float] = {}
_DEFAULT_SYNC_SECONDS = 15.0

_ACTIVE_STATES = frozenset(
    {
        SessionState.STARTING.value,
        SessionState.RUNNING.value,
        SessionState.AWAITING_APPROVAL.value,
        SessionState.RESUMING.value,
    }
)


def _sync_interval_seconds() -> float:
    raw = os.environ.get("OMNIAGENTOS_DISCOVER_INTERVAL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_SYNC_SECONDS
    try:
        return max(5.0, float(raw))
    except ValueError:
        return _DEFAULT_SYNC_SECONDS


def _claim_sync(db_key: str) -> bool:
    now = time.monotonic()
    interval = _sync_interval_seconds()
    with _SYNC_LOCK:
        last = _LAST_SYNC_AT.get(db_key, float("-inf"))
        if now - last < interval:
            return False
        _LAST_SYNC_AT[db_key] = now
        return True


def reset_external_board_throttle(db_key: str | None = None) -> None:
    """Test helper: force the next sync to run immediately."""
    with _SYNC_LOCK:
        if db_key is None:
            _LAST_SYNC_AT.clear()
        else:
            _LAST_SYNC_AT.pop(db_key, None)


def _cwd_label(cwd: str | None, project_dir: str | None = None) -> str:
    path = (cwd or project_dir or "").strip()
    if not path:
        return "unknown cwd"
    try:
        return Path(path).expanduser().name or path
    except Exception:  # noqa: BLE001
        return path[-40:]


def _title_for_discovered(proc: DiscoveredProcess) -> str:
    where = _cwd_label(proc.cwd)
    tty = proc.tty or "headless"
    return f"[{proc.provider} · external] {where} · {tty}"


def _title_for_session(session: dict[str, Any]) -> str:
    existing = str(session.get("title") or "").strip()
    if existing:
        return existing
    provider = str(session.get("provider") or "agent")
    source = str(session.get("source") or "bridge")
    where = _cwd_label(None, str(session.get("project_dir") or ""))
    tag = "external" if source == "external" else "session"
    return f"[{provider} · {tag}] {where}"


def _description_for_discovered(proc: DiscoveredProcess) -> str:
    cmd = (proc.command or "")[:240]
    parts = [
        "Active terminal discovered on this machine (not started by OmniAgentOS).",
        "Observational only — no hooks or control attached.",
        f"provider={proc.provider} pid={proc.pid}",
    ]
    if proc.tty:
        parts.append(f"tty={proc.tty}")
    if proc.cwd:
        parts.append(f"cwd={proc.cwd}")
    if cmd:
        parts.append(f"cmd={cmd}")
    return "\n".join(parts)


def _is_extproc_session(session: dict[str, Any]) -> bool:
    ref = str(session.get("session_ref") or "")
    return ref.startswith(EXTPROC_REF_PREFIX)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive.
        return True
    except OSError:
        return False
    return True


def _list_live_sessions(dal: SessionsDal, limit: int = 500) -> list[dict[str, Any]]:
    """All non-terminal sessions (any provider / source)."""
    if hasattr(dal, "list_live_sessions"):
        try:
            return list(dal.list_live_sessions(limit=limit))
        except Exception:  # noqa: BLE001
            LOG.debug("list_live_sessions failed; falling back", exc_info=True)
    # Fallback: per-state queries (works against older DAL stubs in tests).
    out: list[dict[str, Any]] = []
    for state in _ACTIVE_STATES:
        try:
            out.extend(dal.list_sessions(state=state, limit=limit))
        except Exception:  # noqa: BLE001
            continue
    return out


def _board_result_ref_index(collab_store: CollabStore) -> dict[str, dict[str, Any]]:
    """Map result_ref → board task for non-archived cards."""
    try:
        tasks = collab_store.list_board_tasks(archived=0)
    except TypeError:
        tasks = collab_store.list_board_tasks()
    index: dict[str, dict[str, Any]] = {}
    for task in tasks:
        ref = task.get("result_ref")
        if isinstance(ref, str) and ref:
            # Prefer the first (list is newest-first); don't overwrite.
            index.setdefault(ref, task)
    return index


def _ensure_board_card(
    collab_store: CollabStore,
    session: dict[str, Any],
    *,
    index: dict[str, dict[str, Any]],
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any] | None:
    session_id = str(session.get("id") or "")
    if not session_id:
        return None
    existing = index.get(session_id)
    if existing is not None:
        return existing

    # Skip swarm member cards — swarm owns those board rows.
    # (We can't know swarm linkage from session alone reliably; if a card already
    # exists with result_ref elsewhere it's fine. Creating a plain card for a
    # swarm-owned session is rare because those sessions already have cards.)

    board = BoardTask(
        title=(title or _title_for_session(session))[:200],
        description=description
        or (
            f"Live {session.get('provider') or 'agent'} session "
            f"(source={session.get('source') or 'unknown'})."
        ),
        discipline=str(session.get("provider") or "") or None,
        priority="normal",
        status=BoardTaskStatus.IN_PROGRESS,
        result_ref=session_id,
    )
    try:
        collab_store.create_board_task(board)
    except Exception:  # noqa: BLE001 -- never break board reads on create races
        LOG.debug("external board card create failed for %s", session_id, exc_info=True)
        return None
    created = collab_store.get_board_task(board.id) or {
        "id": board.id,
        "result_ref": session_id,
        "status": BoardTaskStatus.IN_PROGRESS.value,
    }
    index[session_id] = created
    # Best-effort board.updated event
    try:
        h1 = getattr(collab_store, "_store", None)
        if h1 is not None and hasattr(h1, "insert_event"):
            h1.insert_event(
                "board.updated",
                "external-board",
                "board.external_projected",
                target_type="board_task",
                target_id=str(created.get("id") or board.id),
                payload={"session_id": session_id, "provider": session.get("provider")},
            )
    except Exception:  # noqa: BLE001
        LOG.debug("external board event emit failed", exc_info=True)
    return created


def _upsert_discovered_session(
    dal: SessionsDal,
    proc: DiscoveredProcess,
    *,
    live_by_pid: dict[int, dict[str, Any]],
    live_by_ref: dict[str, dict[str, Any]],
    agents_by_pid: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Create or refresh an external session row for a discovered process."""
    now = utc_now_iso()
    # Agent View data is Claude-only; never attach it to other providers even
    # when an OS pid collides with an entry in the Claude registry map.
    agent = agents_by_pid.get(proc.pid, {}) if proc.provider == "claude" else {}
    agent_fields = {
        "agent_name": agent.get("name") if isinstance(agent.get("name"), str) else None,
        "agent_status": agent.get("status") if isinstance(agent.get("status"), str) else None,
        "agent_profile": agent.get("profile") if isinstance(agent.get("profile"), str) else None,
        "agent_session_id": (
            agent.get("sessionId") if isinstance(agent.get("sessionId"), str) else None
        ),
    }
    existing = live_by_pid.get(proc.pid) or live_by_ref.get(proc.session_ref)
    if existing is not None:
        session_id = str(existing["id"])
        # Keep pid / activity fresh; promote starting → running if needed.
        try:
            if existing.get("pid") != proc.pid:
                dal.set_pid(session_id, proc.pid)
            dal.touch_activity(session_id, now)
            state = str(existing.get("state") or "")
            if state == SessionState.STARTING.value:
                dal.update_session_state(
                    session_id,
                    SessionState.RUNNING.value,
                    expect=SessionState.STARTING.value,
                )
                existing["state"] = SessionState.RUNNING.value
            # An empty agent dict means "no data for this pid" — which is also
            # what a transient collector failure (timeout, bad JSON) looks like.
            # Never null-overwrite previously persisted metadata on absence;
            # only write when the collector actually returned data.
            if agent and hasattr(dal, "set_agent_view"):
                dal.set_agent_view(
                    session_id,
                    name=agent_fields["agent_name"],
                    status=agent_fields["agent_status"],
                    profile=agent_fields["agent_profile"],
                    session_id_value=agent_fields["agent_session_id"],
                )
                existing.update(agent_fields)
            existing["pid"] = proc.pid
            existing["last_activity_at"] = now
        except Exception:  # noqa: BLE001
            LOG.debug("refresh discovered session %s failed", session_id, exc_info=True)
        return existing

    session_id = new_id("ses")
    project_dir = proc.cwd or str(Path.home())
    row = {
        "id": session_id,
        "source": "external",
        "project_dir": project_dir,
        "provider": proc.provider,
        "session_ref": proc.session_ref,
        "state": SessionState.RUNNING.value,
        "pid": proc.pid,
        "model": None,
        "title": _title_for_discovered(proc),
        **agent_fields,
        "budget_usd_max": None,
        # Unknown at discovery — SQL NULL, never a silent $0.00 (migration 088).
        # Process discovery has no usage report; 0.0 would claim "spent nothing"
        # and the dashboard would render $0.00 for a long-running external CLI.
        # Matches usage_capture: cost_usd left unknown (NULL), never a silent $0.00.
        "cost_usd": None,
        "kill_requested": 0,
        "last_activity_at": now,
        "created_at": now,
        "updated_at": now,
    }
    dal.create_session(row)
    live_by_pid[proc.pid] = row
    live_by_ref[proc.session_ref] = row
    return row


def _terminalize_missing_extproc(
    dal: SessionsDal,
    live_sessions: list[dict[str, Any]],
    live_pids: set[int],
) -> int:
    """Mark process-discovered external sessions completed when the PID is gone."""
    closed = 0
    for session in live_sessions:
        if str(session.get("source") or "") != "external":
            continue
        if not _is_extproc_session(session):
            continue
        raw_pid = session.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else 0
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and (pid in live_pids or _pid_alive(pid)):
            continue
        session_id = str(session["id"])
        try:
            if dal.terminalize_session(
                session_id,
                SessionState.COMPLETED.value,
                void_note="external process no longer running",
            ):
                closed += 1
                session["state"] = SessionState.COMPLETED.value
        except Exception:  # noqa: BLE001
            LOG.debug("terminalize missing extproc %s failed", session_id, exc_info=True)
    return closed


def _set_busy_timeout_ms(dal: Any, ms: int) -> int | None:
    """Set this connection's timeout and return its prior value, best effort."""
    conn = getattr(dal, "_connection", None)
    if conn is None:
        store = getattr(dal, "_store", None)
        conn = getattr(store, "_connection", None) if store is not None else None
    if conn is None:
        return None
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        previous = int(row[0]) if row is not None else None
        conn.execute(f"PRAGMA busy_timeout={int(ms)}")
        return previous
    except Exception:  # noqa: BLE001
        LOG.debug("could not set busy_timeout=%s", ms, exc_info=True)
        return None


def sync_external_sessions_to_board(
    sessions_dal: SessionsDal,
    collab_store: CollabStore,
    *,
    discovered: list[DiscoveredProcess] | None = None,
    force: bool = False,
    db_key: str | None = None,
) -> dict[str, Any]:
    """Discover external agent CLIs, upsert sessions, ensure Kanban cards.

    Returns a small stats dict for tests / diagnostics.

    Never blocks the board read path: discovery writes use a short busy_timeout
    and abort cleanly if the database is locked by another writer.
    """
    key = db_key or getattr(sessions_dal, "db_path", None) or "default"
    key = str(key)
    if not force and not _claim_sync(key):
        return {"skipped": True, "reason": "throttled"}

    if not discovery_enabled() and discovered is None:
        # Still project already-tracked live sessions onto the board.
        procs: list[DiscoveredProcess] = []
    elif discovered is not None:
        procs = list(discovered)
    else:
        try:
            procs = list_discovered_processes()
        except Exception:  # noqa: BLE001 -- discovery must never break /api/board
            LOG.exception("external process discovery failed")
            procs = []

    # Fail-fast under lock contention: board reconcile must stay <250ms when a
    # writer holds the DB (see test_reconcile_without_stale_rows_…).
    sessions_busy_timeout = _set_busy_timeout_ms(sessions_dal, 50)
    collab_busy_timeout = _set_busy_timeout_ms(collab_store, 50)
    try:
        return _sync_external_sessions_to_board_body(
            sessions_dal,
            collab_store,
            procs=procs,
        )
    except Exception as exc:  # noqa: BLE001
        # sqlite3.OperationalError: database is locked, etc.
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            LOG.debug("external board sync deferred (db busy): %s", exc)
            return {"skipped": True, "reason": "db_busy"}
        raise
    finally:
        if sessions_busy_timeout is not None:
            _set_busy_timeout_ms(sessions_dal, sessions_busy_timeout)
        if collab_busy_timeout is not None:
            _set_busy_timeout_ms(collab_store, collab_busy_timeout)


def _sync_external_sessions_to_board_body(
    sessions_dal: SessionsDal,
    collab_store: CollabStore,
    *,
    procs: list[DiscoveredProcess],
) -> dict[str, Any]:
    try:
        from omniagentos.sessions.agents_view import collect_all

        agents_by_pid = collect_all()
    except Exception:  # noqa: BLE001 -- Agent View must never break board projection
        LOG.debug("Claude Agent View enrollment unavailable", exc_info=True)
        agents_by_pid = {}
    live = _list_live_sessions(sessions_dal)
    live_by_pid: dict[int, dict[str, Any]] = {}
    live_by_ref: dict[str, dict[str, Any]] = {}
    for session in live:
        raw_pid = session.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else 0
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            live_by_pid.setdefault(pid, session)
        ref = str(session.get("session_ref") or "")
        if ref:
            live_by_ref.setdefault(ref, session)

    created_sessions = 0
    refreshed = 0
    for proc in procs:
        # If a bridge-owned session already has this pid, don't mint a second row.
        existing = live_by_pid.get(proc.pid)
        if existing is not None and str(existing.get("source") or "") == "bridge":
            refreshed += 1
            try:
                sessions_dal.touch_activity(str(existing["id"]), utc_now_iso())
            except Exception:  # noqa: BLE001
                pass
            continue
        before_ids = {str(s["id"]) for s in live_by_pid.values()} | {
            str(s["id"]) for s in live_by_ref.values()
        }
        row = _upsert_discovered_session(
            sessions_dal,
            proc,
            live_by_pid=live_by_pid,
            live_by_ref=live_by_ref,
            agents_by_pid=agents_by_pid,
        )
        if str(row["id"]) not in before_ids:
            created_sessions += 1
            live.append(row)
        else:
            refreshed += 1

    live_pids = {p.pid for p in procs}
    closed = _terminalize_missing_extproc(sessions_dal, live, live_pids)

    # Re-list so board projection sees newly created / still-active rows only.
    live = [
        s for s in _list_live_sessions(sessions_dal) if str(s.get("state") or "") in _ACTIVE_STATES
    ]

    index = _board_result_ref_index(collab_store)
    cards_created = 0
    for session in live:
        sid = str(session.get("id") or "")
        if not sid or sid in index:
            continue
        title = str(session.get("title") or "") or None
        desc = None
        if _is_extproc_session(session):
            desc = (
                "Active terminal discovered on this machine "
                "(not started by OmniAgentOS). Observational only."
            )
        before = set(index.keys())
        created = _ensure_board_card(
            collab_store,
            session,
            index=index,
            title=title,
            description=desc,
        )
        if created is not None and sid not in before and sid in index:
            cards_created += 1

    return {
        "skipped": False,
        "discovered": len(procs),
        "sessions_created": created_sessions,
        "sessions_refreshed": refreshed,
        "sessions_closed": closed,
        "cards_created": cards_created,
        "live_sessions": len(live),
        "providers": sorted({p.provider for p in procs}),
    }


def sync_external_sessions_to_board_safe(
    sessions_dal: Any,
    collab_store: Any,
    *,
    force: bool = False,
    db_key: str | None = None,
) -> dict[str, Any]:
    """Wrapper that never raises — safe to call from ``reconcile_board``."""
    try:
        return sync_external_sessions_to_board(
            sessions_dal,
            collab_store,
            force=force,
            db_key=db_key,
        )
    except Exception:  # noqa: BLE001
        LOG.exception("sync_external_sessions_to_board failed (isolated)")
        return {"skipped": True, "reason": "error"}
