"""Migration 131: skill-usage telemetry proves an injected skill was reused.

Before this, nothing recorded which skills got injected into a brief, so
"an extracted skill was actually replayed" could never be shown from data.
``record_skill_usage`` is called at the two sites that resolve/inject VERIFIED
skill content — ``Runner._resolved_skill_block`` (runner steps) and
``UnifiedSpawner._resolved_skill_prompt`` (swarm workers) — right after each
computes the actually-injected set, not the pre-verification candidate set.

DECISIVE TESTS
    ``test_runner_step_records_skill_usage_for_injected_skill`` and
    ``test_swarm_spawn_records_skill_usage_for_injected_skill`` — drive the
    REAL production call path (not ``omniagentos.skills.usage`` directly) and
    read the ``skill_usage`` row back from sqlite, including its
    ``skill_version`` (2026-08-14 xcrit F4).

COUNTERFEIT
    ``test_write_failure_does_not_propagate`` — a closed/broken connection
    must not turn skill-usage telemetry into a run/spawn failure.
    ``test_runner_does_not_record_when_render_emits_nothing`` (xcrit F2) — the
    runner must record only AFTER the block is confirmed non-empty, matching
    the swarm site's ordering.
    ``test_runner_keeps_skill_block_if_usage_import_fails`` /
    ``test_swarm_keeps_skill_block_if_usage_import_fails`` (xcrit F3) — a fault
    resolving ``omniagentos.skills.usage`` itself must not strip an
    already-resolved skill block from the brief.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.db.store import _connect
from omniagentos.skills.usage import record_skill_usage

SENTINEL_BODY = "## Preferred Method\n1. Always run the linter before you claim done.\n"


def _skill_usage_rows(db_path: str) -> list[sqlite3.Row]:
    connection = _connect(db_path)
    try:
        return connection.execute(
            "SELECT run_id, skill_id, skill_version, brief_kind, injected_at "
            "FROM skill_usage ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def _seed_skill(db_path: str, *, slug: str, category: str, body: str) -> None:
    """Insert one active skill via the real write path (assumes OMNIAGENTOS_DB=db_path)."""
    from omniagentos.skills import upsert_skill

    upsert_skill(
        {
            "slug": slug,
            "category": category,
            "subcategory": "General",
            "title": f"{slug} title",
            "content_snapshot": body,
        }
    )


# --------------------------------------------------------------------------
# DECISIVE: runner site
# --------------------------------------------------------------------------


def test_runner_step_records_skill_usage_for_injected_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniagentos.db.store import SqliteStore
    from omniagentos.runner.core import Runner

    db_path = str(tmp_path / "runner_usage.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    store = SqliteStore(db_path)
    _seed_skill(db_path, slug="runner_skill", category="Coding", body=SENTINEL_BODY)

    runner = Runner.__new__(Runner)
    runner.store = store  # type: ignore[misc]
    block = runner._resolved_skill_block({"discipline_id": "coding", "id": "run_abc123"}, {})
    assert "Always run the linter" in block, block

    rows = _skill_usage_rows(db_path)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["run_id"] == "run_abc123"
    assert row["skill_id"] == "runner_skill"
    assert row["brief_kind"] == "runner"
    assert row["injected_at"]
    assert row["skill_version"] == "1"  # xcrit F4: version must be recorded, not NULL


def test_runner_step_records_nothing_when_no_skill_survives_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped (unverified) skill must never show up as "used"."""
    from omniagentos.db.store import SqliteStore
    from omniagentos.runner.core import Runner

    db_path = str(tmp_path / "runner_usage_dropped.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    store = SqliteStore(db_path)
    _seed_skill(db_path, slug="runner_skill", category="Coding", body=SENTINEL_BODY)

    connection = _connect(db_path)
    try:
        connection.execute("UPDATE skill_versions SET content_digest = 'tampered'")
        connection.commit()
    finally:
        connection.close()

    runner = Runner.__new__(Runner)
    runner.store = store  # type: ignore[misc]
    block = runner._resolved_skill_block({"discipline_id": "coding", "id": "run_dropped"}, {})
    assert block == ""
    assert _skill_usage_rows(db_path) == []


# --------------------------------------------------------------------------
# DECISIVE: swarm site
# --------------------------------------------------------------------------


def test_swarm_spawn_records_skill_usage_for_injected_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniagentos.swarm.scheduler import SpawnRequest
    from omniagentos.swarm.spawn import UnifiedSpawner
    from tests.swarm.test_spawn_integrations import (  # noqa: PLC0415
        _CapturingRunner,
        _CapturingSupervisor,
        _disable_cbm,
        _seed_coding_skill,
        _SessionsDal,
        _SwarmDal,
    )

    _disable_cbm(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_CHAMPION_PROMPT_MODE", "off")
    monkeypatch.setenv("OMNIAGENTOS_CORAL_CONTEXT_MODE", "off")
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "off")

    db_path = str(tmp_path / "swarm_usage.db")
    _seed_coding_skill(db_path, slug="coding-impl")

    dal = _SwarmDal()
    task_id = "task_usage"
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "Implement feature",
        "description": "backend API",
        "discipline": "coding",
    }
    dal.swarm_jsons[task_id] = {"task_key": "codex", "risk_class": "none", "domain": "coding"}
    runner = _CapturingRunner()
    spawner = UnifiedSpawner(
        supervisor=_CapturingSupervisor(),
        provider_runner=runner,
        swarm_dal=dal,
        sessions_dal=_SessionsDal(),
        convert_reservation=lambda r, s: True,
        release_reservation=lambda r: True,
        var_root=tmp_path / "var",
        db_path=db_path,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spawner.spawn(
        SpawnRequest(
            run_id="swr_usage",
            task_id=task_id,
            task_key="codex",
            attempt_id="swa_usage",
            working_dir=str(workspace),
            prompt="do the work",
            provider="codex",
            model="gpt-5.6-sol",
            tier="standard",
            account_id="acct_codex",
            idle_minutes=20.0,
            budget_usd_max=5.0,
            reservation_id="rsv_usage",
        )
    )

    assert len(runner.calls) == 1
    prompt = str(runner.calls[0].get("prompt") or "")
    assert "coding-impl" in prompt

    rows = _skill_usage_rows(db_path)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["run_id"] == "swr_usage"
    assert row["skill_id"] == "coding-impl"
    assert row["brief_kind"] == "swarm"
    assert row["injected_at"]
    assert row["skill_version"] == "1"  # xcrit F4: version must be recorded, not NULL


# --------------------------------------------------------------------------
# xcrit F2: record only AFTER the block is confirmed non-empty
# --------------------------------------------------------------------------


def test_runner_does_not_record_when_render_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved-but-never-rendered skill (e.g. a budget-drop inside
    ``render_skill_block``) must not leave a usage row. Before xcrit F2 the
    runner recorded from ``resolved`` BEFORE calling ``render_skill_block``,
    so any post-write render fault/drop still left a stray row."""
    from omniagentos.db.store import SqliteStore
    from omniagentos.runner.core import Runner
    from omniagentos.skills import resolve as resolve_mod

    db_path = str(tmp_path / "runner_usage_no_render.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    store = SqliteStore(db_path)
    _seed_skill(db_path, slug="runner_skill", category="Coding", body=SENTINEL_BODY)

    monkeypatch.setattr(resolve_mod, "render_skill_block", lambda *a, **k: "")

    runner = Runner.__new__(Runner)
    runner.store = store  # type: ignore[misc]
    block = runner._resolved_skill_block({"discipline_id": "coding", "id": "run_norender"}, {})
    assert block == ""
    assert _skill_usage_rows(db_path) == []


# --------------------------------------------------------------------------
# xcrit F3: a fault resolving the telemetry module must not cost the skills
# --------------------------------------------------------------------------


def test_runner_keeps_skill_block_if_usage_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    from omniagentos.db.store import SqliteStore
    from omniagentos.runner.core import Runner

    db_path = str(tmp_path / "runner_usage_import_fault.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    store = SqliteStore(db_path)
    _seed_skill(db_path, slug="runner_skill", category="Coding", body=SENTINEL_BODY)

    real_import = builtins.__import__

    def boom(name: str, *args: object, **kwargs: object) -> object:
        if name == "omniagentos.skills.usage":
            raise ImportError("usage module missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)

    runner = Runner.__new__(Runner)
    runner.store = store  # type: ignore[misc]
    block = runner._resolved_skill_block({"discipline_id": "coding", "id": "run_importfault"}, {})
    assert "Always run the linter" in block, block
    # No usage row (the write never happened), but the skill content survives.
    assert _skill_usage_rows(db_path) == []


def test_swarm_keeps_skill_block_if_usage_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    from omniagentos.swarm.scheduler import SpawnRequest
    from omniagentos.swarm.spawn import UnifiedSpawner
    from tests.swarm.test_spawn_integrations import (  # noqa: PLC0415
        _CapturingRunner,
        _CapturingSupervisor,
        _disable_cbm,
        _seed_coding_skill,
        _SessionsDal,
        _SwarmDal,
    )

    _disable_cbm(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_CHAMPION_PROMPT_MODE", "off")
    monkeypatch.setenv("OMNIAGENTOS_CORAL_CONTEXT_MODE", "off")
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "off")

    db_path = str(tmp_path / "swarm_usage_import_fault.db")
    _seed_coding_skill(db_path, slug="coding-impl")

    real_import = builtins.__import__

    def boom(name: str, *args: object, **kwargs: object) -> object:
        if name == "omniagentos.skills.usage":
            raise ImportError("usage module missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)

    dal = _SwarmDal()
    task_id = "task_usage_fault"
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "Implement feature",
        "description": "backend API",
        "discipline": "coding",
    }
    dal.swarm_jsons[task_id] = {"task_key": "codex", "risk_class": "none", "domain": "coding"}
    runner = _CapturingRunner()
    spawner = UnifiedSpawner(
        supervisor=_CapturingSupervisor(),
        provider_runner=runner,
        swarm_dal=dal,
        sessions_dal=_SessionsDal(),
        convert_reservation=lambda r, s: True,
        release_reservation=lambda r: True,
        var_root=tmp_path / "var",
        db_path=db_path,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spawner.spawn(
        SpawnRequest(
            run_id="swr_usage_fault",
            task_id=task_id,
            task_key="codex",
            attempt_id="swa_usage_fault",
            working_dir=str(workspace),
            prompt="do the work",
            provider="codex",
            model="gpt-5.6-sol",
            tier="standard",
            account_id="acct_codex",
            idle_minutes=20.0,
            budget_usd_max=5.0,
            reservation_id="rsv_usage_fault",
        )
    )

    assert len(runner.calls) == 1
    prompt = str(runner.calls[0].get("prompt") or "")
    assert "coding-impl" in prompt  # the skill block survives the import fault
    assert _skill_usage_rows(db_path) == []


# --------------------------------------------------------------------------
# COUNTERFEIT: telemetry write faults must never propagate
# --------------------------------------------------------------------------


def test_write_failure_does_not_propagate(tmp_path: Path) -> None:
    """A closed connection must not turn telemetry into a caller-visible error."""
    db_path = str(tmp_path / "closed_usage.db")
    connection = _connect(db_path)
    migrate_connection(connection)
    connection.close()

    # No exception -- the module must swallow the failure and log it.
    record_skill_usage(connection, "run_closed", ["some_skill"], "runner")

    reopened = _connect(db_path)
    try:
        rows = reopened.execute("SELECT * FROM skill_usage").fetchall()
    finally:
        reopened.close()
    assert rows == []


def test_write_failure_on_unmigrated_database_does_not_propagate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Same guarantee via the path-based entry point: an unmigrated db (no
    ``skill_usage`` table yet) must degrade silently, not raise into the
    caller's run/spawn."""
    db_path = str(tmp_path / "unmigrated.db")
    _connect(db_path).close()  # creates the file with no schema at all

    with caplog.at_level("WARNING"):
        record_skill_usage(db_path, "run_unmigrated", ["some_skill"], "runner")
    assert any("skill usage recording failed" in r.getMessage() for r in caplog.records)


def test_record_skill_usage_writes_one_row_per_skill(tmp_path: Path) -> None:
    db_path = str(tmp_path / "multi_usage.db")
    connection = _connect(db_path)
    try:
        migrate_connection(connection)
    finally:
        connection.close()

    record_skill_usage(db_path, "run_multi", ["skill_a", "skill_b"], "swarm")

    rows = _skill_usage_rows(db_path)
    assert [(row["skill_id"], row["brief_kind"]) for row in rows] == [
        ("skill_a", "swarm"),
        ("skill_b", "swarm"),
    ]
    assert all(row["run_id"] == "run_multi" for row in rows)


def test_record_skill_usage_writes_the_index_aligned_version(tmp_path: Path) -> None:
    """xcrit F4: skill_version travels alongside skill_id, index-aligned."""
    db_path = str(tmp_path / "versioned_usage.db")
    connection = _connect(db_path)
    try:
        migrate_connection(connection)
    finally:
        connection.close()

    record_skill_usage(
        db_path,
        "run_versioned",
        ["skill_a", "skill_b"],
        "runner",
        skill_versions=["3", "7"],
    )

    rows = _skill_usage_rows(db_path)
    assert [(row["skill_id"], row["skill_version"]) for row in rows] == [
        ("skill_a", "3"),
        ("skill_b", "7"),
    ]


def test_record_skill_usage_version_mismatch_falls_back_to_null(tmp_path: Path) -> None:
    """A length mismatch between ids and versions must never mis-pair them --
    degrade to NULL versions rather than guess."""
    db_path = str(tmp_path / "mismatched_usage.db")
    connection = _connect(db_path)
    try:
        migrate_connection(connection)
    finally:
        connection.close()

    record_skill_usage(
        db_path,
        "run_mismatch",
        ["skill_a", "skill_b"],
        "runner",
        skill_versions=["only_one"],
    )

    rows = _skill_usage_rows(db_path)
    assert [row["skill_version"] for row in rows] == [None, None]
