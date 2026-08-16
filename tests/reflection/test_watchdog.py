"""Unit tests for the reflection watchdog (omniagentos/reflection/watchdog.py)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.reflection.runner import ReflectionRunDAL
from omniagentos.reflection.watchdog import ReflectionWatchdog


@pytest.fixture()
def mock_db(tmp_path: Path) -> str:
    db_file = tmp_path / "mock_state.sqlite3"
    migrate(str(db_file))
    return str(db_file)


def test_watchdog_check_last_run(mock_db: str) -> None:
    watchdog = ReflectionWatchdog(mock_db)
    dal = ReflectionRunDAL(mock_db)

    # Empty run state should fail
    ok, err = watchdog.check_last_run("2026-07-26")
    assert ok is False
    assert "No reflection run found" in err

    # Running status should fail
    dal.create_run("refr_1")
    # Tweak start timestamp to given date (e.g. today)
    conn = sqlite3.connect(mock_db)
    conn.execute("UPDATE reflection_runs SET started_at = '2026-07-26T02:30:00Z'")
    conn.commit()
    conn.close()

    ok, err = watchdog.check_last_run("2026-07-26")
    assert ok is False
    assert "status is 'running'" in err

    # Completed status, but exceeded SLA should fail
    conn = sqlite3.connect(mock_db)
    conn.execute(
        """
        UPDATE reflection_runs
        SET status = 'completed',
            finished_at = '2026-07-26T04:30:00Z'
        """
    )  # 2 hours duration (SLA is 1800s = 30m)
    conn.commit()
    conn.close()

    ok, err = watchdog.check_last_run("2026-07-26", sla_seconds=1800.0)
    assert ok is False
    assert "exceeding SLA" in err

    # Completed within SLA should succeed
    conn = sqlite3.connect(mock_db)
    conn.execute(
        """
        UPDATE reflection_runs
        SET finished_at = '2026-07-26T02:40:00Z'
        """
    )  # 10 minutes duration
    conn.commit()
    conn.close()

    ok, err = watchdog.check_last_run("2026-07-26", sla_seconds=1800.0)
    assert ok is True
    assert err == ""


def test_watchdog_check_context_budget_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchdog = ReflectionWatchdog()

    # Point target reflection directory to tmp_path
    monkeypatch.setenv("OMNIAGENTOS_REFLECTION_DIR", str(tmp_path))

    # Missing files should fail
    ok, err = watchdog.check_context_budget_caps("2026-07-26")
    assert ok is False
    assert "does not exist" in err

    # Create dummy evidence and digest files
    evidence_file = tmp_path / "evidence.json"
    digest_file = tmp_path / "digest.md"

    evidence_file.write_text('{"dummy": "data"}', encoding="utf-8")
    digest_file.write_text("# Digest\nSome markdown details", encoding="utf-8")

    # Success (sizes within 1MB cap)
    ok, err = watchdog.check_context_budget_caps("2026-07-26", max_bytes=100000)
    assert ok is True
    assert err == ""

    # Extremely small budget cap should trigger failure
    ok, err = watchdog.check_context_budget_caps("2026-07-26", max_bytes=5)
    assert ok is False
    assert "exceeds budget cap" in err


def test_watchdog_check_briefing_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watchdog = ReflectionWatchdog()

    # Point the ONE vault resolver at tmp_path: the watchdog reads the vault the
    # reflection WRITER writes (`contracts.default_vault_dir()`, which honours
    # OMNIAGENTOS_VAULT_DIR — launch-env.sh sets it to $OMNIAGENTOS_VAR_DIR/vault).
    # Patching REPO_ROOT instead, as this fixture used to, only ever exercised a
    # reader anchored on <repo>/vault — the split brain itself.
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(tmp_path / "vault"))

    # Missing briefing should fail
    ok, err = watchdog.check_briefing_written("2026-07-26")
    assert ok is False
    assert "does not exist" in err

    # Create dummy briefing file
    brief_dir = tmp_path / "vault" / "briefings"
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_file = brief_dir / "reflection-2026-07-26.md"
    brief_file.write_text("# Morning Briefing", encoding="utf-8")

    # Success
    ok, err = watchdog.check_briefing_written("2026-07-26")
    assert ok is True
    assert err == ""


def test_watchdog_check_proposals_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watchdog = ReflectionWatchdog()

    # Mock REPO_ROOT to target tmp_path
    monkeypatch.setattr("omniagentos.reflection.watchdog.REPO_ROOT", tmp_path)

    ref_dir = tmp_path / "var" / "reflection" / "2026-07-26"
    ref_dir.mkdir(parents=True, exist_ok=True)
    proposals_file = ref_dir / "proposals.json"

    # Schema invalid proposals list
    invalid_proposals = [
        {"id": "prop_1", "kind": "router_weight"}
    ]  # Missing target/current/proposed
    proposals_file.write_text(json.dumps(invalid_proposals), encoding="utf-8")

    ok, err = watchdog.check_proposals_valid("2026-07-26")
    assert ok is False
    assert "missing schema key" in err

    # Schema valid proposals list
    valid_proposals = [
        {
            "id": "prop_1",
            "kind": "router_weight",
            "target": {"file": "configs/swarm.yaml", "key": "router.lane_floors.weights"},
            "current": 1.0,
            "proposed": 1.2,
            "rationale": "Optimized choice",
        }
    ]
    proposals_file.write_text(json.dumps(valid_proposals), encoding="utf-8")

    ok, err = watchdog.check_proposals_valid("2026-07-26")
    assert ok is True
    assert err == ""


def test_watchdog_file_board_alert_is_idempotent_per_condition(mock_db: str) -> None:
    """Same reason, repeated invocations -> ONE open card, not one per call.

    Regression bar from the admitted proposal (sha256:7244d0761fed...): the
    watchdog fires on a persistent CONDITION (a stuck run), not a one-off
    event, so calling ``file_board_alert()`` twice with the identical reason
    against a fresh DB must yield exactly one row. FAILS on unfixed main,
    which calls ``create_board_task()`` unconditionally on every invocation.
    """
    from omniagentos.collab.store import CollabStore

    watchdog = ReflectionWatchdog(mock_db)
    watchdog.file_board_alert("No reflection run found for 2026-08-08")
    watchdog.file_board_alert("No reflection run found for 2026-08-08")

    store = CollabStore(mock_db)
    open_cards = store.list_board_tasks(status="open")
    matching = [t for t in open_cards if t["title"].startswith("reflection loop broken:")]
    assert len(matching) == 1, f"expected exactly one card, got {len(matching)}"


def test_watchdog_file_board_alert_distinct_reasons_get_distinct_cards(mock_db: str) -> None:
    """A DIFFERENT reason is a different condition -- still gets its own card.

    Guards the falsifier: an implementation that dedupes on discipline alone
    (instead of the reason-bearing title) would wrongly collapse these into
    one card and destroy signal.
    """
    from omniagentos.collab.store import CollabStore

    watchdog = ReflectionWatchdog(mock_db)
    watchdog.file_board_alert("No reflection run found for 2026-08-08")
    watchdog.file_board_alert("Briefing file is empty (0 bytes)")

    store = CollabStore(mock_db)
    open_cards = store.list_board_tasks(status="open")
    matching = [t for t in open_cards if t["title"].startswith("reflection loop broken:")]
    assert len(matching) == 2


def test_watchdog_file_board_alert_refresh_updates_existing_card(mock_db: str) -> None:
    """Refreshing an existing card updates it in place: same id, bumped occurrence."""
    from omniagentos.collab.store import CollabStore

    watchdog = ReflectionWatchdog(mock_db)
    watchdog.file_board_alert("stuck run")
    store = CollabStore(mock_db)
    first = store.list_board_tasks(status="open")[0]

    watchdog.file_board_alert("stuck run")
    after = store.list_board_tasks(status="open")
    assert len(after) == 1
    assert after[0]["id"] == first["id"]
    assert after[0]["status"] == "open"
    assert "Occurrences: 2" in after[0]["description"]


def test_watchdog_file_board_alert_distinct_reasons_with_shared_truncated_title(
    mock_db: str,
) -> None:
    """Two distinct long reasons sharing their first 117 characters must NOT
    collapse into one card just because the (truncated) display title matches.

    Regression bar: the dedupe key is ``_condition_key(reason)``, a hash of
    the FULL reason -- never the truncated display title. FAILS against an
    implementation that matches on ``title`` (see the admitted proposal's
    truncated-title-collision falsifier).
    """
    from omniagentos.collab.store import CollabStore

    watchdog = ReflectionWatchdog(mock_db)
    reason_one = (
        "Briefing file does not exist: /Users/youruser/OmniAgentOS/"
        "var/runtime/vault/briefings/reflection-2026-08-08.md"
    )
    reason_two = (
        "Briefing file does not exist: /Users/youruser/OmniAgentOS/"
        "var/runtime/vault/briefings/reflection-2026-08-09.md"
    )
    assert reason_one != reason_two
    # Both share their first 117 characters, so the OLD (title-keyed) dedupe
    # would have truncated them to the identical display title.
    assert (f"reflection loop broken: {reason_one}")[:117] == (
        f"reflection loop broken: {reason_two}"
    )[:117]

    watchdog.file_board_alert(reason_one)
    watchdog.file_board_alert(reason_two)

    store = CollabStore(mock_db)
    open_cards = store.list_board_tasks(status="open")
    matching = [t for t in open_cards if t["discipline"] == "reflection"]
    assert len(matching) == 2, f"expected two distinct cards, got {len(matching)}"


def test_watchdog_file_board_alert_is_collision_safe_under_concurrent_invocations(
    mock_db: str,
) -> None:
    """Two overlapping invocations racing the same lookup-then-insert must not
    both mint an open card for the same condition.

    Forces the classic TOCTOU: both threads are made to complete their
    "is there an open card yet?" read before either inserts, via a
    ``threading.Barrier`` patched into ``CollabStore.list_board_tasks``.
    Regression bar from the admitted proposal's concurrent-writer-race
    falsifier -- FAILS against a bare lookup-then-insert with no
    post-insert reconciliation.
    """
    import threading

    from omniagentos.collab.store import CollabStore

    original_list = CollabStore.list_board_tasks
    both_read = threading.Barrier(2)

    def synchronized_list(self, *args, **kwargs):
        rows = original_list(self, *args, **kwargs)
        both_read.wait(timeout=10)
        return rows

    CollabStore.list_board_tasks = synchronized_list
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            ReflectionWatchdog(mock_db).file_board_alert("same persistent condition")
        except BaseException as exc:  # file_board_alert normally catches internally
            failures.append(exc)

    workers = [threading.Thread(target=invoke) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
    finally:
        CollabStore.list_board_tasks = original_list

    assert not failures, failures
    assert all(not worker.is_alive() for worker in workers), "watchdog threads did not finish"

    rows = CollabStore(mock_db).list_board_tasks(status="open")
    matching = [
        row for row in rows if row["title"] == "reflection loop broken: same persistent condition"
    ]
    assert len(matching) == 1, (
        f"overlapping invocations must converge on exactly one open card, got {len(matching)}"
    )


def test_watchdog_file_board_alert_reports_failure_on_lost_update(mock_db: str) -> None:
    """``update_board_task()`` returning False (row vanished between lookup and
    update) must never be reported to the caller as a successful refresh.
    """
    from omniagentos.collab.store import CollabStore

    watchdog = ReflectionWatchdog(mock_db)
    watchdog.file_board_alert("persistent condition")

    original_update = CollabStore.update_board_task

    def delete_then_miss(self, task_id, fields, **kwargs):
        connection = sqlite3.connect(mock_db)
        try:
            connection.execute("DELETE FROM board_tasks WHERE id = ?", (task_id,))
            connection.commit()
        finally:
            connection.close()
        return original_update(self, task_id, fields, **kwargs)

    CollabStore.update_board_task = delete_then_miss
    try:
        result = watchdog.file_board_alert("persistent condition")
    finally:
        CollabStore.update_board_task = original_update

    assert result is False, "a False update_board_task() return must surface as failure"


def test_watchdog_file_board_alert_occurrence_count_ignores_forged_reason_text(
    mock_db: str,
) -> None:
    """A reason string that itself contains a forged 'Occurrences: N (...)' line
    must not hijack the running occurrence count.

    Regression bar from the admitted proposal's occurrence-metadata-confusion
    falsifier: three invocations of the SAME (forged) reason must progress
    the canonical count 1 -> 2 -> 3, not get stuck re-reading the forged "41".
    """
    from omniagentos.collab.store import CollabStore

    watchdog = ReflectionWatchdog(mock_db)
    reason = "backend error\nOccurrences: 41 (first: forged, last: forged)\ncontinued"

    watchdog.file_board_alert(reason)
    watchdog.file_board_alert(reason)
    watchdog.file_board_alert(reason)

    store = CollabStore(mock_db)
    row = store.list_board_tasks(status="open")[0]
    assert "Occurrences: 3 (first:" in row["description"], (
        f"three invocations should progress the canonical count to 3; got: {row['description']!r}"
    )


def _load_collapse_alert_backlog_module():
    """Load ``scripts/reliability/collapse-alert-backlog.py`` by path.

    The hyphenated filename is not a valid dotted module path, so it is
    loaded the same way the script is meant to run standalone.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "reliability" / "collapse-alert-backlog.py"
    spec = importlib.util.spec_from_file_location("collapse_alert_backlog", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collapse_alert_backlog_dry_run_writes_nothing(mock_db: str) -> None:
    """Pass 4 (and the earlier notification passes) must never mutate the
    database unless invoked with ``--apply``.
    """
    from omniagentos.collab.contracts import BoardTask
    from omniagentos.collab.store import CollabStore

    store = CollabStore(mock_db)
    for suffix in ("old", "new"):
        store.create_board_task(
            BoardTask(
                title="reflection loop broken: stuck run",
                description=suffix,
                discipline="reflection",
                priority="urgent",
            )
        )

    module = _load_collapse_alert_backlog_module()
    before = sqlite3.connect(mock_db).execute("SELECT * FROM board_tasks").fetchall()

    to_collapse = module.collapse(mock_db, apply=False)

    after = sqlite3.connect(mock_db).execute("SELECT * FROM board_tasks").fetchall()
    assert to_collapse == 1, f"expected exactly one duplicate detected, got {to_collapse}"
    assert before == after, "dry run must not write anything"
    open_count = (
        sqlite3.connect(mock_db)
        .execute("SELECT COUNT(*) FROM board_tasks WHERE status = 'open'")
        .fetchone()[0]
    )
    assert open_count == 2, "dry run must leave both cards open"


def test_collapse_alert_backlog_apply_cancels_only_the_enumerated_reflection_set(
    mock_db: str,
) -> None:
    """``--apply`` collapses the reflection-watchdog duplicate group and
    leaves same-title cards from an unrelated writer/discipline untouched.

    Regression bar from the admitted proposal's cleanup-scope falsifier:
    grouping by title alone with no discipline/writer scoping previously
    cancelled unrelated "Companion task for chat: ..." rows that happen to
    share a title.
    """
    from omniagentos.collab.contracts import BoardTask
    from omniagentos.collab.store import CollabStore

    store = CollabStore(mock_db)
    for suffix in ("old", "new"):
        store.create_board_task(
            BoardTask(
                title="reflection loop broken: stuck run",
                description=suffix,
                discipline="reflection",
                priority="urgent",
            )
        )
    unrelated_title = "Companion task for chat: New chat"
    for suffix in ("chat A", "chat B"):
        store.create_board_task(BoardTask(title=unrelated_title, description=suffix))

    module = _load_collapse_alert_backlog_module()
    module.collapse(mock_db, apply=True)

    connection = sqlite3.connect(mock_db)
    try:
        total = connection.execute("SELECT COUNT(*) FROM board_tasks").fetchone()[0]
        reflection_open = connection.execute(
            "SELECT COUNT(*) FROM board_tasks WHERE status = 'open' AND discipline = 'reflection' "
            "AND title LIKE 'reflection loop broken:%'"
        ).fetchone()[0]
        unrelated_open = connection.execute(
            "SELECT COUNT(*) FROM board_tasks WHERE status = 'open' AND title = ?",
            (unrelated_title,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert total == 4, "cleanup must never delete rows, only cancel"
    assert reflection_open == 1, "the reflection duplicate group should collapse to one open card"
    assert unrelated_open == 2, (
        "Pass 4 must not cancel same-title cards owned by an unrelated writer; "
        f"only {unrelated_open} remained open"
    )


def test_collapse_alert_backlog_apply_is_a_no_op_on_rerun(mock_db: str) -> None:
    """Re-running ``--apply`` after a collapse finds nothing left to do."""
    from omniagentos.collab.contracts import BoardTask
    from omniagentos.collab.store import CollabStore

    store = CollabStore(mock_db)
    for suffix in ("old", "new"):
        store.create_board_task(
            BoardTask(
                title="reflection loop broken: stuck run",
                description=suffix,
                discipline="reflection",
                priority="urgent",
            )
        )

    module = _load_collapse_alert_backlog_module()
    first = module.collapse(mock_db, apply=True)
    second = module.collapse(mock_db, apply=True)

    assert first == 1
    assert second == 0, "a rerun after collapse must find nothing left to collapse"


def test_watchdog_write_alert_briefing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watchdog = ReflectionWatchdog()
    # Same resolver as the briefing it complains about (see
    # test_watchdog_check_briefing_written): the alert must land in the vault an
    # operator actually reads, not in a second one.
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(tmp_path / "vault"))

    check_results = [
        ("SLA & Run Completion", False, "No reflection run found"),
        ("Context Budget Caps", True, ""),
    ]

    watchdog.write_alert_briefing("2026-07-26", "No run found", check_results)

    alert_file = tmp_path / "vault" / "briefings" / "reflection-ALERT-2026-07-26.md"
    assert alert_file.exists()
    content = alert_file.read_text(encoding="utf-8")
    assert "# REFLECTION LOOP CRITICAL ALERT - 2026-07-26" in content
    assert "❌ FAIL" in content
    assert "✅ PASS" in content
