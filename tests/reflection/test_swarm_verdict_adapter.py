"""Unit tests for the pump-lane swarm outcome adapter (var/swarm harvesting)."""

from __future__ import annotations

import os
import time

from omniagentos.reflection.adapters import SwarmVerdictSourceAdapter


def _touch(path, *, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def test_discover_windows_by_mtime_and_skips_clones(tmp_path, monkeypatch):
    swarm = tmp_path / "swarm"
    monkeypatch.setenv("OMNIAGENTOS_SWARM_DIR", str(swarm))

    fresh_verdict = swarm / "sol-verdicts" / "sw-runner.md"
    reworked = swarm / "sol-verdicts" / "sw-runner.md.reworked-1"
    stale_verdict = swarm / "sol-verdicts" / "old-lane.md"
    role_log = swarm / "role-log.jsonl"
    stale_ledger = swarm / "fleet-ledger.jsonl"
    clone_file = swarm / "clones" / "lane-a" / "README.md"

    _touch(fresh_verdict, age_seconds=60)
    _touch(reworked, age_seconds=30)
    _touch(stale_verdict, age_seconds=7200)
    _touch(role_log, age_seconds=90)
    _touch(stale_ledger, age_seconds=7200)
    _touch(clone_file, age_seconds=10)

    window_start = time.time() - 3600
    found = SwarmVerdictSourceAdapter().discover(window_start)

    names = [p.name for p in found]
    assert "sw-runner.md" in names
    assert "sw-runner.md.reworked-1" in names
    assert "role-log.jsonl" in names
    assert "old-lane.md" not in names  # outside the window
    assert "fleet-ledger.jsonl" not in names  # outside the window
    assert "README.md" not in names  # clones/ is never scanned
    # Newest first.
    mtimes = [p.stat().st_mtime for p in found]
    assert mtimes == sorted(mtimes, reverse=True)


def test_discover_missing_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAGENTOS_SWARM_DIR", str(tmp_path / "nope"))
    assert SwarmVerdictSourceAdapter().discover(0.0) == []


def test_extract_digest_and_caps(tmp_path, monkeypatch):
    swarm = tmp_path / "swarm"
    monkeypatch.setenv("OMNIAGENTOS_SWARM_DIR", str(swarm))
    verdict = swarm / "sol-verdicts" / "sw-runner.md"
    verdict.parent.mkdir(parents=True)
    verdict.write_text(
        "VERDICT: REJECT\nReviewer: gpt-5.6-sol (OpenAI), cross-lineage reviewer of grok-4.5.\n"
        + ("filler line\n" * 200),
        encoding="utf-8",
    )

    digest = SwarmVerdictSourceAdapter().extract(verdict, byte_cap=256, token_cap=8000)
    assert digest.source_name == "swarm:sol-verdicts/sw-runner.md"
    assert "VERDICT: REJECT" in digest.summary_or_sample
    assert digest.bytes_read > 0
    assert "byte_cap" in digest.caps_hit

    ledger = swarm / "role-log.jsonl"
    ledger.write_text('{"role": "bottleneck-finder"}\n', encoding="utf-8")
    ledger_digest = SwarmVerdictSourceAdapter().extract(ledger, byte_cap=4096, token_cap=8000)
    assert ledger_digest.source_name == "swarm:role-log.jsonl"
    assert ledger_digest.caps_hit == []
