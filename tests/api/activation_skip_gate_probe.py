"""Deliberate all-skipped probe for D5 activation gate tests.

Not named test_*.py so the default pytest collection does not pick it up.
Activation tests invoke it explicitly via:

    pytest tests/api/activation_skip_gate_probe.py

Pytest exits 0 for an all-skipped suite — that must NOT activate (D5).
"""

import pytest


def test_deliberate_activation_skip() -> None:
    pytest.skip("deliberate all-skipped activation gate suite")
