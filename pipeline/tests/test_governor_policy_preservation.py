"""The governor's WRITER path must not conflate an ABSENT budget.json (first
run: defaults are correct) with a PRESENT-but-unreadable one (an instrument
error: the previous policy is UNKNOWN). Regression coverage for
sha256:f5a3741b8eeba24147ebcec4f44bb2dbdca9e2eeb5cbcfbbc536e7aae86fe9eb.

Drives ``main()`` (the WRITER), not ``check()`` (the already-correct READER,
pinned separately by test_governor_load.py). ``probe_claude_slots`` and
``read_codex_window_pct`` are monkeypatched to constants because they are a
subprocess fan-out and a home-directory rglob — not on the path under test,
and leaving them live makes the test slow and host-dependent.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import governor as g  # noqa: E402

OPERATOR_BUDGET = {
    "disk_free_gb_min": 40,
    "load_avg_1m_max": 12,
    "wip_cap": 60,
    "subscription": {
        "accounts": {
            "codex-1": {"provider": "codex", "window_pct_max": 85},
        },
        "claude_pool": {"live_count": 2, "probed_at": "2026-08-11T06:00:00Z", "stale": False},
    },
    "metered_usd": {
        "row_a": {"spent_today": 1.0, "daily_cap": 5.0},
        "row_b": {"spent_today": 0.0, "daily_cap": 5.0},
    },
    "alert": {"channel": "ntfy", "target": "topic-x"},
    "loop_accounts": ["acct-1", "acct-2"],
    "reset_at_local_midnight": True,
}


def _patch_probes(monkeypatch):
    monkeypatch.setattr(g, "probe_claude_slots",
                        lambda *a, **k: {"live_count": 2, "probed_at": g._now(), "stale": False})
    monkeypatch.setattr(g, "read_codex_window_pct", lambda: 10.0)


def _run_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["governor.py", "--loops-root", str(tmp_path), "--once"])
    return g.main()


def _read_alerts(tmp_path) -> str:
    p = tmp_path / "ALERTS.md"
    return p.read_text() if p.exists() else ""


def _read_ledger_events(tmp_path) -> list[dict]:
    p = tmp_path / "ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_an_unreadable_budget_is_not_rewritten_from_defaults(tmp_path, monkeypatch):
    _patch_probes(monkeypatch)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    budget_path = tmp_path / "state" / "budget.json"

    # Seed an operator budget via a normal, healthy tick.
    budget_path.write_text(json.dumps(OPERATOR_BUDGET))
    rc = _run_once(tmp_path, monkeypatch)
    assert rc == 0
    seeded_bytes = budget_path.read_bytes()
    seeded = json.loads(seeded_bytes)
    assert seeded["wip_cap"] == 60

    # Corrupt the file — present but unreadable.
    budget_path.write_text("{not valid json")
    before = budget_path.read_bytes()

    rc = _run_once(tmp_path, monkeypatch)

    after = budget_path.read_bytes()
    assert after == before, "the file must be byte-unchanged after a refusal"
    assert rc == 2

    reloaded = json.loads(seeded_bytes)
    assert reloaded["wip_cap"] == 60
    assert reloaded["disk_free_gb_min"] == 40
    assert reloaded["load_avg_1m_max"] == 12
    assert reloaded["metered_usd"] == seeded["metered_usd"]
    assert reloaded["subscription"]["accounts"] == seeded["subscription"]["accounts"]
    assert reloaded["alert"] == seeded["alert"]
    assert reloaded["loop_accounts"] == seeded["loop_accounts"]
    assert reloaded["reset_at_local_midnight"] is True

    assert "budget-unreadable" in _read_alerts(tmp_path) or "unreadable" in _read_alerts(tmp_path)
    events = _read_ledger_events(tmp_path)
    assert any(e.get("event") == "instrument_error" for e in events)


def test_an_absent_budget_is_a_first_run_not_an_error(tmp_path, monkeypatch):
    _patch_probes(monkeypatch)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    budget_path = tmp_path / "state" / "budget.json"
    assert not budget_path.exists()

    rc = _run_once(tmp_path, monkeypatch)

    assert rc == 0
    budget = json.loads(budget_path.read_text())
    assert budget["wip_cap"] == 4
    assert budget["updated_by"] == "governor"
    assert _read_alerts(tmp_path) == ""
    assert _read_ledger_events(tmp_path) == []


@pytest.mark.parametrize("bad_content", ["[]", "", "[1, 2]", '"nope"'])
def test_non_object_budgets_refuse_uniformly(tmp_path, monkeypatch, bad_content):
    _patch_probes(monkeypatch)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    budget_path = tmp_path / "state" / "budget.json"
    budget_path.write_text(bad_content)
    before = budget_path.read_bytes()

    rc = _run_once(tmp_path, monkeypatch)

    assert rc == 2
    assert budget_path.read_bytes() == before


def test_refusal_alerts_once_per_episode(tmp_path, monkeypatch):
    _patch_probes(monkeypatch)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    budget_path = tmp_path / "state" / "budget.json"
    budget_path.write_text("{stuck corrupt bytes 1")

    for _ in range(10):
        rc = _run_once(tmp_path, monkeypatch)
        assert rc == 2

    lines_after_stuck = [ln for ln in _read_alerts(tmp_path).splitlines() if ln.strip()]
    assert len(lines_after_stuck) == 1

    budget_path.write_text("{different corrupt bytes 2 zzz")
    rc = _run_once(tmp_path, monkeypatch)
    assert rc == 2

    lines_after_new_episode = [ln for ln in _read_alerts(tmp_path).splitlines() if ln.strip()]
    assert len(lines_after_new_episode) == 2
