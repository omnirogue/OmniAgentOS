"""Deliberate always-fail probe for D5 activation gate tests.

Not named test_*.py so the default pytest collection does not pick it up.
Activation tests invoke it explicitly via:

    pytest tests/api/activation_fail_gate_probe.py
"""


def test_deliberate_activation_fail() -> None:
    raise AssertionError("deliberate activation gate failure")
