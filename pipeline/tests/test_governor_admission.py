"""``admit()`` — the pre-spawn admission decision (the operator 2026-08-09, "stop load-171
recurring"). Load-171 was a blind spawn dispatched onto a box whose load could
not be read; this is the guard that makes that unrepresentable.

Three verdicts only: ``go``, ``wait``, ``route-twin``. The one rule every test
below drives at: **an unmeasurable input must resolve to ``wait``, never
``go``.** ``admit()`` is pure — no sleeping, no subprocess, no budget.json
mutation — so every dependency (the local load reader, the core-count reader,
the ceiling, the twin reader) is passed in or monkeypatched, exactly like
``sample_load_until_clear`` is tested in test_governor_load.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import governor as g  # noqa: E402


def _twin_free():
    return 2.0  # a quiet twin


def _twin_busy():
    return 40.0  # an over-ceiling twin


def _twin_unmeasurable():
    return None


# --------------------------------------------------------------------- go --


def test_local_load_below_ceiling_admits_go():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 3.0,
        core_reader=lambda: 16,
    )
    assert decision["verdict"] == "go"


def test_local_load_at_ceiling_boundary_is_go():
    """Consistent with sample_load_until_clear: == ceiling is not OVER it."""
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 16.0,
        core_reader=lambda: 16,
    )
    assert decision["verdict"] == "go"


# ---------------------------------------------------------------- route-twin --


def test_over_ceiling_with_free_twin_routes_to_twin():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 40.0,
        core_reader=lambda: 16,
        twin_reader=_twin_free,
    )
    assert decision["verdict"] == "route-twin"


# --------------------------------------------------------------------- wait --


def test_over_ceiling_with_busy_twin_waits():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 40.0,
        core_reader=lambda: 16,
        twin_reader=_twin_busy,
    )
    assert decision["verdict"] == "wait"


def test_over_ceiling_with_no_twin_reader_waits():
    """No twin measurement available in-process at all -> wait, not go."""
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 40.0,
        core_reader=lambda: 16,
        twin_reader=None,
    )
    assert decision["verdict"] == "wait"


def test_over_ceiling_with_unmeasurable_twin_waits():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 40.0,
        core_reader=lambda: 16,
        twin_reader=_twin_unmeasurable,
    )
    assert decision["verdict"] == "wait"


def test_unreadable_local_load_waits_never_goes():
    """The load-bearing requirement: read_load_1m() returning None must never
    read as headroom."""
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: None,
        core_reader=lambda: 16,
        twin_reader=_twin_free,
    )
    assert decision["verdict"] == "wait"


def test_load_reader_raising_waits_and_does_not_propagate():
    def boom():
        raise RuntimeError("os.getloadavg exploded")

    decision = g.admit(
        "gate", 1,
        load_reader=boom,
        core_reader=lambda: 16,
        twin_reader=_twin_free,
    )
    assert decision["verdict"] == "wait"


def test_core_count_unavailable_waits():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 3.0,
        core_reader=lambda: None,
    )
    assert decision["verdict"] == "wait"


def test_malformed_ceiling_waits():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 3.0,
        core_reader=lambda: 16,
        ceiling=float("nan"),
    )
    assert decision["verdict"] == "wait"


def test_core_reader_raising_waits_and_does_not_propagate():
    def boom():
        raise OSError("sysctl unavailable")

    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 3.0,
        core_reader=boom,
    )
    assert decision["verdict"] == "wait"


def test_twin_reader_raising_waits_and_does_not_propagate():
    def boom():
        raise RuntimeError("twin probe exploded")

    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 40.0,
        core_reader=lambda: 16,
        twin_reader=boom,
    )
    assert decision["verdict"] == "wait"


# -------------------------------------------------------------------- shape --


def test_admit_returns_dict_with_reason_and_evidence():
    decision = g.admit(
        "gate", 1,
        load_reader=lambda: 3.0,
        core_reader=lambda: 16,
    )
    assert decision["verdict"] == "go"
    assert isinstance(decision["reason"], str) and decision["reason"]
    assert decision["kind"] == "gate"
    assert decision["cost"] == 1
    assert decision["load"] == 3.0
    assert decision["ceiling"] == 16.0
