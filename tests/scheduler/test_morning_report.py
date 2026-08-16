from omniagentos.contracts import (
    AgentUsage,
    Arm,
    HarnessProfile,
    HarnessType,
    RunManifest,
    RunState,
)
from omniagentos.scheduler import morning_report


def manifest(run_id: str, state: RunState, arm: Arm, harness: HarnessType) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        task_id=f"task-{run_id}",
        state=state,
        arm=arm,
        harness=HarnessProfile(harness=harness),
        usage=AgentUsage(wall_ms=100, input_tokens=10, output_tokens=20, cost_usd=0.5),
    )


def test_dry_run_rolls_up_manifests_and_writes_nothing(monkeypatch, capsys):
    manifests = [
        manifest("one", RunState.COMPLETED, Arm.B0, HarnessType.MOCK),
        manifest("two", RunState.FAILED, Arm.CHAMPION, HarnessType.FUSION),
    ]
    monkeypatch.setattr(morning_report, "default_ledger_dir", lambda: "fake-ledger")
    monkeypatch.setattr(morning_report, "default_vault_dir", lambda: "fake-vault")
    monkeypatch.setattr(
        "omniagentos.ledger.read_manifests", lambda ledger: manifests, raising=False
    )

    result = morning_report.run_report(dry_run=True)

    assert result == {
        "count": 2,
        "by_state": {"completed": 1, "failed": 1},
        "by_arm": {"b0": 1, "champion": 1},
        "by_harness": {"fusion": 1, "mock": 1},
        "total_est_cost_usd": 1.0,
        "total_est_tokens": 60,
        "avg_wall_ms": 100.0,
    }
    assert "benchmarks/morning-" in capsys.readouterr().out


def test_non_dry_run_writes_rendered_note(monkeypatch, tmp_path):
    calls = []
    manifests = [manifest("one", RunState.COMPLETED, Arm.B0, HarnessType.MOCK)]
    monkeypatch.setattr(
        "omniagentos.ledger.read_manifests", lambda ledger: manifests, raising=False
    )
    monkeypatch.setattr(
        "omniagentos.vault.render_benchmark_note",
        lambda bench_id, received: ("benchmarks/example.md", "content"),
        raising=False,
    )
    monkeypatch.setattr(
        "omniagentos.vault.write_note",
        lambda vault, relpath, content: calls.append((vault, relpath, content)),
        raising=False,
    )

    # Was the literal relative string "vault", which resolves against the process
    # cwd to the OPERATOR vault when pytest runs from the repo root — the write
    # only missed it because write_note is stubbed above. An absolute isolated
    # path asserts the same thing (the resolved dir is threaded to write_note)
    # without naming live state.
    vault_dir = tmp_path / "vault"
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))
    morning_report.run_report()

    assert calls == [(str(vault_dir), "benchmarks/example.md", "content")]
