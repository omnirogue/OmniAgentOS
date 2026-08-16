from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import workbook
from omniagentos.longhaul.engine import LonghaulEngine
from omniagentos.longhaul.store import LonghaulStore


def async_test(function: Any) -> Any:
    @functools.wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeSupervisor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("injected spawn crash")
        return f"ses_fake_{len(self.calls)}"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "longhaul.db")
    migrate(path)
    monkeypatch.setattr(workbook, "WORKBOOK_ROOT", tmp_path / "workbooks")
    return path


def _cfg(supervisor: FakeSupervisor, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "max_sessions": 8,
        "default_cooldown_s": 3600,
        "cross_harness_fallback": False,
        "static_fallback_order": [{"harness": "cli-claude", "model": "opus"}],
        "review": {"enabled": False, "deny_respawns": 2},
        "spawn_grace_s": 0,
        # Existing engine tests exercise immediate redispatch; L-13 backoff is
        # covered by a dedicated test that opts back in.
        "fast_crash_s": 0,
        "_supervisor": supervisor,
    }
    cfg.update(overrides)
    return cfg


def _task(
    store: LonghaulStore,
    task_id: str,
    *,
    category_id: str | None = None,
    status: str = "pending",
    state: dict[str, Any] | None = None,
) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO board_tasks "
        "(id,title,description,status,created_at,updated_at,lane,category_id,longhaul_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            f"Task {task_id}",
            "Implement the requested code.\n\nAcceptance criteria:\n- Tests pass",
            status,
            now,
            now,
            "longhaul",
            category_id,
            json.dumps(state or {}),
        ),
    )
    store._connection.commit()


def _account(store: LonghaulStore, account_id: str, *, cooling: bool = False) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO claude_accounts "
        "(id,label,enabled,status,created_at,updated_at,cooldown_until) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            account_id,
            account_id,
            1,
            "rate_limited" if cooling else "ok",
            now,
            now,
            "2999-01-01T00:00:00Z" if cooling else None,
        ),
    )
    store._connection.commit()


def _done(task_id: str) -> None:
    path = Path(workbook._path(task_id))
    path.write_text(path.read_text().replace("WORKING", "DONE"), encoding="utf-8")


@async_test
async def test_category_serialization_and_fifo_release(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    category = store.create_category("Repository", wip_limit=1)
    _task(store, "btk_one", category_id=category["id"])
    _task(store, "btk_two", category_id=category["id"])
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)

    await engine.dispatch("btk_one")
    await engine.dispatch("btk_two")
    assert (
        store._connection.execute(
            "SELECT park_state FROM board_tasks WHERE id='btk_two'"
        ).fetchone()["park_state"]
        == "waiting_category"
    )

    _done("btk_one")
    first = store.current_attempt("btk_one")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{first['id']}]", "state": "completed"}, [], 0
    )
    assert (
        store._connection.execute("SELECT status FROM board_tasks WHERE id='btk_one'").fetchone()[
            "status"
        ]
        == "done"
    )
    assert store.current_attempt("btk_two") is not None


@async_test
async def test_transition_events_populate_execution_id_and_gap_free_sequence(
    db_path: str,
) -> None:
    """W2.6 (086): a board_task IS the execution unit for the longhaul lane, so
    every ``_event_in_tx`` row (emitted from ``_transition``) must carry
    execution_id=task_id and a dense per-task 1, 2, 3, ... sequence."""
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_seq")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)

    await engine.dispatch("btk_seq")
    _done("btk_seq")
    attempt = store.current_attempt("btk_seq")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{attempt['id']}]", "state": "completed"}, [], 0
    )

    rows = store._connection.execute(
        "SELECT sequence FROM events WHERE execution_id = 'btk_seq' ORDER BY id ASC"
    ).fetchall()
    sequences = [row["sequence"] for row in rows]
    assert len(sequences) >= 2, "dispatch + completion must emit at least 2 events"
    assert sequences == list(range(1, len(sequences) + 1)), (
        "sequence must be dense 1..N with no gap or duplicate"
    )
    # NOTE: LonghaulStore.close_attempt_with_task_state (longhaul/store.py, a
    # DIFFERENT file, out of this lane's owned paths) has its own clone-sibling
    # "INSERT INTO events" that is NOT touched here and stays execution_id=NULL
    # -- see the lane report follow-up. Only LonghaulEngine._event_in_tx's rows
    # are asserted dense above.


@async_test
async def test_dispatch_is_durable_and_title_marker_survives(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_durable")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_durable")

    attempt = store.current_attempt("btk_durable")
    assert attempt is not None and attempt["session_id"] == "ses_fake_1"
    assert supervisor.calls[0]["title_prefix"] == f"[longhaul:{attempt['id']}]"
    state = store.get_longhaul_json("btk_durable")
    assert state["phase"] == "running"
    assert Path(state["workbook_path"]).exists()


@async_test
async def test_tick_closes_spawn_incomplete_and_redispatches(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor(fail=True)
    _account(store, "acct_a")
    _task(store, "btk_crash")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_crash")
    first = store.current_attempt("btk_crash")
    supervisor.fail = False

    await engine.tick()
    attempts = store.list_attempts("btk_crash")
    assert attempts[0]["id"] == first["id"]
    assert attempts[0]["end_reason"] == "crashed"
    assert attempts[0]["detail"] == "spawn_incomplete"
    assert attempts[1]["session_id"] == "ses_fake_2"


@async_test
async def test_duplicate_terminal_callback_is_idempotent(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_dupe")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_dupe")
    attempt = store.current_attempt("btk_dupe")
    _done("btk_dupe")
    session = {"title": f"[longhaul:{attempt['id']}]", "state": "completed"}

    await engine.on_session_terminal(session, [], 0)
    await engine.on_session_terminal(session, [], 0)
    content = workbook.read_workbook("btk_dupe")
    assert content.count("### Checkpoint") == 1
    assert len(store.list_attempts("btk_dupe")) == 1


@async_test
async def test_usage_limit_sets_cooldown_and_parks_without_capacity(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_limit")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_limit")
    attempt = store.current_attempt("btk_limit")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{attempt['id']}]", "state": "failed"},
        [
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "error": "You've reached your usage limit; resets at 3am UTC",
            }
        ],
        1,
    )
    account = store._connection.execute(
        "SELECT status,cooldown_until FROM claude_accounts WHERE id='acct_a'"
    ).fetchone()
    task = store._connection.execute(
        "SELECT status,park_state FROM board_tasks WHERE id='btk_limit'"
    ).fetchone()
    assert account["status"] == "rate_limited" and account["cooldown_until"]
    assert dict(task) == {"status": "in_progress", "park_state": "waiting_capacity"}


@async_test
async def test_healthy_limit_text_is_not_a_cooldown(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_false_limit")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_false_limit")
    attempt = store.current_attempt("btk_false_limit")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{attempt['id']}]", "state": "completed"},
        [{"type": "assistant", "message": "Documentation mentions a usage limit."}],
        0,
    )
    assert (
        store._connection.execute(
            "SELECT cooldown_until FROM claude_accounts WHERE id='acct_a'"
        ).fetchone()["cooldown_until"]
        is None
    )
    assert store.list_attempts("btk_false_limit")[-1]["ended_at"] is None


@async_test
async def test_all_accounts_cooling_parks_waiting_capacity(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a", cooling=True)
    _account(store, "acct_b", cooling=True)
    _task(store, "btk_capacity")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_capacity")
    row = store._connection.execute(
        "SELECT status,park_state FROM board_tasks WHERE id='btk_capacity'"
    ).fetchone()
    assert dict(row) == {"status": "in_progress", "park_state": "waiting_capacity"}
    assert supervisor.calls == []


@async_test
async def test_auth_failure_disables_account_and_has_one_successor(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _account(store, "acct_b")
    _task(store, "btk_auth")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_auth")
    first = store.current_attempt("btk_auth")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{first['id']}]", "state": "failed"},
        [
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "error": "failed to authenticate OAuth session",
            }
        ],
        1,
    )
    assert (
        store._connection.execute(
            "SELECT enabled FROM claude_accounts WHERE id='acct_a'"
        ).fetchone()["enabled"]
        == 0
    )
    assert len(store.list_attempts("btk_auth")) == 2
    assert len(supervisor.calls) == 2


@async_test
async def test_successful_exit_without_done_continues(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_unfinished")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_unfinished")
    first = store.current_attempt("btk_unfinished")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{first['id']}]", "state": "completed"}, [], 0
    )
    attempts = store.list_attempts("btk_unfinished")
    assert [item["end_reason"] for item in attempts] == ["unfinished_exit", None]


@async_test
async def test_max_sessions_blocks_and_never_fails_board(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_max")
    engine = LonghaulEngine(store, _cfg(supervisor, max_sessions=1), db_path)
    await engine.dispatch("btk_max")
    attempt = store.current_attempt("btk_max")
    await engine.on_session_terminal(
        {"title": f"[longhaul:{attempt['id']}]", "state": "completed"}, [], 0
    )
    row = store._connection.execute(
        "SELECT status,longhaul_json FROM board_tasks WHERE id='btk_max'"
    ).fetchone()
    assert row["status"] == "blocked"
    assert json.loads(row["longhaul_json"])["phase"] == "blocked"


@async_test
@pytest.mark.parametrize(
    ("verdict", "expected_status", "expected_park", "attempt_count"),
    [
        ({"verdict": "confirm", "feedback": "ok"}, "done", None, 1),
        ({"verdict": "deny", "feedback": "fix tests"}, "in_progress", None, 2),
        (None, "in_progress", "waiting_review", 1),
    ],
)
async def test_review_gate_pass_deny_and_unavailable(
    db_path: str,
    verdict: dict[str, str] | None,
    expected_status: str,
    expected_park: str | None,
    attempt_count: int,
) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    task_id = f"btk_review_{expected_status}_{expected_park}"
    _task(store, task_id)

    def reviewer(*_: Any) -> Any:
        return verdict

    cfg = _cfg(
        supervisor,
        review={"enabled": True, "deny_respawns": 2, "backoff_s": 1},
        _reviewer=reviewer,
    )
    engine = LonghaulEngine(store, cfg, db_path)
    await engine.dispatch(task_id)
    attempt = store.current_attempt(task_id)
    _done(task_id)
    await engine.on_session_terminal(
        {"title": f"[longhaul:{attempt['id']}]", "state": "completed"}, [], 0
    )
    row = store._connection.execute(
        "SELECT status,park_state FROM board_tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert dict(row) == {"status": expected_status, "park_state": expected_park}
    assert len(store.list_attempts(task_id)) == attempt_count


@async_test
@pytest.mark.parametrize(
    ("session_state", "killed_by", "kill_requested", "expect"),
    [
        # D7: reaper/budget/reconcile/unknown kills route to the killed branch
        # so the engine (sole respawn owner) spawns exactly one successor.
        ("killed", "idle-reaper", 1, "respawn"),
        ("killed", "budget", 1, "respawn"),
        ("killed", "reconcile", 0, "respawn"),  # regression pin: worked pre-D7
        ("killed", None, 1, "respawn"),  # unknown killer: respawn, never cancel
        # Operator intent — and ONLY operator intent — supersedes the task.
        ("killed", "operator", 1, "cancelled"),
        ("killed", "cancel_requested", 1, "cancelled"),
        ("cancelled", "operator", 1, "cancelled"),
        # F2: natural exit after request_cancel — terminalize preserves
        # killed_by='cancel_requested' (COALESCE), so even state='completed'
        # supersedes instead of classifying as a normal completion.
        ("completed", "cancel_requested", 1, "cancelled"),
    ],
)
async def test_killed_by_discrimination_matrix(
    db_path: str,
    session_state: str,
    killed_by: str | None,
    kill_requested: int,
    expect: str,
) -> None:
    """D7 terminal matrix: kill_requested alone is NOT operator intent."""
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    task_id = f"btk_matrix_{session_state}_{killed_by}_{expect}"
    _task(store, task_id)
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch(task_id)
    attempt = store.current_attempt(task_id)

    await engine.on_session_terminal(
        {
            "title": f"[longhaul:{attempt['id']}]",
            "state": session_state,
            "killed_by": killed_by,
            "kill_requested": kill_requested,
        },
        [],
        143,
    )

    attempts = store.list_attempts(task_id)
    status = store._connection.execute(
        "SELECT status FROM board_tasks WHERE id=?", (task_id,)
    ).fetchone()["status"]
    if expect == "respawn":
        assert attempts[0]["end_reason"] == "killed"
        assert len(attempts) == 2  # exactly one successor
        assert attempts[1]["ended_at"] is None
        assert status == "in_progress"
    else:
        assert attempts[0]["end_reason"] == "superseded"
        assert len(attempts) == 1  # no successor, ever
        assert status == "cancelled"


@async_test
async def test_duplicate_reaper_kill_delivery_closes_once(db_path: str) -> None:
    """Direct notify + tick reconcile can both deliver the same terminal session;
    the close_attempt CAS must make the second delivery a no-op (one successor)."""
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_dupe_kill")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)
    await engine.dispatch("btk_dupe_kill")
    attempt = store.current_attempt("btk_dupe_kill")
    session = {
        "title": f"[longhaul:{attempt['id']}]",
        "state": "killed",
        "killed_by": "idle-reaper",
        "kill_requested": 1,
    }

    await engine.on_session_terminal(session, [], 143)
    await engine.on_session_terminal(session, [], 143)

    attempts = store.list_attempts("btk_dupe_kill")
    assert [item["end_reason"] for item in attempts] == ["killed", None]
    assert len(supervisor.calls) == 2  # original spawn + exactly one successor


@async_test
async def test_spawn_passes_idle_minutes_on_fresh_and_native_resume(db_path: str) -> None:
    """D8: the lane idle threshold rides supervisor.spawn on BOTH spawn paths —
    fresh dispatch and the one-shot native resume after a kill."""
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_idle")
    engine = LonghaulEngine(store, _cfg(supervisor), db_path)

    await engine.dispatch("btk_idle")
    assert supervisor.calls[0]["idle_minutes"] == 45.0  # config default
    assert supervisor.calls[0]["resume_session_ref"] is None

    first = store.current_attempt("btk_idle")
    await engine.on_session_terminal(
        {
            "title": f"[longhaul:{first['id']}]",
            "state": "killed",
            "killed_by": "idle-reaper",
            "kill_requested": 1,
            "session_ref": "ref-native-1",
        },
        [],
        143,
    )
    assert supervisor.calls[1]["resume_session_ref"] == "ref-native-1"  # resume path
    assert supervisor.calls[1]["idle_minutes"] == 45.0


@async_test
async def test_spawn_honors_configured_idle_minutes(db_path: str) -> None:
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_idle_cfg")
    engine = LonghaulEngine(store, _cfg(supervisor, idle_minutes=60), db_path)
    await engine.dispatch("btk_idle_cfg")
    assert supervisor.calls[0]["idle_minutes"] == 60.0


@async_test
async def test_yaml_bool_idle_minutes_falls_back_to_default(db_path: str) -> None:
    """F3: bool is an int subclass — a YAML `idle_minutes: true` must fall back
    to the 45-minute default, never coerce to a 1.0-minute reap threshold."""
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    _account(store, "acct_a")
    _task(store, "btk_idle_bool")
    engine = LonghaulEngine(store, _cfg(supervisor, idle_minutes=True), db_path)
    await engine.dispatch("btk_idle_bool")
    assert supervisor.calls[0]["idle_minutes"] == 45.0


@async_test
async def test_codex_attempt_records_supervision_detail_and_cancel_reconciles(
    db_path: str,
) -> None:
    store = LonghaulStore(db_path)
    _task(store, "btk_codex")
    cfg = {
        "cross_harness_fallback": True,
        "static_fallback_order": [{"harness": "cli-codex", "model": "gpt-5.6-sol"}],
        "review": {"enabled": False},
        "_codex_runner": lambda *_: {"pid": 999999, "pgid": 999999, "started": utc_now_iso()},
    }
    engine = LonghaulEngine(store, cfg, db_path)
    await engine.dispatch("btk_codex")
    attempt = store.current_attempt("btk_codex")
    assert json.loads(attempt["detail"])["pgid"] == 999999

    store._connection.execute(
        "UPDATE board_tasks SET archived_at=? WHERE id='btk_codex'", (utc_now_iso(),)
    )
    store._connection.commit()
    await engine.tick()
    assert store.list_attempts("btk_codex")[0]["end_reason"] == "superseded"
    assert (
        store._connection.execute("SELECT status FROM board_tasks WHERE id='btk_codex'").fetchone()[
            "status"
        ]
        == "cancelled"
    )
