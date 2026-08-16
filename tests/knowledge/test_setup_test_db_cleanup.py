"""Regression test: ``setup_test_db`` must DROP its owned test DB when the session ends.

H-31 (``tests/knowledge/db_ownership.py``) already gives every pytest process a
uniquely named ``omniagentos_knowledge_test_<cwd>_<worker>_<token>`` database, so
concurrent runs/workers stop sharing rows. Nothing dropped that database again,
though: measured on the operator host before this fix, 666+ orphaned
``omniagentos_knowledge_test_*`` databases had accumulated from prior runs — every
one of them created, never cleaned up.

This test drives a REAL nested pytest session (a single throwaway probe test,
collected only under ``tests/knowledge/`` so its own ``setup_test_db`` fixture
fires) in a subprocess, records the database name that session claimed, waits for
the subprocess (and therefore the fixture's teardown) to finish, and then asserts
directly against Postgres that the database no longer exists.

Goes RED if the teardown in ``setup_test_db`` (``tests/knowledge/conftest.py``) is
removed or the drop starts swallowing its own success — restore it and this test
is GREEN again (see the commit message / task report for the paired before/after
run).

No exclusion marker: this test only reaches its body once THIS session's own
``setup_test_db`` autouse fixture has already proven PostgreSQL reachable (the
same pattern ``test_e2e_real.py``/``test_runner_integration.py`` rely on), so a
nested pytest session started from here has the same guarantee. It is slower
than the rest of the module (~60-90s: a full nested interpreter, migration, and
drop cycle) but not a benchmark, so it stays in the default suite rather than
behind ``perf``/``live``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import psycopg

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_nested_probe_session(out_file: Path) -> subprocess.CompletedProcess[str]:
    """Run one throwaway pytest session under tests/knowledge/ and capture its DB name."""
    probe = _REPO_ROOT / "tests" / "knowledge" / "_zz_owned_db_cleanup_probe.py"
    probe.write_text(
        textwrap.dedent(f"""\
            # Throwaway — written and deleted by test_setup_test_db_cleanup.py.
            from tests.knowledge.db_ownership import OWNED_TEST_DB


            def test_record_owned_db_name() -> None:
                with open({str(out_file)!r}, "w") as f:
                    f.write(OWNED_TEST_DB)
                assert OWNED_TEST_DB
            """)
    )
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        probe.unlink(missing_ok=True)


def test_setup_test_db_drops_its_database_when_the_session_ends(tmp_path: Path) -> None:
    out_file = tmp_path / "owned_db_name.txt"

    result = _run_nested_probe_session(out_file)
    assert result.returncode == 0, (
        f"nested probe session failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    dbname = out_file.read_text().strip()
    assert dbname, "nested session never recorded an owned DB name"
    assert dbname.startswith("omniagentos_knowledge_test_")

    with psycopg.connect("postgresql://localhost/postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            row = cur.fetchone()
    assert row is None, (
        f"{dbname!r} still exists after its owning pytest session ended — "
        "setup_test_db's teardown did not drop it"
    )
