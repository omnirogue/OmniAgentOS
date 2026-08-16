"""Comprehensive OmniAgentOS suite: max parallelism + full feature matrix.

Run with::

    ./scripts/test-comprehensive.sh
    # or
    pytest -q tests/comprehensive -m "not live"

Optional knobs:

    COMPREHENSIVE_WORKERS=32   # ThreadPool size for stress tests (default 24)
    COMPREHENSIVE_DIAMONDS=48  # concurrent diamond graphs (default 32)
"""
