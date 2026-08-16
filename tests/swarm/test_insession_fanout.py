"""PKG-INSESSION-FANOUT: coordinator-granted in-session fan-out.

A claude swarm worker's validated subtasks_request may be answered LIVE with a
durable grant (``subtasks_grant.<attempt>.json``) authorizing 2-4 children as
subagents inside its own session. These tests pin the four layers:

* the grant store: admission is atomic + capacity-checked, one grant per
  attempt ever, consume is bounded by budget/TTL/void/attempt-liveness;
* the scheduler seams: the await-loop grant scan (decides at most once,
  fail-closed) and the settle-time rule (a CONSUMED grant skips the
  card-split; an unconsumed one is voided and today's flow runs unchanged);
* the accounting: live grant budgets count against max_agents_per_account,
  fleet_available reports live children, close_attempt voids on every path;
* the prompts/argv: the insession protocol variant and the Task unlock are
  flag-gated and byte-identical when dark.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import omniagentos.sessions.supervisor as sup
from omniagentos.collab.store import CollabStore
from omniagentos.swarm import insession
from omniagentos.swarm.contracts import (
    ACTION_SUBTASKS_DENIED,
    ACTION_SUBTASKS_GRANTED,
)
from omniagentos.swarm.scheduler import (
    _RunState,
    subtasks_request_protocol_lines,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler
from tests.swarm.test_request_subtasks import (
    VALID_REQUEST,
    _workbook_dir,
    _write_file,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One DB for everything, exactly like production: the harness db is also
    the default db insession/limit_state read. The workbook root is redirected
    beside it, and the feature env override starts UNSET."""
    monkeypatch.delenv("OMNIAGENTOS_INSESSION_FANOUT", raising=False)
    var_root = tmp_path / "var" / "swarm"
    monkeypatch.setattr("omniagentos.swarm.spawn.default_swarm_var_root", lambda: var_root)


def _init_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = str(tmp_path / "main.db")
    CollabStore(db)
    monkeypatch.setenv("OMNIAGENTOS_DB", db)
    return db


def _seed_attempt(
    db: str,
    attempt_id: str = "att-1",
    *,
    session_id: str = "ses-1",
    account_id: str = "acct-1",
    ended: bool = False,
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
        "session_id, provider, model, tier, account_id, started_at, ended_at, source) "
        "VALUES (?, 'run-1', ?, 1, ?, 'claude', 'sonnet', 'standard', ?, ?, ?, ?)",
        (
            attempt_id,
            f"task-{attempt_id}",
            session_id,
            account_id,
            "2026-07-27T11:00:00Z",
            "2026-07-27T11:30:00Z" if ended else None,
            "test",
        ),
    )
    conn.commit()
    conn.close()


CHILDREN = [
    {"title": "part one", "description": "one half", "owned_paths": ["src/a/one.txt"]},
    {"title": "part two", "description": "other half", "owned_paths": ["src/a/two.txt"]},
]


def _grant(
    db: str,
    attempt_id: str = "att-1",
    session_id: str = "ses-1",
    *,
    now: datetime | None = NOW,
):
    """Create a grant at the FIXED clock by default (deterministic tests that
    also consume/read at ``NOW``). Tests whose consumes/reads run on the REAL
    clock must pass ``now=None`` — a fixed creation time plus a real-clock read
    is a time bomb that expires the grant once wall time passes NOW + TTL."""
    grant, deny = insession.create_grant(
        swarm_run_id="run-1",
        board_task_id=f"task-{attempt_id}",
        attempt_id=attempt_id,
        session_id=session_id,
        provider="claude",
        account_id="acct-1",
        children=CHILDREN,
        db_path=db,
        now=now,
    )
    assert deny is None, deny
    assert grant is not None
    return grant


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------


class TestConfig:
    def test_flag_mirrors_the_shipped_config(self) -> None:
        """With no env override, the flag is exactly what configs/swarm.yaml
        ships (LIVE since 2026-07-27) — pinned via the file so this test states
        the shipped posture without hard-coding it twice."""
        import yaml

        from omniagentos.routing.limit_state import swarm_config_path

        shipped = yaml.safe_load(swarm_config_path().read_text(encoding="utf-8"))
        assert insession.insession_enabled() is bool(shipped["insession"]["enabled"])
        assert shipped["insession"]["enabled"] is True  # the 2026-07-27 turn-on

    @pytest.mark.parametrize(
        ("value", "expected"), [("1", True), ("true", True), ("0", False), ("off", False)]
    )
    def test_env_overrides_both_directions(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", value)
        assert insession.insession_enabled() is expected

    def test_max_children_clamped_to_the_guard_bounds(self) -> None:
        assert 1 <= insession.insession_max_children() <= 4

    def test_grant_path_mapping(self) -> None:
        request = "/w/b/subtasks_request.att-9.json"
        assert insession.grant_path_for_request(request) == "/w/b/subtasks_grant.att-9.json"
        with pytest.raises(ValueError):
            insession.grant_path_for_request("/w/b/other.json")


# ---------------------------------------------------------------------------
# Grant store
# ---------------------------------------------------------------------------


class TestGrantStore:
    def test_create_and_read_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        grant = _grant(db)
        loaded = insession.grant_for_attempt("att-1", db_path=db)
        assert loaded is not None
        assert loaded.id == grant.id
        assert loaded.max_children == 2
        assert loaded.children_spawned == 0
        assert [c["title"] for c in loaded.children] == ["part one", "part two"]

    def test_one_grant_per_attempt_ever(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        _grant(db)
        again, deny = insession.create_grant(
            swarm_run_id="run-1",
            board_task_id="task-att-1",
            attempt_id="att-1",
            session_id="ses-1",
            provider="claude",
            account_id="acct-1",
            children=CHILDREN,
            db_path=db,
            now=NOW,
        )
        assert again is None
        assert deny == "already_granted"

    def test_agent_capacity_denies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sessions inflight + live budgets + requested children ≤ ceiling."""
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db, "att-1", session_id="ses-1")
        _seed_attempt(db, "att-2", session_id="ses-2")
        monkeypatch.setattr(insession, "max_agents_per_account", lambda p: 3)
        _grant(db, "att-1", "ses-1")  # commits budget 2 ≤ 3
        denied, deny = insession.create_grant(
            swarm_run_id="run-1",
            board_task_id="task-att-2",
            attempt_id="att-2",
            session_id="ses-2",
            provider="claude",
            account_id="acct-1",
            children=CHILDREN,
            db_path=db,
            now=NOW,
        )
        assert denied is None
        assert deny == "agent_capacity"

    def test_no_account_denies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        grant, deny = insession.create_grant(
            swarm_run_id="run-1",
            board_task_id="task-att-1",
            attempt_id="att-1",
            session_id="ses-1",
            provider="claude",
            account_id=None,
            children=CHILDREN,
            db_path=db,
            now=NOW,
        )
        assert grant is None
        assert deny == "no_account"

    def test_consume_walks_the_budget_then_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        _grant(db)
        assert insession.consume_child_slot("ses-1", db_path=db, now=NOW) == (True, "granted")
        assert insession.consume_child_slot("ses-1", db_path=db, now=NOW) == (True, "granted")
        allowed, why = insession.consume_child_slot("ses-1", db_path=db, now=NOW)
        assert (allowed, why) == (False, "budget_exhausted")
        loaded = insession.grant_for_attempt("att-1", db_path=db)
        assert loaded is not None and loaded.children_spawned == 2

    def test_consume_requires_an_open_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A settled/killed attempt can never spawn — even before the void
        lands (the liveness join is inside the consume UPDATE itself)."""
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        _grant(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE swarm_attempts SET ended_at = '2026-07-27T12:01:00Z' WHERE id = 'att-1'"
        )
        conn.commit()
        conn.close()
        assert insession.consume_child_slot("ses-1", db_path=db, now=NOW) == (
            False,
            "attempt_ended",
        )

    def test_consume_respects_ttl_and_void(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        _grant(db)
        late = NOW + timedelta(seconds=insession.grant_ttl_seconds() + 1)
        assert insession.consume_child_slot("ses-1", db_path=db, now=late) == (
            False,
            "grant_expired",
        )
        assert insession.void_grant_for_attempt("att-1", "test", db_path=db) is True
        assert insession.void_grant_for_attempt("att-1", "test", db_path=db) is False
        assert insession.consume_child_slot("ses-1", db_path=db, now=NOW) == (
            False,
            "grant_voided",
        )

    def test_missing_table_fails_safe(self, tmp_path: Path) -> None:
        """A pre-081 database denies consumes and reports zero usage."""
        db = str(tmp_path / "old.db")
        sqlite3.connect(db).close()
        assert insession.consume_child_slot("ses-1", db_path=db) == (False, "no_grant")
        assert insession.live_children_total(db_path=db) == 0
        assert insession.live_grant_budget_total("claude", db_path=db) == 0

    def test_live_totals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        _grant(db)
        insession.consume_child_slot("ses-1", db_path=db, now=NOW)
        assert insession.live_grant_budget_total("claude", db_path=db, now=NOW) == 2
        assert insession.live_children_total(db_path=db, now=NOW) == 1
        insession.void_grant_for_attempt("att-1", "done", db_path=db)
        assert insession.live_grant_budget_total("claude", db_path=db, now=NOW) == 0
        assert insession.live_children_total(db_path=db, now=NOW) == 0


# ---------------------------------------------------------------------------
# Scheduler seams
# ---------------------------------------------------------------------------


def _make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    h = make_harness(tmp_path, [{"id": "a", "owned_paths": ["src/a"]}], max_concurrency=1)
    # Production topology: ONE database. The harness db is the default db.
    monkeypatch.setenv("OMNIAGENTOS_DB", h.db_path)
    scheduler = make_scheduler(h)
    return h, scheduler


def _open_attempt(h):
    task_row = h.task_row("a")
    task_id = str(task_row["id"])
    attempt = h.dal.open_attempt(
        h.run_id, task_id, provider="claude", model="sonnet", tier="simple", account_id="acct", source="test")
    return task_row, task_id, str(attempt["id"])


class TestLiveGrantScan:
    def test_grants_and_writes_the_grant_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        h, scheduler = _make(tmp_path, monkeypatch)
        task_row, task_id, attempt_id = _open_attempt(h)
        workbook_dir = _workbook_dir(h, task_id)
        _write_file(workbook_dir / f"subtasks_request.{attempt_id}.json", VALID_REQUEST)
        state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
        session = {"id": "ses-live", "provider": "claude", "account_id": "acct"}
        decided = scheduler._maybe_grant_insession(state, dict(task_row), attempt_id, session)
        assert decided is True
        grant = insession.grant_for_attempt(attempt_id, db_path=h.db_path)
        assert grant is not None and grant.session_id == "ses-live"
        grant_file = workbook_dir / f"subtasks_grant.{attempt_id}.json"
        assert grant_file.exists()
        body = json.loads(grant_file.read_text(encoding="utf-8"))
        assert body["grant_id"] == grant.id
        assert [c["title"] for c in body["children"]] == ["part one", "part two"]
        assert any(a == ACTION_SUBTASKS_GRANTED for a, _ in h.emitter.events)

    def test_nothing_written_keeps_watching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        h, scheduler = _make(tmp_path, monkeypatch)
        task_row, _task_id, attempt_id = _open_attempt(h)
        state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
        session = {"id": "ses-live", "provider": "claude", "account_id": "acct"}
        assert scheduler._maybe_grant_insession(state, dict(task_row), attempt_id, session) is False

    def test_flag_off_never_scans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "0")  # kill switch
        h, scheduler = _make(tmp_path, monkeypatch)
        task_row, task_id, attempt_id = _open_attempt(h)
        _write_file(
            _workbook_dir(h, task_id) / f"subtasks_request.{attempt_id}.json", VALID_REQUEST
        )
        state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
        session = {"id": "ses-live", "provider": "claude", "account_id": "acct"}
        assert scheduler._maybe_grant_insession(state, dict(task_row), attempt_id, session) is True
        assert insession.grant_for_attempt(attempt_id, db_path=h.db_path) is None

    def test_guard_denial_emits_insession_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        h, scheduler = _make(tmp_path, monkeypatch)
        task_row, task_id, attempt_id = _open_attempt(h)
        bad = dict(VALID_REQUEST)
        bad["subtasks"] = [VALID_REQUEST["subtasks"][0]]  # count guard: 1 < 2
        _write_file(_workbook_dir(h, task_id) / f"subtasks_request.{attempt_id}.json", bad)
        state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
        session = {"id": "ses-live", "provider": "claude", "account_id": "acct"}
        assert scheduler._maybe_grant_insession(state, dict(task_row), attempt_id, session) is True
        assert insession.grant_for_attempt(attempt_id, db_path=h.db_path) is None
        denials = [p for a, p in h.emitter.events if a == ACTION_SUBTASKS_DENIED]
        assert denials and denials[-1]["reason"] == "count"
        assert denials[-1]["mode"] == "insession"

    def test_non_claude_is_not_applicable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        h, scheduler = _make(tmp_path, monkeypatch)
        task_row, task_id, attempt_id = _open_attempt(h)
        _write_file(
            _workbook_dir(h, task_id) / f"subtasks_request.{attempt_id}.json", VALID_REQUEST
        )
        state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
        session = {"id": "ses-live", "provider": "kimi", "account_id": "acct"}
        assert scheduler._maybe_grant_insession(state, dict(task_row), attempt_id, session) is True
        assert insession.grant_for_attempt(attempt_id, db_path=h.db_path) is None


class TestSettleRule:
    def test_consumed_grant_skips_the_split_and_voids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The children already executed inside the attempt: settle must NOT
        register cards for the same work, and the grant releases its
        capacity."""
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        h, scheduler = _make(tmp_path, monkeypatch)
        _task_row, task_id, attempt_id = _open_attempt(h)
        _grant(h.db_path, attempt_id, "ses-live", now=None)  # real clock: consume below is too
        assert insession.consume_child_slot("ses-live", db_path=h.db_path) == (
            True,
            "granted",
        )
        _write_file(
            _workbook_dir(h, task_id) / f"subtasks_request.{attempt_id}.json", VALID_REQUEST
        )
        assert scheduler._insession_grant_consumed(attempt_id) is True
        grant = insession.grant_for_attempt(attempt_id, db_path=h.db_path)
        assert grant is not None
        assert grant.voided_at is not None and grant.void_reason == "settled_consumed"
        child_keys = [
            str((h.dal.get_swarm_json(t["id"]) or {}).get("task_key") or "")
            for t in h.dal.tasks_for_run(h.run_id)
        ]
        assert not any(key.startswith("a.") for key in child_keys)

    def test_unconsumed_grant_is_voided_and_split_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        h, scheduler = _make(tmp_path, monkeypatch)
        _task_row, _task_id, attempt_id = _open_attempt(h)
        _grant(h.db_path, attempt_id, "ses-live")
        assert scheduler._insession_grant_consumed(attempt_id) is False
        grant = insession.grant_for_attempt(attempt_id, db_path=h.db_path)
        assert grant is not None
        assert grant.void_reason == "settled_unconsumed"

    def test_close_attempt_voids_on_every_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h, _scheduler = _make(tmp_path, monkeypatch)
        _task_row, _task_id, attempt_id = _open_attempt(h)
        _grant(h.db_path, attempt_id, "ses-live")
        assert h.dal.close_attempt(attempt_id, "killed", "external terminalization")
        grant = insession.grant_for_attempt(attempt_id, db_path=h.db_path)
        assert grant is not None
        assert grant.voided_at is not None
        assert grant.void_reason == "attempt_closed:killed"


# ---------------------------------------------------------------------------
# Prompts + argv (dark guarantees)
# ---------------------------------------------------------------------------


class TestProtocolText:
    def test_default_variant_is_byte_identical(self) -> None:
        lines = subtasks_request_protocol_lines("/w/subtasks_request.a1.json")
        assert "children — you cannot spawn anything; the coordinator validates your" in lines
        assert not any("Task tool" in line for line in lines)

    def test_insession_variant_carries_grant_protocol(self) -> None:
        lines = subtasks_request_protocol_lines("/w/subtasks_request.a1.json", insession=True)
        text = "\n".join(lines)
        assert "/w/subtasks_grant.a1.json" in text
        assert "Task tool" in text
        assert "never" in text.lower() and "grant" in text.lower()
        # The classic end-your-attempt fallback must survive in the variant.
        assert "END this attempt" in text


def _stub_supervisor() -> sup.SessionSupervisor:
    s = sup.SessionSupervisor.__new__(sup.SessionSupervisor)
    s.claude_binary = "claude"
    return s


class TestArgvUnlock:
    def test_default_still_disallows_task(self) -> None:
        argv = sup.SessionSupervisor._bridge_launch_argv(
            _stub_supervisor(), "ref-1", "haiku", "prompt", None
        )
        assert argv[argv.index("--disallowedTools") + 1] == "Task"

    def test_fanout_launch_omits_the_disallow(self) -> None:
        argv = sup.SessionSupervisor._bridge_launch_argv(
            _stub_supervisor(), "ref-1", "haiku", "prompt", None, allow_task_fanout=True
        )
        assert "--disallowedTools" not in argv
        assert argv[argv.index("--model") + 1] == "haiku"

    def test_resume_never_unlocks(self) -> None:
        argv = sup.SessionSupervisor._bridge_resume_argv(
            _stub_supervisor(), "ref-1", "prompt", None, None
        )
        assert argv[argv.index("--disallowedTools") + 1] == "Task"


# ---------------------------------------------------------------------------
# Accounting surfaces
# ---------------------------------------------------------------------------


class TestAccounting:
    def test_fleet_available_reports_live_children(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.routing import limit_state

        db = _init_db(tmp_path, monkeypatch)
        _seed_attempt(db)
        _grant(db, now=None)  # real clock: the consume and snapshot below are too
        insession.consume_child_slot("ses-1", db_path=db)
        snapshot = limit_state.fleet_available(db_path=db)
        assert snapshot.insession_children_live == 1

    def test_preflight_reports_the_agents_ceiling_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.routing import fleet_preflight

        db = _init_db(tmp_path, monkeypatch)
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
        report = fleet_preflight.preflight(db_path=db)
        assert any(c.name == "provider.account_agents" for c in report.ceilings)

    def test_preflight_omits_the_row_when_killed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.routing import fleet_preflight

        db = _init_db(tmp_path, monkeypatch)
        monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "0")  # kill switch
        report = fleet_preflight.preflight(db_path=db)
        assert not any(c.name == "provider.account_agents" for c in report.ceilings)
