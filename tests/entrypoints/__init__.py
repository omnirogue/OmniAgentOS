"""Production entry-point tests.

These tests enter through the real production surface (ASGI lifespan,
HTTP routes) and observe mechanism *effects* without importing the
mechanism under test. They exist to catch "built and unit-tested but
never wired to the live path" defects (see
devtasks/SIMULATION-RESULTS-selfopt-001-ADDENDUM.md).
"""
