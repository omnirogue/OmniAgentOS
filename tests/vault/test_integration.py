"""End-to-end: render -> write -> resolve -> aggregate -> home, all through the
public omniagentos.vault API, against a vault_dir seeded with the real Home.md
(as it would look in the actual repo)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import (
    AgentUsage,
    Arm,
    HarnessProfile,
    HarnessType,
    IdempotencyReceipt,
    NoteType,
    RunManifest,
    RunState,
)
from omniagentos.vault import (
    parse_frontmatter,
    render_benchmark_note,
    render_run_note,
    update_home,
    write_note,
)


def test_full_pipeline_run_note_benchmark_and_home(vault_dir: Path) -> None:
    run = {
        "id": "run_pipeline0000000000001",
        "task_id": "tsk_pipeline000000000001",
        "task_title": "Add retry/backoff to the CLI adapter",
        "discipline": "code-changes",
        "arm": "b1",
        "harness": "cli-claude",
        "model": "claude-sonnet",
        "state": "completed",
        "queued_at": "2026-07-11T15:20:00Z",
        "started_at": "2026-07-11T15:20:05Z",
        "finished_at": "2026-07-11T15:24:26Z",
        "wall_ms": 261000,
        "turns": 4,
        "input_tokens": 8000,
        "output_tokens": 1200,
        "cost_usd": 0.31,
        "usage_estimated": False,
        "usage_source": "cli-report",
        "trace_id": "trace-pipeline",
        "artifacts": ["patch.diff"],
    }
    steps = [
        {"seq": 1, "name": "plan", "status": "completed", "started_at": "t0", "finished_at": "t1"},
        {"seq": 2, "name": "apply_patch", "status": "completed", "started_at": "t1", "finished_at": "t2"},
    ]
    receipts = [
        IdempotencyReceipt(
            key="idem_apply_patch",
            run_id=run["id"],
            step_name="apply_patch",
            result_json='{"changed": 1}',
            created_at="2026-07-11T15:21:00Z",
            completed_at="2026-07-11T15:21:30Z",
        )
    ]

    relpath, content = render_run_note(run, steps, "ledger/runs-202607.jsonl", receipts)
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)

    # 1. the run note itself round-trips its frontmatter
    written = Path(abs_path).read_text(encoding="utf-8")
    fm = parse_frontmatter(written)
    assert fm.id == run["id"]
    assert fm.type == NoteType.RUN
    assert fm.discipline == "code-changes"

    # 2. required wikilinks are present AND resolve to real files
    assert "[[code-changes]]" in written
    assert "[[Home]]" in written
    assert (vault_dir / "disciplines" / "code-changes.md").is_file()
    assert (vault_dir / "Home.md").is_file()

    # 3. task title is plain text, not a wikilink; estimated flag surfaced
    assert "Add retry/backoff to the CLI adapter" in written
    assert "[[Add retry/backoff to the CLI adapter]]" not in written
    assert "**Estimated:** **no** (source: cli-report)" in written

    # 4. aggregate into a benchmark note that links back to this run
    manifest = RunManifest(
        run_id=run["id"],
        task_id=run["task_id"],
        discipline=run["discipline"],
        arm=Arm.B1,
        harness=HarnessProfile(harness=HarnessType.CLI_CLAUDE, version="2.1", env_hash="abc"),
        model=run["model"],
        state=RunState.COMPLETED,
        usage=AgentUsage(
            wall_ms=run["wall_ms"],
            input_tokens=run["input_tokens"],
            output_tokens=run["output_tokens"],
            cost_usd=run["cost_usd"],
            estimated=False,
            source="cli-report",
        ),
        receipts=receipts,
        trace_id=run["trace_id"],
    )
    bench_relpath, bench_content = render_benchmark_note("pipeline-check", [manifest])
    bench_abs = write_note(str(vault_dir), bench_relpath, bench_content, autocommit=False)
    bench_written = Path(bench_abs).read_text(encoding="utf-8")
    assert f"[[{run['id']}]]" in bench_written

    # 5. update_home reflects the run without disturbing the rest of the file
    home_before = (vault_dir / "Home.md").read_text(encoding="utf-8")
    update_home(str(vault_dir), {"total_runs": 1, "by_state": {"completed": 1}})
    home_after = (vault_dir / "Home.md").read_text(encoding="utf-8")
    assert home_after != home_before
    assert "## Map" in home_after  # untouched section survives
    assert "- Disciplines: [[research-briefs]] · [[code-changes]]" in home_after
