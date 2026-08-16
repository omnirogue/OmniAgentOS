from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    IdempotencyReceipt,
    RunManifest,
    RunState,
)
from omniagentos.ledger import append_manifest, read_manifests


def make_manifest(run_id: str, finished_at: str = "2026-07-11T12:00:00Z") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        task_id="tsk_example",
        harness=HarnessProfile(
            harness=HarnessType.MOCK,
            version="1.2.3",
            env_hash="abc123",
            params={"nested": {"answer": 42}},
        ),
        state=RunState.COMPLETED,
        started_at="2026-07-11T11:59:00Z",
        finished_at=finished_at,
        usage=AgentUsage(
            wall_ms=1234,
            turns=2,
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.01,
            estimated=False,
            source="cli-report",
        ),
        receipts=[
            IdempotencyReceipt(
                key="idem_1",
                run_id=run_id,
                step_name="write",
                result_json='{"ok":true}',
                created_at="2026-07-11T12:00:00Z",
                completed_at="2026-07-11T12:00:01Z",
            )
        ],
        extra={"preserve": ["all", "fields"]},
    )


def test_round_trip_including_receipts_and_harness_profile(tmp_path: Path) -> None:
    manifest = make_manifest("run_round_trip")

    append_manifest(str(tmp_path), manifest)

    assert read_manifests(str(tmp_path)) == [manifest]


def test_uses_finished_at_month_for_file_name(tmp_path: Path) -> None:
    path = append_manifest(str(tmp_path), make_manifest("run_july"))

    assert Path(path) == tmp_path / "runs-202607.jsonl"


def test_rollover_and_newest_first_ordering(tmp_path: Path) -> None:
    july = make_manifest("run_july", "2026-07-30T12:00:00Z")
    august_old = make_manifest("run_august_old", "2026-08-01T12:00:00Z")
    august_new = make_manifest("run_august_new", "2026-08-02T12:00:00Z")
    for manifest in (july, august_old, august_new):
        append_manifest(str(tmp_path), manifest)

    assert [manifest.run_id for manifest in read_manifests(str(tmp_path))] == [
        "run_august_new",
        "run_august_old",
        "run_july",
    ]


def test_append_is_idempotent_and_fsyncs(tmp_path: Path) -> None:
    manifest = make_manifest("run_once")
    with patch("omniagentos.ledger.os.fsync") as fsync:
        append_manifest(str(tmp_path), manifest)
        append_manifest(str(tmp_path), manifest)

    assert fsync.call_count == 1
    assert [entry.run_id for entry in read_manifests(str(tmp_path))] == ["run_once"]


def test_skips_corrupt_lines_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    append_manifest(str(tmp_path), make_manifest("run_valid"))
    path = tmp_path / "runs-202607.jsonl"
    with path.open("a", encoding="utf-8") as ledger_file:
        ledger_file.write("not json\n")

    manifests = read_manifests(str(tmp_path))

    assert [manifest.run_id for manifest in manifests] == ["run_valid"]
    assert "Skipping corrupt ledger line" in caplog.text


def test_run_id_filter_and_limit(tmp_path: Path) -> None:
    for index in range(10):
        append_manifest(str(tmp_path), make_manifest(f"run_{index}"))

    assert [manifest.run_id for manifest in read_manifests(str(tmp_path), run_id="run_3")] == [
        "run_3"
    ]
    assert len(read_manifests(str(tmp_path), limit=5)) == 5
