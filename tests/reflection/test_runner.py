"""Unit tests for the reflection runner (omniagentos/reflection/runner.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.reflection.runner import (
    ReflectionRunDAL,
    load_reflection_config,
    run_reflection_loop,
)


def test_reflection_run_dal(tmp_path: Path) -> None:
    db_file = tmp_path / "test_state.sqlite3"
    migrate(str(db_file))
    dal = ReflectionRunDAL(str(db_file))

    # The table is installed by migration 077, not by DAL initialization.
    assert db_file.exists()

    # Create run row
    dal.create_run("refr_abc123")
    run = dal.get_run("refr_abc123")
    assert run is not None
    assert run["id"] == "refr_abc123"
    assert run["status"] == "running"
    assert run["started_at"] is not None

    # Update stage status
    dal.update_stage_status("refr_abc123", "harvest", "ok")
    run = dal.get_run("refr_abc123")
    assert run["harvest_status"] == "ok"

    # Update stage status with error
    dal.update_stage_status("refr_abc123", "propose", "failed", "timeout error")
    run = dal.get_run("refr_abc123")
    assert run["propose_status"] == "failed"
    assert run["error"] == "timeout error"

    # Finish run
    dal.finish_run("refr_abc123", "completed")
    run = dal.get_run("refr_abc123")
    assert run["status"] == "completed"
    assert run["finished_at"] is not None


def test_reflection_run_dal_requires_migration(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"reflection_runs table is missing.*make migrate"):
        ReflectionRunDAL(str(tmp_path / "unmigrated.sqlite3"))


def test_reflection_runner_has_no_runtime_reflection_ddl() -> None:
    reflection_dir = Path(__file__).resolve().parents[2] / "omniagentos" / "reflection"
    runner_source = (reflection_dir / "runner.py").read_text(encoding="utf-8")
    assert "CREATE TABLE" not in runner_source.upper()

    migration_owned_tables = ("reflection_runs", "reflection_proposals", "reflection_outcomes")
    for source_path in reflection_dir.glob("*.py"):
        source = source_path.read_text(encoding="utf-8").upper()
        for table_name in migration_owned_tables:
            assert f"CREATE TABLE {table_name.upper()}" not in source
            assert f"CREATE TABLE IF NOT EXISTS {table_name.upper()}" not in source


def test_reflection_run_dal_uses_migration_077_schema(tmp_path: Path) -> None:
    db_file = tmp_path / "migrated.sqlite3"
    migrate(str(db_file))

    import sqlite3

    with sqlite3.connect(str(db_file)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reflection_runs)")}
    assert {"sources_read", "bytes_read", "caps_hit"}.issubset(columns)

    dal = ReflectionRunDAL(str(db_file))
    dal.create_run("refr_migrated")
    assert dal.get_run("refr_migrated")["status"] == "running"


def test_load_reflection_config(tmp_path: Path) -> None:
    config = load_reflection_config()
    assert config is not None
    assert "limits" in config
    assert "validation" in config


def test_run_reflection_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Use a mock/tmp DB for the loop
    db_file = tmp_path / "test_loop.sqlite3"
    migrate(str(db_file))
    monkeypatch.setattr("omniagentos.reflection.runner.default_db_path", lambda: str(db_file))

    # Stub every stage with the REAL module APIs so the unit test never touches
    # ledgers, the network, or the repo working tree.
    class _Stub:
        @staticmethod
        def harvest_evidence(date_str=None):
            return {"date": date_str, "sources": []}

        @staticmethod
        def run_propose(date_str=None, db_path=None):
            return {"date": date_str, "proposals": [], "dropped_proposals": []}

        @staticmethod
        def auto_apply_eligible(db_path=None, accepted_fingerprints=None):
            return []

        @staticmethod
        def generate_reflection_report(date_str=None, db_path=None, vault_dir=None):
            return str(tmp_path / "briefing.md")

    for factory in ("get_harvester", "get_proposer", "get_applier", "get_reporter"):
        monkeypatch.setattr(f"omniagentos.reflection.runner.{factory}", lambda _s=_Stub: _s)

    # Execute the loop in observe-only dry run mode
    run_id = run_reflection_loop(observe_only=True)
    assert run_id.startswith("refr_")

    dal = ReflectionRunDAL(str(db_file))
    run = dal.get_run(run_id)
    assert run is not None
    assert run["status"] == "completed"

    # THREE-VALUED SETTLEMENT — this block asserts `ungateable`, not `ok`.
    #
    # Every stub above returns a well-formed but EMPTY artifact: `sources: []`,
    # `proposals: []`, `auto_apply_eligible() -> []`, and a briefing path under
    # tmp_path that is never actually written. Under the contract in
    # omniagentos/reflection/settlement.py, `classify_settlement` settles a
    # missing/empty artifact as UNGATEABLE ("nothing to grade"), and reaching
    # OK is impossible without positive evidence.
    #
    # These assertions read `== "ok"` from 2026-07-26 (c02e3e6e, the original
    # runner commit) until now. settlement.py landed 2026-08-03 (3648d4f8) and
    # DELIBERATELY removed that behaviour — its docstring names the old status
    # as "a transcript of which `try` blocks did not raise". The test was never
    # updated, so it has been red on main ever since, asserting precisely the
    # favourable-absence bug the module was written to kill. The code is
    # correct here; the expectation was stale.
    for stage in ("harvest", "propose", "validate", "apply", "report"):
        assert run[f"{stage}_status"] == "ungateable", (
            f"{stage} produced no artifact and must settle 'ungateable' "
            f"(settlement.py), got {run[f'{stage}_status']!r}"
        )


def test_run_reflection_loop_settles_ok_on_real_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive half of the settlement contract.

    Without this, ``test_run_reflection_loop`` above would be satisfied by a
    runner that hardcoded ``ungateable`` everywhere — which would be just as
    blind as the ``ok`` it replaced, only in the other direction. Here harvest
    returns real sources while propose still returns none, so the two stages
    must settle DIFFERENTLY inside a single run. That difference is the whole
    point of the status column.
    """
    db_file = tmp_path / "test_loop_ok.sqlite3"
    migrate(str(db_file))
    monkeypatch.setattr("omniagentos.reflection.runner.default_db_path", lambda: str(db_file))

    class _Stub:
        @staticmethod
        def harvest_evidence(date_str=None):
            return {"date": date_str, "sources": ["ledger.jsonl", "vault/Home.md"]}

        @staticmethod
        def run_propose(date_str=None, db_path=None):
            return {"date": date_str, "proposals": [], "dropped_proposals": []}

        @staticmethod
        def auto_apply_eligible(db_path=None, accepted_fingerprints=None):
            return []

        @staticmethod
        def generate_reflection_report(date_str=None, db_path=None, vault_dir=None):
            return str(tmp_path / "briefing.md")

    for factory in ("get_harvester", "get_proposer", "get_applier", "get_reporter"):
        monkeypatch.setattr(f"omniagentos.reflection.runner.{factory}", lambda _s=_Stub: _s)

    run_id = run_reflection_loop(observe_only=True)
    run = ReflectionRunDAL(str(db_file)).get_run(run_id)
    assert run is not None
    # Real sources -> gradeable success.
    assert run["harvest_status"] == "ok"
    # ...while a stage that still saw nothing stays ungateable in the SAME run.
    assert run["propose_status"] == "ungateable"


def test_run_reflection_loop_forwards_validated_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With observe_only=False, apply receives exactly the validated set's
    content fingerprints — removing the forwarding must fail this test."""
    db_file = tmp_path / "loop_fp.sqlite3"
    migrate(str(db_file))
    monkeypatch.setattr("omniagentos.reflection.runner.default_db_path", lambda: str(db_file))

    proposal = {
        "id": "prop_fp",
        "kind": "lesson",
        "target": "docs/lessons/x.md",
        "proposed": "lesson body",
    }
    captured: dict = {}

    class _Stub:
        @staticmethod
        def harvest_evidence(date_str=None):
            return {}

        @staticmethod
        def run_propose(date_str=None, db_path=None):
            return {"proposals": [proposal], "dropped_proposals": []}

        @staticmethod
        def auto_apply_eligible(db_path=None, accepted_fingerprints=None):
            captured["fp"] = accepted_fingerprints
            return []

        @staticmethod
        def generate_reflection_report(date_str=None, db_path=None, vault_dir=None):
            return str(tmp_path / "briefing.md")

    for factory in ("get_harvester", "get_proposer", "get_applier", "get_reporter"):
        monkeypatch.setattr(f"omniagentos.reflection.runner.{factory}", lambda _s=_Stub: _s)
    monkeypatch.setattr(
        "omniagentos.reflection.runner.validate_batch",
        lambda proposals, cfg: [(p, True, "") for p in proposals],
    )

    run_reflection_loop(observe_only=False)

    from omniagentos.reflection.fingerprint import proposal_fingerprint, proposal_strings

    target_str, proposed_str = proposal_strings(proposal["target"], proposal["proposed"])
    assert captured["fp"] == {"prop_fp": proposal_fingerprint("lesson", target_str, proposed_str)}
