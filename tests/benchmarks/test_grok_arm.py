"""These tests verify the first-class grok arm inside the benchmark ladder using a mock
adapter and the invoke seam. They guarantee no actual grok CLI command or network call
is ever initiated.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import AgentInput, AgentResult, AgentUsage, HarnessType, ResultStatus
from scripts.benchmarks.fixtures import FIXTURES_DIR, load_fixture
from scripts.benchmarks.runner import (
    ARM_DEFAULT_MODELS,
    ARM_TABLE,
    SYNTHETIC_ARMS,
    run_fixture,
)

FIXTURE = FIXTURES_DIR / "fx_002_bugfix_failing_test"


class _RecordingAdapter:
    name = "grok"
    version = "test"

    def __init__(self) -> None:
        self.seen: list[AgentInput] = []

    def run(self, input: AgentInput) -> AgentResult:
        self.seen.append(input)
        return AgentResult(
            status=ResultStatus.OK,
            output_text="ok",
            usage=AgentUsage(
                wall_ms=10,
                turns=1,
                input_tokens=5,
                output_tokens=2,
                cost_usd=0.001,
                estimated=False,
                source="cli-report",
            ),
        )


def test_grok_is_a_registered_non_synthetic_arm() -> None:
    assert "grok" in ARM_TABLE
    arm_enum, harness_type = ARM_TABLE["grok"]
    assert arm_enum is None
    assert harness_type is HarnessType.CLI_GROK
    assert "grok" not in SYNTHETIC_ARMS


def test_grok_resolves_to_the_registry_grok_adapter() -> None:
    from omniagentos.adapters.registry import resolve_adapter
    from omniagentos.harnesses.bench.runner import resolve_arm_adapter
    from omniagentos.mock_adapter import MockAdapter

    assert resolve_arm_adapter("grok") is resolve_adapter(HarnessType.CLI_GROK)
    assert resolve_arm_adapter("grok").name == "grok"
    assert isinstance(resolve_arm_adapter("grok", dry_run=True), MockAdapter)


def test_grok_arm_defaults_to_grok_4_5(tmp_path: Path) -> None:
    assert ARM_DEFAULT_MODELS["grok"] == "grok-4.5"
    adapter = _RecordingAdapter()
    fixture = load_fixture(FIXTURE)
    record = run_fixture(
        fixture,
        "grok",
        capture_id="cap_grok",
        workspace_root=tmp_path / "ws",
        adapter=adapter,
    )
    assert len(adapter.seen) == 1
    assert adapter.seen[0].model == "grok-4.5"
    assert record["model"] == "grok-4.5"


def test_explicit_model_overrides_the_arm_default(tmp_path: Path) -> None:
    adapter = _RecordingAdapter()
    fixture = load_fixture(FIXTURE)
    record = run_fixture(
        fixture,
        "grok",
        capture_id="cap_grok",
        workspace_root=tmp_path / "ws",
        adapter=adapter,
        model="grok-4.5-fast",
    )
    assert len(adapter.seen) == 1
    assert adapter.seen[0].model == "grok-4.5-fast"
    assert record["model"] == "grok-4.5-fast"


def test_grok_record_is_labelled_as_a_real_arm(tmp_path: Path) -> None:
    adapter = _RecordingAdapter()
    fixture = load_fixture(FIXTURE)
    record = run_fixture(
        fixture,
        "grok",
        capture_id="cap_grok",
        workspace_root=tmp_path / "ws",
        adapter=adapter,
    )
    assert record["arm"] == "grok"
    assert record["synthetic"] is False
    assert record["arm_enum"] is None
    assert record["harness"]["harness"] == "cli-grok"
    assert record["harness"]["env_hash"]
    assert record["harness"]["params"]["fixture_id"] == fixture.id
    assert record["manifest"]["harness"]["params"].get("dry_run") is None
    assert record["usage"]["source"] == "cli-report"


def test_a_synthetic_arm_keeps_its_null_model(tmp_path: Path) -> None:
    fixture = load_fixture(FIXTURE)
    record = run_fixture(
        fixture,
        "oracle",
        capture_id="cap_o",
        workspace_root=tmp_path / "ws",
    )
    assert record["model"] is None
    assert record["synthetic"] is True
