import json
import sqlite3
from pathlib import Path

from omniagentos.fleetcap import extract as extract_module
from omniagentos.fleetcap.extract import extract
from omniagentos.fleetcap.profiles import Profile


def _schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, cli TEXT, account TEXT, cwd TEXT, agent TEXT,
        models TEXT, start_ts REAL, end_ts REAL, wall_s REAL, active_s REAL,
        model_s REAL, tool_s REAL, human_s REAL, n_user INTEGER, n_assistant INTEGER,
        n_tool INTEGER, n_err INTEGER, n_compact INTEGER, tokens_in INTEGER,
        tokens_out INTEGER, tokens_cached INTEGER, tools TEXT, events TEXT, outcome TEXT,
        capture_method TEXT, created_ts REAL)"""
    )


def _local_profile(monkeypatch, root: Path, cli: str = "claude") -> None:
    monkeypatch.setattr(
        extract_module,
        "existing_profiles",
        lambda: [Profile(cli, "default", root, ("**/*.jsonl",))],
    )


def test_extract_populates_device_and_dispatch(
    tmp_path: Path, real_connection: sqlite3.Connection, monkeypatch
) -> None:
    transcript = tmp_path / "fixtures/claude/default/session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "timestamp": "2026-08-13T10:00:00Z",
            "cwd": "/repo",
            "message": {"content": "build it"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-13T10:00:05Z",
            "message": {"model": "claude-test", "content": "done"},
        },
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices": []}')
    _local_profile(monkeypatch, transcript.parent)
    connection = real_connection
    count = extract(
        connection,
        sources=None,
        ingest_root=tmp_path / "ingest",
        spool=tmp_path / "spool",
        config=config,
        since_days=3650,
    )
    assert count == 1
    row = connection.execute(
        "SELECT session_id, device, dispatch_class, dispatcher, agent FROM sessions "
        "WHERE session_id='session-1'"
    ).fetchone()
    assert row == (
        "session-1",
        "mac-studio",
        "human",
        "emp_owner",
        None,
    )


def test_firm_outcome_is_never_downgraded(
    tmp_path: Path, real_connection: sqlite3.Connection, monkeypatch
) -> None:
    connection = real_connection
    connection.execute(
        "INSERT INTO sessions (session_id, cli, outcome, created_ts) VALUES (?, 'claude', 'success', ?)",
        ("session-1", 1.0),
    )
    # Seed must be committed: _write_row now runs through the busy seam, whose
    # defensive rollback-before-BEGIN discards any uncommitted setup state.
    connection.commit()
    transcript = tmp_path / "fixtures/claude/default/session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"user","timestamp":"2026-08-13T10:00:00Z","message":{"content":"x"}}\n'
    )
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices": []}')
    _local_profile(monkeypatch, transcript.parent)
    extract(
        connection,
        sources=None,
        ingest_root=tmp_path / "ingest",
        spool=tmp_path / "spool",
        config=config,
        since_days=3650,
    )
    assert (
        connection.execute("SELECT outcome FROM sessions WHERE session_id='session-1'").fetchone()[
            0
        ]
        == "success"
    )


def test_grok_is_an_explicit_estimated_observation(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    connection = real_connection
    signal = tmp_path / "fixtures/grok/default/activity.jsonl"
    signal.parent.mkdir(parents=True)
    signal.write_text("{}\n")
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices": []}')
    assert (
        extract(
            connection,
            sources=[tmp_path / "fixtures"],
            ingest_root=tmp_path / "ingest",
            spool=tmp_path / "spool",
            config=config,
            since_days=1,
        )
        == 1
    )
    row = connection.execute(
        "SELECT session_id, capture_method, outcome, dispatch_class FROM sessions "
        "WHERE session_id='estimated-v2:unknown:grok:default'"
    ).fetchone()
    assert row[0] == "estimated-v2:unknown:grok:default"
    assert row[1:] == ("estimated-v2", "unknown?", "unknown")


def test_estimator_updates_one_stable_row(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    signal = tmp_path / "fixtures/grok/default/activity.jsonl"
    signal.parent.mkdir(parents=True)
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices":[]}')
    for sweep in range(6):
        signal.write_text("{}\n" * (sweep + 1))
        extract(
            real_connection,
            sources=[tmp_path / "fixtures"],
            ingest_root=tmp_path / "ingest",
            spool=tmp_path / "spool",
            config=config,
            since_days=1,
        )
    count = real_connection.execute(
        "SELECT count(*) FROM sessions WHERE session_id='estimated-v2:unknown:grok:default'"
    ).fetchone()[0]
    assert count == 1


def test_hub_local_upgrades_legacy_row_preserving_firm_outcome(
    tmp_path: Path, real_connection: sqlite3.Connection, monkeypatch
) -> None:
    real_connection.execute(
        "INSERT OR REPLACE INTO sessions "
        "(session_id,cli,outcome,created_ts,tokens_in,wall_s) VALUES (?,?,?,?,?,?)",
        ("legacy-session", "claude", "success", 1.0, 123456, 99),
    )
    real_connection.commit()  # busy-seam rollback-before-BEGIN discards uncommitted seeds
    transcript = tmp_path / "fixtures/claude/default/legacy-session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"user","timestamp":"2026-08-13T10:00:00Z","cwd":"/repo",'
        '"message":{"content":"work"}}\n'
    )
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices":[]}')
    _local_profile(monkeypatch, transcript.parent)
    for _ in range(2):
        extract(
            real_connection,
            sources=None,
            ingest_root=tmp_path / "ingest",
            spool=tmp_path / "spool",
            config=config,
            since_days=3650,
        )
    rows = real_connection.execute(
        "SELECT outcome,device,dispatcher,tokens_in,wall_s FROM sessions "
        "WHERE session_id='legacy-session'"
    ).fetchall()
    assert rows == [("success", "mac-studio", "emp_owner", 123456, 99)]


def test_hub_local_refreshes_metrics_measured_by_each_sweep(
    tmp_path: Path, real_connection: sqlite3.Connection, monkeypatch
) -> None:
    real_connection.execute(
        "INSERT OR REPLACE INTO sessions "
        "(session_id,cli,outcome,created_ts,tokens_in,wall_s) VALUES (?,?,?,?,?,?)",
        ("growing", "claude", "success", 1.0, 999999, 99),
    )
    real_connection.commit()  # busy-seam rollback-before-BEGIN discards uncommitted seeds
    transcript = tmp_path / "fixtures/claude/default/growing.jsonl"
    transcript.parent.mkdir(parents=True)
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices":[]}')
    _local_profile(monkeypatch, transcript.parent)

    def sweep(turns: int) -> tuple[object, ...]:
        records = []
        for index in range(turns):
            base = 1786600000 + index * 600
            records.extend(
                [
                    {"type": "user", "timestamp": base, "message": {"content": "go"}},
                    {
                        "type": "assistant",
                        "timestamp": base + 30,
                        "message": {
                            "model": "m",
                            "content": [{"type": "tool_use", "id": f"t{index}", "name": "Bash"}],
                        },
                    },
                    {
                        "type": "user",
                        "timestamp": base + 90,
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": f"t{index}",
                                    "content": "ok",
                                }
                            ]
                        },
                    },
                ]
            )
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        extract(
            real_connection,
            sources=None,
            ingest_root=tmp_path / "ingest",
            spool=tmp_path / "spool",
            config=config,
            since_days=36500,
        )
        return real_connection.execute(
            "SELECT n_user,n_tool,model_s,tool_s,end_ts,tokens_in,wall_s "
            "FROM sessions WHERE session_id='growing'"
        ).fetchone()

    first = sweep(1)
    second = sweep(12)
    assert second[:5] != first[:5]
    assert second[0] == second[1] == 12
    assert second[5:] == (999999, 99)


def test_fresh_hub_claude_insert_keeps_unmeasured_tokens_null(
    tmp_path: Path, real_connection: sqlite3.Connection, monkeypatch
) -> None:
    transcript = tmp_path / "local/new-local.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"user","timestamp":"2026-08-13T10:00:00Z","message":{"content":"work"}}\n'
    )
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices":[]}')
    _local_profile(monkeypatch, transcript.parent)
    extract(
        real_connection,
        sources=None,
        ingest_root=tmp_path / "ingest",
        spool=tmp_path / "spool",
        config=config,
        since_days=3650,
    )
    assert real_connection.execute(
        "SELECT tokens_in,tokens_out,tokens_cached FROM sessions WHERE session_id='new-local'"
    ).fetchone() == (None, None, None)


def test_hub_kimi_refreshes_measured_tokens(
    tmp_path: Path, real_connection: sqlite3.Connection, monkeypatch
) -> None:
    real_connection.execute(
        "INSERT INTO sessions (session_id,cli,tokens_in,tokens_out,created_ts) "
        "VALUES ('kimi-local','kimi',1,2,1)"
    )
    real_connection.commit()  # busy-seam rollback-before-BEGIN discards uncommitted seeds
    transcript = tmp_path / "local/kimi-local.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"turn.prompt","time":1786600000}\n'
        '{"type":"usage.record","time":1786600001,'
        '"usage":{"input_tokens":100,"output_tokens":50}}\n'
    )
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices":[]}')
    _local_profile(monkeypatch, transcript.parent, cli="kimi")
    extract(
        real_connection,
        sources=None,
        ingest_root=tmp_path / "ingest",
        spool=tmp_path / "spool",
        config=config,
        since_days=36500,
    )
    assert real_connection.execute(
        "SELECT tokens_in,tokens_out FROM sessions WHERE session_id='kimi-local'"
    ).fetchone() == (100, 50)


def test_unlocatable_source_stays_unknown(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    transcript = tmp_path / "foreign/claude/default/foreign-session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"user","timestamp":"2026-08-13T10:00:00Z","message":{"content":"work"}}\n'
    )
    config = tmp_path / "devices.yaml"
    config.write_text('{"hub":{"device":"mac-studio","owner":"emp_owner"},"devices":[]}')
    extract(
        real_connection,
        sources=[tmp_path / "foreign"],
        ingest_root=tmp_path / "ingest",
        spool=tmp_path / "spool",
        config=config,
        since_days=3650,
    )
    assert real_connection.execute(
        "SELECT session_id,device,device_owner,dispatch_class FROM sessions "
        "WHERE session_id LIKE 'native:unknown:%foreign-session'"
    ).fetchone() == ("native:unknown:claude:default:foreign-session", "unknown", None, "unknown")
