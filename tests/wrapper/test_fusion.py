from __future__ import annotations

import json
from pathlib import Path

from omniagentos.contracts import HarnessType, RunState
from omniagentos.wrapper import import_fusion_run

FIXTURES = Path(__file__).parent / "fixtures"


def test_import_fusion_run_maps_sessions_and_ownership_only_packages() -> None:
    manifests = import_fusion_run(str(FIXTURES / "fusion_run"))

    assert len(manifests) == 2
    wrapper = next(manifest for manifest in manifests if manifest.extra["package"] == "wrapper")
    ledger = next(manifest for manifest in manifests if manifest.extra["package"] == "ledger")
    assert wrapper.harness.harness is HarnessType.FUSION
    assert wrapper.state is RunState.COMPLETED
    assert wrapper.agent == "codex"
    assert wrapper.model == "gpt-5"
    assert wrapper.usage is not None
    assert wrapper.usage.source == "imported"
    assert wrapper.usage.estimated is True
    assert wrapper.usage.cost_usd is None
    assert wrapper.usage.wall_ms == 150_000
    assert wrapper.receipts == []
    assert wrapper.extra["findings_count"] == 2
    assert wrapper.extra["validation"]["allPass"] is True
    assert wrapper.extra["fableApproval"] == {"approved": True, "by": "fable"}
    assert ledger.agent == "claude"
    assert ledger.started_at == "2025-01-10T10:00:00Z"


def test_import_fusion_run_handles_ownership_only_directory(tmp_path: Path) -> None:
    (tmp_path / "ownership.json").write_text(
        json.dumps({"packages": {"api": {"agent": "codex", "model": "gpt-5"}}}),
        encoding="utf-8",
    )

    manifests = import_fusion_run(str(tmp_path))

    assert len(manifests) == 1
    assert manifests[0].extra["package"] == "api"
    assert manifests[0].state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
    assert manifests[0].usage is not None
    assert manifests[0].usage.source == "imported"
