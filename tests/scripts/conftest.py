"""Minimal conftest for tests/scripts/.

Correction: pytest has NO mechanism by which a conftest.py here could "disable" the
repo-wide tests/conftest.py — pytest loads every conftest.py on the path from rootdir
down to a collected test's directory, so tests/conftest.py's autouse fixtures (DB/
knowledge/ledger/vault isolation, auth no-op, etc.) still run for tests collected under
tests/scripts/, this file included. This is harmless for test_lane_done.py specifically
because it never touches any of the resources those fixtures guard directly — it only
drives lane_done.py as a subprocess with an explicitly-constructed environment (see
_run_lane_done's env=env), so repo-wide fixtures mutating this pytest process's own
os.environ do not leak into the subprocess under test. This file exists as a landing
spot for any FUTURE fixtures specific to tests/scripts/, not to shadow the parent.
"""
