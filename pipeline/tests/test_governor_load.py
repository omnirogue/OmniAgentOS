"""The load gate, tested against recorded traces rather than wall-clock luck.

`sample_load_until_clear` takes its sampler and its sleeper by injection for
exactly this reason: a policy about load is only testable if you can hand it the
load. Every trace below is either a real excursion copied out of
`var/gate-evidence/accurate/merge-gate/*/receipt.json` or a shape derived from
one.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import governor as g


def fake(seq):
    """A sampler that walks a trace and then repeats its last value forever."""
    box = {"i": 0}

    def sampler():
        i = min(box["i"], len(seq) - 1)
        box["i"] += 1
        return seq[i]

    return sampler


def norest():
    slept = []
    return slept, slept.append


# ------------------------------------------------------------------ policy --


def test_clear_on_first_sample_never_waits():
    slept, sleeper = norest()
    r = g.sample_load_until_clear(16.0, sampler=fake([3.0]), sleeper=sleeper)
    assert r.clear
    assert r.waited_s == 0.0
    assert slept == [], "a quiet box must not be made to wait"


def test_decaying_transient_clears_and_reports_the_wait():
    slept, sleeper = norest()
    r = g.sample_load_until_clear(16.0, sampler=fake([24.0, 20.0, 15.0]),
                                  interval=15.0, sleeper=sleeper)
    assert r.clear
    assert r.samples == [24.0, 20.0, 15.0]
    assert r.waited_s == 30.0
    assert slept == [15.0, 15.0]


def test_rising_spike_abandons_after_one_interval():
    """The measured 2026-08-08 excursion: 11.83 -> 32.05 -> 44.49 -> 53.61.

    A blind retry would sit through all four samples and still block. The point
    of the derivative test is that this costs ONE interval, not four.
    """
    _slept, sleeper = norest()
    r = g.sample_load_until_clear(16.0, sampler=fake([32.05, 44.49, 53.61, 60.55]),
                                  attempts=6, interval=15.0, sleeper=sleeper)
    assert not r.clear
    assert len(r.samples) == 2, "must abandon on the first non-decaying step"
    assert r.waited_s == 15.0
    assert "not decaying" in r.reason


def test_flat_load_is_not_a_falling_series():
    _slept, sleeper = norest()
    r = g.sample_load_until_clear(16.0, sampler=fake([20.0, 19.9, 19.8]),
                                  attempts=5, sleeper=sleeper)
    assert not r.clear
    assert len(r.samples) == 2, "noise-sized decay must not buy another wait"


def test_slow_but_real_decay_keeps_waiting_to_the_attempt_cap():
    _slept, sleeper = norest()
    trace = [30.0, 28.0, 26.0, 24.0, 22.0, 20.0]
    r = g.sample_load_until_clear(16.0, sampler=fake(trace), attempts=4, sleeper=sleeper)
    assert not r.clear
    assert len(r.samples) == 4, "attempts is a hard cap even while decaying"
    assert "after 45s of re-sampling" in r.reason


def test_unmeasurable_load_refuses_and_never_reads_as_idle():
    r = g.sample_load_until_clear(16.0, sampler=lambda: None, sleeper=lambda s: None)
    assert not r.clear
    assert r.load is None
    assert "UNKNOWN never passes" in r.reason


def test_ceiling_boundary_is_inclusive():
    r = g.sample_load_until_clear(16.0, sampler=fake([16.0]), sleeper=lambda s: None)
    assert r.clear, "load == ceiling is not OVER the ceiling"


def test_attempts_below_one_is_coerced_not_crashed():
    r = g.sample_load_until_clear(16.0, attempts=0, sampler=fake([99.0]),
                                  sleeper=lambda s: None)
    assert not r.clear
    assert len(r.samples) == 1


# ----------------------------------------------------------------- ceiling --


def test_perf_core_count_is_performance_cores_not_logical():
    """The whole reconciliation of governor 24 vs AccurateGate 16.

    On this M2 Ultra: 16 performance + 8 efficiency = 24 logical. The contract
    says performance cores; `os.cpu_count()` answers 24.
    """
    perf = g.perf_core_count()
    if perf is None:
        pytest.skip("no sysctl on this host")
    import os
    assert perf > 0
    assert perf <= (os.cpu_count() or perf)
    if (os.cpu_count() or 0) == 24:
        assert perf == 16, "16 P-cores is what the gate receipts have recorded all along"


def test_build_budget_corrects_a_logical_core_ceiling(tmp_path):
    """bootstrap.sh wrote hw.ncpu into load_avg_1m_max. That must be repaired,
    not merely defaulted around — `setdefault` would leave the wrong value."""
    import os
    perf = g.perf_core_count()
    if perf is None or perf == os.cpu_count():
        pytest.skip("host has no perf/logical split to detect")
    previous = {"load_avg_1m_max": os.cpu_count(), "subscription": {}}
    out = g.build_budget(tmp_path, previous, probe_claude=False)
    assert out["load_avg_1m_max"] == perf
    assert "performance-core" in out["load_avg_1m_max_basis"]


def test_build_budget_leaves_a_deliberate_operator_ceiling_alone(tmp_path):
    """Only the two known-buggy values are corrected. An operator who set 8
    on purpose keeps 8."""
    out = g.build_budget(tmp_path, {"load_avg_1m_max": 8, "subscription": {}},
                         probe_claude=False)
    assert out["load_avg_1m_max"] == 8


def test_claude_probe_age_uses_utc_epoch_in_non_utc_timezone(monkeypatch):
    """time.time() - time.mktime(...) misreads a UTC probe stamp as local wall
    time. Under time.mktime in America/New_York, a probe genuinely 3900s
    (65 min) old computes an age near -10,500s -- deeply negative and well
    under the CLAUDE_PROBE_SECONDS*2 (3600s) staleness ceiling, so a truly
    stale probe is wrongly reported fresh. The correct UTC-epoch age (~3900s)
    correctly crosses that ceiling. This must fail on any implementation of
    the age calculation that routes through time.mktime."""
    import os
    import time as _time

    prev_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        probed_at = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 3900))
        previous = {
            "subscription": {
                "claude_pool": {"live_count": 3, "probed_at": probed_at, "stale": False}
            }
        }
        out = g.build_budget(Path("/tmp"), previous, probe_claude=False)
        pool = out["subscription"]["claude_pool"]
        assert pool["stale"] is True, (
            "a probe genuinely 65 minutes old must be marked stale under UTC epoch math")
    finally:
        if prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev_tz
        if hasattr(_time, "tzset"):
            _time.tzset()


# ------------------------------------------------------------------- check --


def _budget(tmp_path, **over):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    body = {"disk_free_gb_min": 20, "load_avg_1m_max": 16}
    body.update(over)
    p = state / "budget.json"
    p.write_text(json.dumps(body))
    return p


def test_check_does_not_back_the_loop_off_for_load(tmp_path, monkeypatch, capsys):
    """The correctness point behind the whole change.

    Load defers the GATE. Idling the loop on load would throw away the
    admission, ledger replay and queue rebuild that the load stop was never
    supposed to touch.
    """
    # This test isolates load policy. GitHub-hosted runners can have less than
    # 20 GiB free, which otherwise adds an unrelated hard disk stop and makes
    # the assertion depend on the runner image size.
    p = _budget(tmp_path, disk_free_gb_min=0)
    monkeypatch.setattr(g, "sample_load_until_clear",
                        lambda *a, **k: g.LoadCheck(False, 16.0, 99.0, [99.0], 0.0,
                                                    "1-min load 99.00 > ceiling 16"))
    assert g.check(p) == g.CHECK_CLEAR
    out = json.loads(capsys.readouterr().out)
    assert out["clear"] is True
    assert out["gate_clear"] is False
    assert out["stops"] == []
    assert "GATE only" in out["notes"][0]


def test_check_backs_off_on_a_hard_limit(tmp_path, monkeypatch, capsys):
    p = _budget(tmp_path, disk_free_gb_min=10 ** 9)
    monkeypatch.setattr(g, "sample_load_until_clear",
                        lambda *a, **k: g.LoadCheck(True, 16.0, 1.0, [1.0], 0.0, "quiet"))
    assert g.check(p) == g.CHECK_BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert any("disk" in s for s in out["stops"])


def test_check_fails_closed_on_an_unreadable_budget(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    p = state / "budget.json"
    p.write_text("{not json")
    monkeypatch.setattr(g, "sample_load_until_clear",
                        lambda *a, **k: g.LoadCheck(True, 16.0, 1.0, [1.0], 0.0, "quiet"))
    assert g.check(p) == g.CHECK_BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert any("fail closed" in s for s in out["stops"])
