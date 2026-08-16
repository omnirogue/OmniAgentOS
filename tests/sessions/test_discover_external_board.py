"""Multi-provider process discovery → external sessions → Kanban cards."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import omniagentos.sessions.discover as discover_module
from omniagentos.collab.store import CollabStore
from omniagentos.sessions import agents_view
from omniagentos.sessions.dal import SessionsDal, SessionState
from omniagentos.sessions.discover import (
    EXTPROC_REF_PREFIX,
    DiscoveredProcess,
    classify_command,
    list_discovered_processes,
)
from omniagentos.sessions.external_board import (
    reset_external_board_throttle,
    sync_external_sessions_to_board,
)


@pytest.fixture(autouse=True)
def _no_live_agent_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the board-sync path hermetic in the offline lane.

    ``sync_external_sessions_to_board`` enriches Claude rows via
    ``agents_view.collect_all``, which spawns the ``claude`` provider CLI once
    per profile. In the default (OFFLINE) pytest lane the conftest guard refuses
    a real provider-CLI spawn with ``OfflineLaneViolation``. Discovery/board
    projection is not what these tests exercise, so default the collector to an
    empty result; the enrollment tests below override it with their own map.
    """
    monkeypatch.setattr(agents_view, "collect_all", lambda: {})


# ---------------------------------------------------------------------------
# classify_command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "provider"),
    [
        ("claude", "claude"),
        ("/Users/youruser/.local/bin/claude", "claude"),
        ("grok", "grok"),
        ("kimi-code ", "kimi"),
        ("/Users/youruser/.local/bin/qwen", "qwen"),
        ("node /Users/x/.nvm/versions/node/v22/bin/gemini", "gemini"),
        (
            "/Users/x/lib/node_modules/@openai/codex/node_modules/"
            "@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex",
            "codex",
        ),
        ("node /Users/x/.nvm/versions/node/v22/bin/codex", "codex"),
        ("aider", "aider"),
        ("cursor-agent", "cursor"),
    ],
)
def test_classify_known_provider_commands(command: str, provider: str) -> None:
    assert classify_command(command) == provider


@pytest.mark.parametrize(
    "command",
    [
        "uvicorn omniagentos.api:app",
        "pytest -q",
        "ssh -v agentproacademy.com",
        "tail -f /tmp/claude-501/tasks/x.output",
        "rg -i claude|codex",
        "node /Users/x/app/node_modules/.bin/next dev",
        "",
    ],
)
def test_classify_excludes_noise(command: str) -> None:
    assert classify_command(command) is None


def test_process_discovery_requests_untruncated_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    marker = "x" * 320 + " /usr/local/bin/claude"

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.extend(argv)
        return SimpleNamespace(returncode=0, stdout=f" 4242 ttys001 {marker}\n")

    monkeypatch.setattr(discover_module.subprocess, "run", fake_run)

    found = list_discovered_processes(include_cwd=False)

    assert captured == ["ps", "-ww", "-axo", "pid=,tty=,command="]
    assert [(process.pid, process.provider) for process in found] == [(4242, "claude")]


def test_list_discovered_dedupes_node_wrapper_and_binary() -> None:
    ps_text = "\n".join(
        [
            " 111 ttys001 node /Users/x/.nvm/bin/codex",
            " 112 ttys001 /Users/x/vendor/bin/codex",
            " 200 ttys002 claude",
            " 300 ??     uvicorn omniagentos.api:app --port 8485",
        ]
    )
    found = list_discovered_processes(
        ps_runner=lambda: ps_text,
        include_cwd=False,
    )
    by_tty = {(p.provider, p.tty): p for p in found}
    assert ("codex", "ttys001") in by_tty
    # Prefer real binary over node wrapper
    assert by_tty[("codex", "ttys001")].pid == 112
    assert ("claude", "ttys002") in by_tty
    assert all(p.provider != "uvicorn" for p in found)
    assert all(p.session_ref.startswith(EXTPROC_REF_PREFIX) for p in found)


# ---------------------------------------------------------------------------
# board projection
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline_agent_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Agent View collector shells `claude agents --json`, which the
    merge-gate's offline lane forbids (provider CLI spawn). Default to an
    empty registry; tests that exercise enrollment monkeypatch their own."""
    monkeypatch.setattr(agents_view, "collect_all", lambda: {})


@pytest.fixture()
def stores(tmp_path: Path):
    db = str(tmp_path / "test.db")
    # CollabStore + SessionsDal each migrate the shared schema on open.
    collab = CollabStore(db)
    dal = SessionsDal(db)
    reset_external_board_throttle()
    yield dal, collab
    try:
        dal.close()
    except Exception:
        pass


def test_sync_creates_external_session_and_board_card(stores) -> None:
    dal, collab = stores
    procs = [
        DiscoveredProcess(
            pid=4242,
            provider="gemini",
            command="node /bin/gemini",
            tty="ttys013",
            cwd="/Users/youruser/work",
        ),
        DiscoveredProcess(
            pid=5252,
            provider="claude",
            command="claude",
            tty="ttys021",
            cwd="/Users/youruser",
        ),
    ]
    stats = sync_external_sessions_to_board(
        dal,
        collab,
        discovered=procs,
        force=True,
        db_key="test-db",
    )
    assert stats["skipped"] is False
    assert stats["discovered"] == 2
    assert stats["sessions_created"] == 2
    assert stats["cards_created"] == 2

    live = dal.list_live_sessions()
    assert len(live) == 2
    providers = {s["provider"] for s in live}
    assert providers == {"gemini", "claude"}
    assert all(s["source"] == "external" for s in live)
    assert all(str(s["session_ref"]).startswith(EXTPROC_REF_PREFIX) for s in live)

    board = collab.list_board_tasks(archived=0)
    refs = {t.get("result_ref") for t in board}
    assert {s["id"] for s in live} <= refs
    assert all(t["status"] == "in_progress" for t in board if t.get("result_ref") in refs)

    # F3: under the default (empty) collect_all stub the Claude row must carry NO
    # agent-view enrichment -- unset, not "". A writer bug stamping agent_name=""
    # (or any empty-string default) on create would fail here.
    claude_row = dal.get_session_by_pid(5252)
    assert claude_row is not None
    assert claude_row["provider"] == "claude"
    assert claude_row["agent_name"] is None
    assert claude_row["agent_status"] is None
    assert claude_row["agent_profile"] is None
    assert claude_row["agent_session_id"] is None


def test_sync_enrolls_matching_claude_agent_view_pid(
    monkeypatch: pytest.MonkeyPatch, stores
) -> None:
    dal, collab = stores
    monkeypatch.setattr(
        agents_view,
        "collect_all",
        lambda: {
            4242: {
                "pid": 4242,
                "name": "Agent View Name",
                "status": "working",
                "profile": ".claude-account-3",
                "sessionId": "claude-session-123",
            }
        },
    )
    sync_external_sessions_to_board(
        dal,
        collab,
        discovered=[
            DiscoveredProcess(
                pid=4242, provider="claude", command="claude", tty="ttys001", cwd="/tmp/project"
            )
        ],
        force=True,
        db_key="agent-view",
    )
    row = dal.get_session_by_pid(4242)
    assert row is not None
    assert {
        key: row[key]
        for key in ("agent_name", "agent_status", "agent_profile", "agent_session_id")
    } == {
        "agent_name": "Agent View Name",
        "agent_status": "working",
        "agent_profile": ".claude-account-3",
        "agent_session_id": "claude-session-123",
    }


def test_sync_idempotent_does_not_duplicate(stores) -> None:
    dal, collab = stores
    procs = [
        DiscoveredProcess(
            pid=9001,
            provider="grok",
            command="grok",
            tty="ttys008",
            cwd="/Users/youruser",
        )
    ]
    s1 = sync_external_sessions_to_board(dal, collab, discovered=procs, force=True, db_key="idemp")
    s2 = sync_external_sessions_to_board(dal, collab, discovered=procs, force=True, db_key="idemp")
    assert s1["sessions_created"] == 1
    assert s2["sessions_created"] == 0
    assert s2["sessions_refreshed"] == 1
    assert len(dal.list_live_sessions()) == 1
    assert len(collab.list_board_tasks(archived=0)) == 1


def test_sync_restores_each_connection_busy_timeout(stores, tmp_path: Path) -> None:
    dal, collab = stores
    untouched = sqlite3.connect(tmp_path / "test.db")
    try:
        dal._connection.execute("PRAGMA busy_timeout=1234")
        collab._connection.execute("PRAGMA busy_timeout=2345")
        untouched.execute("PRAGMA busy_timeout=3456")

        sync_external_sessions_to_board(
            dal,
            collab,
            discovered=[],
            force=True,
            db_key="busy-timeout-restore",
        )

        assert dal._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert collab._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 2345
        assert untouched.execute("PRAGMA busy_timeout").fetchone()[0] == 3456
    finally:
        untouched.close()


def test_sync_does_not_duplicate_bridge_session_same_pid(stores) -> None:
    dal, collab = stores
    from omniagentos.contracts import new_id, utc_now_iso

    now = utc_now_iso()
    sid = new_id("ses")
    dal.create_session(
        {
            "id": sid,
            "source": "bridge",
            "project_dir": "/tmp/bridge-proj",
            "provider": "claude",
            "session_ref": "bridge-ref",
            "state": SessionState.RUNNING.value,
            "pid": 7777,
            "title": "bridge session",
            "budget_usd_max": None,
            "cost_usd": 0.0,
            "kill_requested": 0,
            "last_activity_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    procs = [
        DiscoveredProcess(
            pid=7777,
            provider="claude",
            command="claude",
            tty="ttys099",
            cwd="/tmp/bridge-proj",
        )
    ]
    stats = sync_external_sessions_to_board(
        dal, collab, discovered=procs, force=True, db_key="bridge-pid"
    )
    assert stats["sessions_created"] == 0
    live = dal.list_live_sessions()
    assert len(live) == 1
    assert live[0]["source"] == "bridge"
    # Still ensures a board card for the bridge orphan
    assert stats["cards_created"] == 1
    board = collab.list_board_tasks(archived=0)
    assert any(t.get("result_ref") == sid for t in board)


def test_sync_closes_extproc_when_process_gone(stores) -> None:
    dal, collab = stores
    procs = [
        # Must be outside the live pid range: this test asserts the process is GONE, and
        # the close path consults the real process table. A pid that CAN be live makes the
        # assertion depend on what else is running -- 61000 refused an innocent candidate
        # at the merge gate on 2026-08-10 because the gate itself was pid 61000.
        DiscoveredProcess(
            pid=161000,
            provider="kimi",
            command="kimi-code",
            tty="ttys006",
            cwd="/Users/youruser",
        )
    ]
    sync_external_sessions_to_board(dal, collab, discovered=procs, force=True, db_key="close-me")
    assert len(dal.list_live_sessions()) == 1
    # Process gone → empty discovery
    stats = sync_external_sessions_to_board(
        dal, collab, discovered=[], force=True, db_key="close-me"
    )
    assert stats["sessions_closed"] == 1
    assert dal.list_live_sessions() == []


# ---------------------------------------------------------------------------
# Unknown cost must be NULL at the WRITER, not 0.0
# ---------------------------------------------------------------------------


def test_external_session_unknown_cost_is_null_not_zero(stores) -> None:
    """Decisive: create external session with no usage → cost_usd IS NULL.

    External process discovery has no token/cost report. Writing 0.0 would
    render as $0.00 on the dashboard and look like a free 16h Claude CLI.
    Follow usage_capture: unknown stays NULL, never a silent $0.00.
    """
    dal, collab = stores
    procs = [
        DiscoveredProcess(
            pid=161616,
            provider="claude",
            command="claude",
            tty="ttys042",
            cwd="/Users/youruser/work",
        )
    ]
    stats = sync_external_sessions_to_board(
        dal, collab, discovered=procs, force=True, db_key="cost-null"
    )
    assert stats["sessions_created"] == 1

    live = dal.list_live_sessions()
    assert len(live) == 1
    row = live[0]
    assert row["source"] == "external"
    # No usage was reported at discovery time.
    assert row.get("input_tokens") is None
    assert row.get("output_tokens") is None
    assert row.get("usage_source") is None

    # Decisive: unknown cost is SQL NULL, not a false free claim.
    assert row["cost_usd"] is None
    assert row["cost_usd"] != 0.0
    # Counterfeit that must fail if the writer reintroduces 0.0 for unknown.
    assert not (row["cost_usd"] == 0.0 and row.get("usage_source") is None)
    # Budget coercion still works (None → 0.0 for caps only).
    assert float(row.get("cost_usd") or 0.0) == 0.0


def test_external_session_cost_null_persists_after_refresh(stores) -> None:
    """Refresh must not re-seed cost_usd=0.0 onto an existing NULL row."""
    dal, collab = stores
    procs = [
        DiscoveredProcess(
            pid=171717,
            provider="grok",
            command="grok",
            tty="ttys043",
            cwd="/Users/youruser",
        )
    ]
    sync_external_sessions_to_board(
        dal, collab, discovered=procs, force=True, db_key="cost-refresh"
    )
    first = dal.list_live_sessions()[0]
    assert first["cost_usd"] is None
    assert first["cost_usd"] != 0.0

    sync_external_sessions_to_board(
        dal, collab, discovered=procs, force=True, db_key="cost-refresh"
    )
    again = dal.get_session(str(first["id"]))
    assert again is not None
    assert again["cost_usd"] is None
    assert again["cost_usd"] != 0.0


def test_refresh_preserves_agent_view_on_transient_collector_failure(
    monkeypatch: pytest.MonkeyPatch, stores
) -> None:
    """A collector miss (timeout/empty) must never null-overwrite stored fields."""
    dal, collab = stores
    proc = DiscoveredProcess(
        pid=5151, provider="claude", command="claude", tty="ttys002", cwd="/tmp/project"
    )
    monkeypatch.setattr(
        agents_view,
        "collect_all",
        lambda: {
            5151: {
                "pid": 5151,
                "name": "Persisted Name",
                "status": "busy",
                "profile": ".claude-account-2",
                "sessionId": "sess-abc",
            }
        },
    )
    sync_external_sessions_to_board(
        dal, collab, discovered=[proc], force=True, db_key="refresh-keep"
    )
    # Second sync hits the refresh branch with an EMPTY collector result,
    # indistinguishable from a transient failure.
    monkeypatch.setattr(agents_view, "collect_all", lambda: {})
    sync_external_sessions_to_board(
        dal, collab, discovered=[proc], force=True, db_key="refresh-keep"
    )
    row = dal.get_session_by_pid(5151)
    assert row is not None
    assert row["agent_name"] == "Persisted Name"
    assert row["agent_status"] == "busy"
    assert row["agent_profile"] == ".claude-account-2"
    assert row["agent_session_id"] == "sess-abc"


def test_refresh_updates_agent_view_when_collector_has_data(
    monkeypatch: pytest.MonkeyPatch, stores
) -> None:
    dal, collab = stores
    proc = DiscoveredProcess(
        pid=6161, provider="claude", command="claude", tty="ttys003", cwd="/tmp/project"
    )
    monkeypatch.setattr(
        agents_view,
        "collect_all",
        lambda: {6161: {"pid": 6161, "name": "First", "status": "busy", "profile": "p"}},
    )
    sync_external_sessions_to_board(
        dal, collab, discovered=[proc], force=True, db_key="refresh-upd"
    )
    monkeypatch.setattr(
        agents_view,
        "collect_all",
        lambda: {6161: {"pid": 6161, "name": "Renamed", "status": "idle", "profile": "p"}},
    )
    sync_external_sessions_to_board(
        dal, collab, discovered=[proc], force=True, db_key="refresh-upd"
    )
    row = dal.get_session_by_pid(6161)
    assert row is not None
    assert row["agent_name"] == "Renamed"
    assert row["agent_status"] == "idle"


def test_agent_view_never_attaches_to_non_claude_provider(
    monkeypatch: pytest.MonkeyPatch, stores
) -> None:
    """A recycled OS pid colliding with the Claude map must not cross providers."""
    dal, collab = stores
    monkeypatch.setattr(
        agents_view,
        "collect_all",
        lambda: {7171: {"pid": 7171, "name": "Claude Thing", "status": "busy", "profile": "p"}},
    )
    sync_external_sessions_to_board(
        dal,
        collab,
        discovered=[
            DiscoveredProcess(
                pid=7171, provider="gemini", command="gemini", tty="ttys004", cwd="/tmp/x"
            )
        ],
        force=True,
        db_key="provider-guard",
    )
    row = dal.get_session_by_pid(7171)
    assert row is not None
    assert row["agent_name"] is None
    assert row["agent_status"] is None
