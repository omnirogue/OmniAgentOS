"""Black-box tests for the read-only interrupted-task discovery index."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCRIPT = REPO_ROOT / "scripts" / "task-resume-index.py"


def _fixture_script(tmp_path: Path) -> Path:
    script = tmp_path / "scripts" / "task-resume-index.py"
    script.parent.mkdir(parents=True)
    script.write_text(SOURCE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    return script


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def resume_fixture(tmp_path: Path) -> tuple[Path, Path]:
    script = _fixture_script(tmp_path)
    db_path = tmp_path / "fixture.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE board_tasks (id TEXT, title TEXT, status TEXT, updated_at TEXT, archived_at TEXT);
        CREATE TABLE tasks (id TEXT, title TEXT, state TEXT, updated_at TEXT);
        CREATE TABLE task_sessions (id TEXT, board_task_id TEXT, seq INTEGER, harness TEXT, model TEXT, started_at TEXT, ended_at TEXT);
        CREATE TABLE swarm_runs (id TEXT);
    """)
    conn.executemany(
        "INSERT INTO board_tasks VALUES (?, ?, ?, ?, ?)",
        [
            ("board-old", "Old board task", "claimed", "2026-08-08T10:00:00Z", None),
            ("board-live", "Live board task", "claimed", "2026-08-08T12:00:00Z", None),
            ("board-new", "New board task", "open", "2026-08-08T13:00:00Z", None),
            ("board-done", "Done board task", "done", "2026-08-08T14:00:00Z", None),
            ("board-archived", "Archived", "open", "2026-08-08T15:00:00Z", "2026-08-08T15:01:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, ?)",
        [
            ("task-old", "Old control task", "running", "2026-08-08T09:00:00Z"),
            ("task-running", "Running control task", "running", "2026-08-08T11:00:00Z"),
            ("task-new", "New control task", "paused", "2026-08-08T13:00:00Z"),
            ("task-completed", "Completed control task", "completed", "2026-08-08T14:00:00Z"),
            ("task-cancelled", "Cancelled control task", "cancelled", "2026-08-08T15:00:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO task_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("session-live", "board-live", 2, "cli-codex", "gpt-5", "2026-08-08T10:00:00Z", None),
            (
                "session-ended",
                "board-live",
                1,
                "cli-codex",
                "gpt-5",
                "2026-08-08T09:00:00Z",
                "2026-08-08T10:00:00Z",
            ),
        ],
    )
    conn.execute("INSERT INTO swarm_runs VALUES ('run-resume')")
    conn.commit()
    conn.close()
    resumed = tmp_path / "var" / "swarm" / "run-resume" / "task-resume"
    resumed.mkdir(parents=True)
    (resumed / "TASK.md").write_text("# Task\n", encoding="utf-8")
    (resumed / "WORKBOOK.md").write_text(
        'checkpoint\n```resume\n{"resume_v": 1}\n```\n', encoding="utf-8"
    )
    nested = resumed / ".fusion"
    nested.mkdir()
    (nested / "TASK.md").write_text("# nested\n", encoding="utf-8")
    plain = tmp_path / "var" / "swarm" / "namespace-noise" / "task-plain"
    plain.mkdir(parents=True)
    (plain / "WORKBOOK.md").write_text("No resume fence here.\n", encoding="utf-8")
    ledger = tmp_path / "var" / "loopqueue" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "\n".join(
            [
                json.dumps({"role": "planning", "event": "proposed"}),
                json.dumps({"role": "planner", "event": "instrument_error"}),
                json.dumps({"role": "repair", "event": "rejected"}),
                json.dumps({"role": "integration", "event": "found"}),
                json.dumps({"role": "ignored", "event": "unrelated"}),
                '{"role":"planner","event":"proposed"',
            ]
        ),
        encoding="utf-8",
    )
    return script, db_path


def test_json_index_lists_sources_ranked_rows_and_nested_contracts(
    resume_fixture: tuple[Path, Path],
) -> None:
    script, db_path = resume_fixture
    result = _run(script, "--db", str(db_path), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert {"generated_at", "repo_root", "sources", "notes", "unmatched_dirs"} <= set(payload)
    # A live session ranks board-live above two newer/older rows, and all three prove reverse sort coverage.
    assert [row["id"] for row in payload["board_tasks"]] == ["board-live", "board-new", "board-old"]
    assert [row["id"] for row in payload["tasks"]] == ["task-new", "task-running", "task-old"]
    assert payload["sources"]["db"]["status"] == "ok"
    assert payload["sources"]["ledger"]["lines_unparseable"] == 1
    assert {row["dir"].rsplit("/", 1)[-1] for row in payload["swarm_docs"]} >= {
        "task-resume",
        ".fusion",
    }
    assert all(row["run_known"] is True for row in payload["swarm_docs"])
    assert payload["unmatched_dirs"][0]["run_known"] is False
    resume = next(row for row in payload["swarm_docs"] if row["dir"].endswith("task-resume"))
    assert resume["resume_block_v1"] is True
    assert "tail_last_16384" in resume["resume_probe"]


def test_missing_db_has_machine_and_human_absence_receipts(tmp_path: Path) -> None:
    script = _fixture_script(tmp_path)
    result = _run(script, "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["board_tasks"] == []
    assert payload["sources"]["db"]["status"] == "missing"
    assert payload["notes"] and "Database not found" in payload["notes"][0]
    human = _run(script)
    assert "Database not found" in human.stdout


def test_absent_table_is_not_laundered_to_empty(tmp_path: Path) -> None:
    script = _fixture_script(tmp_path)
    db = tmp_path / "partial.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, state TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()
    payload = json.loads(_run(script, "--db", str(db), "--json").stdout)
    assert payload["sources"]["board_tasks"]["status"] == "table_absent"
    assert "not found" in payload["sources"]["board_tasks"]["detail"].lower()
    assert "not found" in _run(script, "--db", str(db)).stdout.lower()


def test_existing_invalid_db_exits_one(tmp_path: Path) -> None:
    script = _fixture_script(tmp_path)
    invalid = tmp_path / "not-a-database.sqlite3"
    invalid.write_text("not sqlite", encoding="utf-8")
    result = _run(script, "--db", str(invalid), "--json")
    assert result.returncode == 1
    assert "Unable to open database read-only" in result.stderr


def test_full_run_never_mutates_its_input_directories(resume_fixture: tuple[Path, Path]) -> None:
    script, db_path = resume_fixture
    roots = [
        script.parent,
        db_path.parent,
        script.parent.parent / "var" / "swarm",
        script.parent.parent / "var" / "loopqueue",
    ]
    before = {root: sorted(path.name for path in root.iterdir()) for root in roots}
    result = _run(script, "--db", str(db_path), "--role-stats", "--json")
    after = {root: sorted(path.name for path in root.iterdir()) for root in roots}
    assert result.returncode == 0, result.stderr
    assert after == before


def test_wal_reads_leave_new_sidecars_in_place_and_report_them(tmp_path: Path) -> None:
    script = _fixture_script(tmp_path)
    db = tmp_path / "wal.sqlite3"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=wal")
    writer.executescript(
        "CREATE TABLE board_tasks (id TEXT, title TEXT, status TEXT, updated_at TEXT); CREATE TABLE tasks (id TEXT, title TEXT, state TEXT, updated_at TEXT); CREATE TABLE task_sessions (id TEXT, board_task_id TEXT, seq INTEGER, harness TEXT, model TEXT, started_at TEXT, ended_at TEXT);"
    )
    writer.execute("INSERT INTO board_tasks VALUES ('b', 'x', 'open', '2026-01-01T00:00:00Z')")
    writer.commit()
    before_live = sorted(path.name for path in tmp_path.iterdir())
    assert _run(script, "--db", str(db), "--json").returncode == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == before_live
    writer.close()
    before_closed = sorted(path.name for path in tmp_path.iterdir())
    result = _run(script, "--db", str(db), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    after_closed = sorted(path.name for path in tmp_path.iterdir())
    if after_closed != before_closed:
        assert "read created wal-index sidecars (-wal/-shm)" in payload["sources"]["db"]["detail"]
        assert {"wal.sqlite3-wal", "wal.sqlite3-shm"} <= set(after_closed)


def test_wal_reader_never_unlinks_concurrent_writer_sidecars(tmp_path: Path, monkeypatch) -> None:
    """A sidecar that appears after the reader snapshot belongs to its writer, not us."""
    spec = importlib.util.spec_from_file_location("task_resume_index_under_test", SOURCE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db = tmp_path / "concurrent.sqlite3"
    setup = sqlite3.connect(db)
    setup.executescript("""
        PRAGMA journal_mode=wal;
        CREATE TABLE board_tasks (id TEXT, title TEXT, status TEXT, updated_at TEXT, archived_at TEXT);
        CREATE TABLE tasks (id TEXT, title TEXT, state TEXT, updated_at TEXT);
        CREATE TABLE task_sessions (id TEXT, board_task_id TEXT, seq INTEGER, harness TEXT, model TEXT, started_at TEXT, ended_at TEXT);
    """)
    setup.commit()
    setup.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    setup.close()
    sidecars = [Path(f"{db}{suffix}") for suffix in ("-wal", "-shm")]
    assert not any(path.exists() for path in sidecars)

    committed, release_writer = threading.Event(), threading.Event()

    def writer() -> None:
        connection = sqlite3.connect(db, isolation_level=None)
        connection.execute("PRAGMA journal_mode=wal")
        connection.execute(
            "INSERT INTO board_tasks VALUES ('writer-live', 'live', 'open', '2026-08-08T10:00:00Z', NULL)"
        )
        committed.set()
        assert release_writer.wait(timeout=10)
        connection.close()

    original_open = module._open_db

    def open_after_writer(path: Path, immutable: bool = False):
        thread = threading.Thread(target=writer)
        thread.start()
        assert committed.wait(timeout=10)
        module._concurrent_writer_thread = thread
        return original_open(path, immutable)

    monkeypatch.setattr(module, "_open_db", open_after_writer)
    sources = {
        "db": module._source("path", db),
        "board_tasks": {"status": "ok", "detail": ""},
        "tasks": {"status": "ok", "detail": ""},
        "task_sessions": {"status": "ok", "detail": ""},
    }
    module._collect_db_rows(db, sources)
    assert all(path.exists() for path in sidecars), "the tool must not delete live writer sidecars"
    release_writer.set()
    module._concurrent_writer_thread.join(timeout=10)
    assert not module._concurrent_writer_thread.is_alive()
    recovered = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    assert recovered.execute("SELECT id FROM board_tasks").fetchall() == [("writer-live",)]
    recovered.close()


def test_filters_limit_and_help_contract(resume_fixture: tuple[Path, Path]) -> None:
    script, db_path = resume_fixture
    payload = json.loads(
        _run(
            script,
            "--db",
            str(db_path),
            "--status",
            "paused",
            "--since",
            "2026-08-08T12:00:00Z",
            "--limit",
            "1",
            "--json",
        ).stdout
    )
    assert [row["id"] for row in payload["tasks"]] == ["task-new"]
    help_text = _run(script, "--help").stdout
    assert "and presupposes neither is authoritative — that decision is pending." in help_text
    assert "--swarm-root" in help_text and "--ledger" in help_text and "quiescent" in help_text


def test_since_uses_instants_and_preserves_unknown_timestamps(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("task_resume_index_under_test", SOURCE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._apply_filters(
        [{"id": "offset-old", "updated_at": "2026-08-07T23:14:20Z"}],
        "updated_at",
        50,
        "2026-08-07T23:00:00-05:00",
    ) == []
    fractional = {"id": "fractional-new", "workbook_md_mtime": "2026-07-31T02:02:46.592331Z"}
    assert module._apply_filters(
        [fractional], "workbook_md_mtime", 50, "2026-07-31T02:02:46Z"
    ) == [fractional]
    unknown = {"id": "unknown-time", "error": "permission denied"}
    assert module._apply_filters([unknown], "workbook_md_mtime", 50, "2026-01-01T00:00:00Z") == [
        unknown
    ]


def test_section_counts_and_human_limit_footer(resume_fixture: tuple[Path, Path]) -> None:
    script, db_path = resume_fixture
    payload = json.loads(_run(script, "--db", str(db_path), "--limit", "2", "--json").stdout)
    assert {
        key: payload["sources"]["board_tasks"][key] for key in ("total", "shown", "truncated")
    } == {"total": 3, "shown": 2, "truncated": True}
    human = _run(script, "--db", str(db_path), "--limit", "2").stdout
    assert "showing 2 of 3 (truncated by limit, limit=2)" in human


def test_role_stats_streams_dynamic_events_and_receipts(resume_fixture: tuple[Path, Path]) -> None:
    script, _ = resume_fixture
    payload = json.loads(_run(script, "--role-stats", "--json").stdout)
    assert payload["role_stats"]["planner"]["proposed"] == 1
    assert payload["role_stats"]["planner"]["instrument_error"] == 1
    assert payload["role_stats"]["implementer"]["found"] == 1
    assert payload["sources"]["ledger"]["lines_read"] == 6
    assert payload["sources"]["ledger"]["lines_unparseable"] == 1


def test_role_stats_survives_unreadable_db(resume_fixture: tuple[Path, Path]) -> None:
    script, _ = resume_fixture
    broken = script.parent.parent / "broken.sqlite3"
    broken.write_text("not sqlite", encoding="utf-8")
    ledger = script.parent.parent / "var" / "loopqueue" / "ledger.jsonl"
    result = _run(script, "--db", str(broken), "--ledger", str(ledger), "--role-stats", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["role_stats"]["planner"]["proposed"] == 1
    assert payload["sources"]["db"]["status"] == "unreadable"


def test_namespace_dirs_are_excluded_and_missing_workbook_is_labeled(
    resume_fixture: tuple[Path, Path],
) -> None:
    script, db_path = resume_fixture
    root = script.parent.parent / "var" / "swarm"
    clone_task = root / "clones" / "clone-task"
    clone_task.mkdir(parents=True)
    (clone_task / "TASK.md").write_text("# clone", encoding="utf-8")
    (clone_task / "WORKBOOK.md").write_text("checkpoint", encoding="utf-8")
    task_only = root / "run-resume" / "task-only"
    task_only.mkdir()
    (task_only / "TASK.md").write_text("# task only", encoding="utf-8")
    payload = json.loads(_run(script, "--db", str(db_path), "--json").stdout)
    all_rows = payload["swarm_docs"] + payload["unmatched_dirs"]
    assert not any("/clones/" in row["dir"] for row in all_rows)
    assert any(row["run_id"] == "run-resume" for row in payload["swarm_docs"])
    no_workbook = next(row for row in payload["swarm_docs"] if row["dir"].endswith("task-only"))
    assert no_workbook["resume_probe"] == "no_workbook"
    assert payload["sources"]["swarm"]["namespace_dirs_skipped"] == ["clones"]


def test_collect_role_stats_distinguishes_unreadable_or_missing(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("task_resume_index_under_test", SOURCE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing_stats, missing = module._collect_role_stats(tmp_path / "missing.jsonl")
    assert missing_stats == {} and missing["status"] == "missing"
    # A directory exercises OSError reliably even when tests run as root.
    unreadable_stats, unreadable = module._collect_role_stats(tmp_path)
    assert unreadable_stats == {} and unreadable["status"] == "unreadable"


def test_tail_probe_overlap_and_unknown_truncated_absence(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("task_resume_index_under_test", SOURCE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    needle, window = b"```resume", 16 * 1024
    for omitted in range(1, len(needle)):
        path = tmp_path / f"straddle-{omitted}.md"
        path.write_bytes(b"x" * 100 + needle + b"y" * (window - len(needle) + omitted))
        assert module._workbook_has_resume_block(path) is True
    old = tmp_path / "old.md"
    old.write_bytes(needle + b"y" * (40 * 1024))
    assert module._workbook_has_resume_block(old) is None
