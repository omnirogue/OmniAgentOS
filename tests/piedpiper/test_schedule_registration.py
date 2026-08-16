from __future__ import annotations

from omniagentos.scheduler.system_jobs import CATALOG


def test_catalog_registers_piedpiper_pipeline_daily() -> None:
    entries = [entry for entry in CATALOG if entry.key == "piedpiper-pipeline-daily"]
    assert len(entries) == 1, "piedpiper-pipeline-daily must be registered exactly once"
    entry = entries[0]
    assert entry.module == "omniagentos.piedpiper.pipeline_report"
    assert entry.executor == "launchd"
    assert entry.source == "scripts/scheduler/install-piedpiper-pipeline.sh"
    assert entry.template == "scripts/scheduler/com.omniagentos.piedpiper-pipeline.plist.template"
