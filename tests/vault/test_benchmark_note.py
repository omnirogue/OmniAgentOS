"""render_benchmark_note (contracts/interfaces.md §p05: "Benchmark notes link
every run note they aggregate")."""

from __future__ import annotations

from omniagentos.contracts import (
    AgentUsage,
    Arm,
    HarnessProfile,
    HarnessType,
    NoteType,
    RunManifest,
    RunState,
)
from omniagentos.vault import parse_frontmatter, render_benchmark_note


def _manifest(**overrides: object) -> RunManifest:
    base: dict[str, object] = dict(
        run_id="run_bench_1",
        task_id="tsk_1",
        discipline="code-changes",
        arm=Arm.B0,
        harness=HarnessProfile(harness=HarnessType.CLI_CLAUDE, version="1.0", env_hash="h"),
        model="claude-sonnet",
        state=RunState.COMPLETED,
        usage=AgentUsage(
            wall_ms=1000, input_tokens=100, output_tokens=50, cost_usd=0.05,
            estimated=False, source="cli-report",
        ),
    )
    base.update(overrides)
    return RunManifest(**base)  # type: ignore[arg-type]


def test_relpath_is_benchmarks_bench_id() -> None:
    relpath, _content = render_benchmark_note("b0-vs-b1", [_manifest()])
    assert relpath == "benchmarks/b0-vs-b1.md"


def test_frontmatter_type_is_benchmark() -> None:
    _relpath, content = render_benchmark_note("b0-vs-b1", [_manifest()])
    fm = parse_frontmatter(content)
    assert fm.id == "b0-vs-b1"
    assert fm.type == NoteType.BENCHMARK
    assert fm.status == "active"


def test_links_every_aggregated_run() -> None:
    manifests = [
        _manifest(run_id="run_a1", arm=Arm.B0),
        _manifest(run_id="run_a2", arm=Arm.B1),
        _manifest(run_id="run_a3", arm=Arm.CHAMPION),
    ]
    _relpath, content = render_benchmark_note("triple", manifests)
    assert "[[run_a1]]" in content
    assert "[[run_a2]]" in content
    assert "[[run_a3]]" in content


def test_benchmark_note_is_not_an_orphan_even_when_empty() -> None:
    _relpath, content = render_benchmark_note("empty-bench", [])
    assert "[[Home]]" in content
    fm = parse_frontmatter(content)
    assert fm.id == "empty-bench"


def test_common_discipline_is_recorded() -> None:
    manifests = [
        _manifest(run_id="run_x1", discipline="code-changes"),
        _manifest(run_id="run_x2", discipline="code-changes"),
    ]
    _relpath, content = render_benchmark_note("same-discipline", manifests)
    fm = parse_frontmatter(content)
    assert fm.discipline == "code-changes"


def test_mixed_discipline_is_null() -> None:
    manifests = [
        _manifest(run_id="run_y1", discipline="code-changes"),
        _manifest(run_id="run_y2", discipline="research-briefs"),
    ]
    _relpath, content = render_benchmark_note("mixed-discipline", manifests)
    fm = parse_frontmatter(content)
    assert fm.discipline is None


def test_estimated_flag_surfaced_for_benchmarks_too() -> None:
    manifests = [
        _manifest(
            run_id="run_z1",
            usage=AgentUsage(wall_ms=1, cost_usd=0.1, estimated=True, source="estimator"),
        )
    ]
    _relpath, content = render_benchmark_note("bench-estimated", manifests)
    summary = content.split("## Summary", 1)[1].split("## Runs", 1)[0]
    assert "Estimated:" in summary
    assert "**yes**" in summary


def test_missing_usage_counts_as_estimated_and_not_reported() -> None:
    manifests = [_manifest(run_id="run_no_usage", usage=None)]
    _relpath, content = render_benchmark_note("bench-no-usage", manifests)
    assert "not reported" in content
    summary = content.split("## Summary", 1)[1].split("## Runs", 1)[0]
    assert "**yes**" in summary
