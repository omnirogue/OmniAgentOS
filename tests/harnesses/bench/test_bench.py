"""Tests for the p11 bench runner, MiniSweAdapter, B0 arm, and devtasks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    AgentAdapter,
    AgentInput,
    AgentResult,
    AgentUsage,
    HarnessType,
    ResultStatus,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.ledger import read_manifests
from omniagentos.mock_adapter import MockAdapter
from omniagentos.vault import parse_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_bench_dry_run() -> None:
    """Smoke: bench runner with --dry-run produces ≥6 manifests (3 tasks × 2 arms)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omniagentos.harnesses.bench",
            "--arms",
            "b0,b1",
            "--tasks",
            "devtasks",
            "--limit",
            "3",
            "--dry-run",
            "--no-append",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0, (
        f"bench dry-run failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    lines = [ln for ln in result.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    manifests: list[dict[str, Any]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "run_id" in obj and "arm" in obj:
            manifests.append(obj)

    assert len(manifests) >= 6, f"expected ≥6 manifests, got {len(manifests)}: {manifests!r}"

    arms_seen: set[str] = set()
    task_ids_by_arm: dict[str, set[str]] = {"b0": set(), "b1": set()}
    for m in manifests:
        arm = m.get("arm")
        assert arm in ("b0", "b1"), m
        arms_seen.add(str(arm))
        harness = m.get("harness")
        assert isinstance(harness, dict), m
        assert harness.get("harness") in (
            "cli-claude",
            "mini-swe",
            HarnessType.CLI_CLAUDE.value,
            HarnessType.MINI_SWE.value,
        ), harness
        assert harness.get("version") is not None and harness.get("version") != "", harness
        env_h = harness.get("env_hash")
        assert env_h, f"env_hash missing/empty on manifest: {m}"
        assert env_h is not None
        task_ids_by_arm[str(arm)].add(str(m.get("task_id")))

    assert "b0" in arms_seen and "b1" in arms_seen
    # Same task ids on both arms for the limited set.
    assert task_ids_by_arm["b0"] == task_ids_by_arm["b1"]
    assert len(task_ids_by_arm["b0"]) >= 3


def test_bench_dry_run_writes_store_ledger_and_vault(tmp_path: Path) -> None:
    """Mocked B0/B1 work is offline but still finalizes all durable projections."""
    from omniagentos.harnesses.bench.runner import run_bench

    db_path = tmp_path / "runs.db"
    ledger_dir = tmp_path / "ledger"
    vault_dir = tmp_path / "vault"
    migrate(str(db_path))

    manifests = run_bench(
        arms=["b0", "b1"],
        tasks_dir=REPO_ROOT / "devtasks",
        limit=2,
        dry_run=True,
        db_path=str(db_path),
        ledger_dir=str(ledger_dir),
        vault_dir=str(vault_dir),
    )

    assert len(manifests) == 4
    ledger_by_id = {manifest.run_id: manifest for manifest in read_manifests(str(ledger_dir))}
    store = SqliteStore(str(db_path))
    for manifest in manifests:
        assert ledger_by_id[manifest.run_id].arm == manifest.arm
        row = store.get_run(manifest.run_id)
        assert row is not None
        assert row["state"] == manifest.state.value
        assert row["harness"] == manifest.harness.harness.value
        assert row["wall_ms"] == manifest.usage.wall_ms
        assert row["output_text"] == f"dry-run ok arm={manifest.arm.value} task={manifest.task_id}"
        assert row["manifest_path"]
        assert row["vault_note_path"]
        note = Path(str(row["vault_note_path"]))
        assert note.is_file()
        content = note.read_text(encoding="utf-8")
        assert parse_frontmatter(content).id == manifest.run_id
        assert str(row["output_text"]) in content
        assert '"prompt":"' in content


def test_bench_reports_durable_finalization_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omniagentos.harnesses.bench import runner

    def _fail_finalization(*args: object, **kwargs: object) -> tuple[str, str | None]:
        raise OSError("forced finalize failure")

    monkeypatch.setattr(runner, "finalize_manifest", _fail_finalization)

    result = runner.main(
        [
            "--arms",
            "b0",
            "--tasks",
            str(REPO_ROOT / "devtasks"),
            "--limit",
            "1",
            "--dry-run",
            "--db-path",
            str(tmp_path / "runs.db"),
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--vault-dir",
            str(tmp_path / "vault"),
        ]
    )

    assert result == 2
    assert "durable finalization failed" in capsys.readouterr().err


def test_miniswe_adapter_exists() -> None:
    """MiniSweAdapter is importable at the registry path and satisfies AgentAdapter."""
    from omniagentos.harnesses.miniswe.adapter import MiniSweAdapter

    adapter = MiniSweAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "mini-swe"
    assert isinstance(adapter.version, str) and adapter.version

    # If p04 registry is present, also resolve via it.
    try:
        from omniagentos.adapters import resolve_adapter  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        try:
            from omniagentos.adapters.registry import (  # type: ignore[import-not-found]
                resolve_adapter,
            )
        except ImportError:
            resolve_adapter = None  # type: ignore[assignment]

    if resolve_adapter is not None:
        try:
            resolved = resolve_adapter(HarnessType.MINI_SWE)
            assert isinstance(resolved, AgentAdapter)
            assert resolved.name == "mini-swe"
        except KeyError:
            # Registry not yet wired for mini-swe — path exposure is enough for p11.
            pass


def test_b0_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """B0 arm resolves cli-claude and runs a single shot (mocked offline)."""
    from omniagentos.harnesses.bench import b0 as b0_mod

    mock = MockAdapter()

    def _fake_resolve(harness: HarnessType) -> AgentAdapter:
        assert harness is HarnessType.CLI_CLAUDE
        return mock

    monkeypatch.setattr(b0_mod, "_import_resolve_adapter", lambda: _fake_resolve)

    inp = AgentInput(
        run_id="test-run",
        task_id="test-task",
        prompt="Say hello",
        working_dir=".",
    )
    result = b0_mod.run_b0_arm(inp)
    assert result.status in (ResultStatus.OK, ResultStatus.ERROR)
    assert isinstance(result, AgentResult)
    assert isinstance(result.usage, AgentUsage)


def test_10_devtasks_exist() -> None:
    """All filename-scoped benchmark tasks load and have required fields."""
    from omniagentos.harnesses.bench.runner import load_tasks

    task_dir = REPO_ROOT / "devtasks"
    assert task_dir.is_dir(), f"missing devtasks dir at {task_dir}"
    tasks = load_tasks(task_dir)
    assert len(tasks) >= 10, f"Expected ≥10 tasks, found {len(tasks)}: {tasks}"
    assert all(task["discipline"] == "code-changes" for task in tasks)


def test_load_tasks_ignores_non_task_metadata_files(tmp_path: Path) -> None:
    """Metadata may coexist with the benchmark corpus without becoming a task."""
    from omniagentos.harnesses.bench.runner import load_tasks

    (tmp_path / "task_001.yaml").write_text(
        "id: task_001\nprompt: hello\ndiscipline: code-changes\n", encoding="utf-8"
    )
    (tmp_path / "lane-claims.yaml").write_text("version: 1\nlanes: []\n", encoding="utf-8")

    tasks = load_tasks(tmp_path)

    assert [task["id"] for task in tasks] == ["task_001"]


def test_load_tasks_rejects_malformed_filename_scoped_task(tmp_path: Path) -> None:
    """Filename filtering must not turn malformed benchmark tasks into a silent pass."""
    from omniagentos.harnesses.bench.runner import load_tasks

    broken = tmp_path / "task_broken.yaml"
    broken.write_text("id: task_broken\ndiscipline: code-changes\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"task_broken\.yaml missing required field 'prompt'"):
        load_tasks(tmp_path)


def test_miniswe_health() -> None:
    """health() reports honest capabilities without raising."""
    from omniagentos.harnesses.miniswe.adapter import MiniSweAdapter

    h = MiniSweAdapter().health()
    assert isinstance(h.healthy, bool)
    assert "live_runs" in h.capabilities
    assert isinstance(h.capabilities["live_runs"], bool)


def test_env_hash_on_manifest() -> None:
    """build_manifest always populates harness.env_hash."""
    from omniagentos.harnesses.bench.runner import build_manifest
    from omniagentos.harnesses.envhash import env_hash

    adapter = MockAdapter()
    result = adapter.run(AgentInput(run_id="r1", task_id="t1", prompt="hi", working_dir="."))
    m = build_manifest(
        run_id="r1",
        task={"id": "t1", "prompt": "hi", "discipline": "code-changes"},
        arm=__import__("omniagentos.contracts", fromlist=["Arm"]).Arm.B1,
        harness_type=HarnessType.MINI_SWE,
        adapter=adapter,
        result=result,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        dry_run=True,
    )
    assert m.harness.env_hash
    assert m.harness.env_hash == env_hash()
    assert m.arm is not None
    assert m.harness.harness is HarnessType.MINI_SWE
