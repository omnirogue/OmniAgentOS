"""Test configuration and fixtures for the knowledge subsystem."""

from __future__ import annotations

from collections.abc import Generator

import pytest

from omniagentos.knowledge.config import require_pg, test_admin_dsn, test_dsn
from omniagentos.knowledge.migrate import migrate
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder

# H-31 — unique per-session / per-worker test DB ownership. The claim and every
# destructive helper live in tests.knowledge.db_ownership so that this conftest
# and the module-local perf fixture share one identity and one guard.
from tests.knowledge.db_ownership import (
    OWNED_TEST_DB,
    assert_owned,
    force_drop_owned_test_db,
    parse_dbname_from_dsn,
    recreate_owned_test_db,
    terminate_owned_backends,
)


def _role_dsn(role: str) -> str:
    """Derive a role-specific DSN from the base test DSN.

    The base test DSN connects as the OS superuser (trust-auth) — which would make the
    F1 quarantine trigger (keyed on current_user='knowledge_agent') never fire, silently
    passing a test that is supposed to prove the security boundary. So the agent/admin
    fixtures connect as their ACTUAL roles. Under the pre-scram trust-auth dev cluster
    these are passwordless; post-flip, an explicit env DSN (which carries the password)
    takes precedence.
    """
    env = {"knowledge_agent": test_dsn, "knowledge_admin": test_admin_dsn}
    explicit = env[role]()
    if explicit and f"//{role}" in explicit:
        return explicit
    base = test_dsn()
    dbname = parse_dbname_from_dsn(base)
    return f"postgresql://{role}@localhost/{dbname}"


def _truncate_all() -> None:
    """Truncate all knowledge tables as the admin role (knowledge_agent has no TRUNCATE).

    First terminate any OTHER backend on the test DB: a heavy end-to-end runner test can
    leave its process-wide recall connection in an ACTIVE/mid-command state, which holds
    a lock that would make TRUNCATE (ACCESS EXCLUSIVE) block forever ("another command is
    already in progress"). Killing stray backends releases those locks so teardown always
    completes; the reset fixture rebuilds the singleton on the next test.
    """
    import psycopg

    dbname = parse_dbname_from_dsn(test_dsn())
    # Only ever disturb backends on *this* session's owned test DB.
    assert_owned(dbname)
    try:
        with psycopg.connect("postgresql://localhost/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                terminate_owned_backends(cur, dbname)
    except Exception:
        pass

    for dsn in (_role_dsn("knowledge_admin"), test_dsn()):
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "TRUNCATE recall_log, edges, facts, entities, episodes RESTART IDENTITY CASCADE"
                    )
            return
        except Exception:
            continue


def pytest_configure(config: pytest.Config) -> None:
    """Register pytest markers."""
    config.addinivalue_line("markers", "live_ollama: tests that require real Ollama")
    config.addinivalue_line("markers", "live: tests that require external services")
    config.addinivalue_line("markers", "perf: performance benchmarks")


@pytest.fixture(scope="session", autouse=True)
def setup_test_db() -> Generator[None, None, None]:
    """Bootstrap the uniquely owned test database, and DROP it when the session ends.

    Skip ONLY when PostgreSQL is genuinely unreachable AND not required. The earlier
    guard skipped whenever OMNIAGENTOS_REQUIRE_PG was unset — without ever checking
    reachability — which silently hid the ENTIRE knowledge suite on a dev box that had
    PG up. Now: probe first; with REQUIRE_PG=1 an unreachable PG is a hard failure; with
    the flag unset the tests still RUN when PG is up.

    H-31's per-worker/per-run ownership (``tests.knowledge.db_ownership``) already stops
    concurrent runs from sharing rows — every process claims a fresh
    ``omniagentos_knowledge_test_<cwd>_<worker>_<token>`` name — but nothing ever dropped
    that database again: measured on this host before this fix, 666 orphaned
    ``omniagentos_knowledge_test_*`` databases had accumulated from prior runs, none of
    them ever cleaned up. The teardown below force-drops (through the same
    ownership-checked helper used at setup) only the ONE database this session/worker
    just claimed, so a normal run now leaves nothing behind. A killed/crashed worker
    still leaks its DB — that residual gap is a follow-up, not something a fixture
    teardown can close.
    """
    import psycopg

    def _pg_reachable() -> bool:
        try:
            with psycopg.connect("postgresql://localhost/postgres", connect_timeout=3):
                return True
        except Exception:
            return False

    if not _pg_reachable():
        if require_pg():
            raise RuntimeError(
                "OMNIAGENTOS_REQUIRE_PG=1 but PostgreSQL is unreachable at localhost"
            )
        pytest.skip("PostgreSQL unavailable", allow_module_level=True)

    superuser_dsn = "postgresql://localhost/postgres"

    # Bootstrap on the local trust-auth cluster (pre-scram dev). In production this is
    # the privileged ENV_MIGRATE_DSN; migrations always run as a superuser/owner.
    try:
        with psycopg.connect(superuser_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                # Force-drop only this session/worker's owned test DB (prefix + token).
                recreate_owned_test_db(cur, OWNED_TEST_DB)

        # Run migrations on the test database
        test_dsn_val = test_dsn()
        migrate(test_dsn_val)

    except Exception as e:
        if require_pg():
            raise
        pytest.skip(f"Could not bootstrap test DB: {e}", allow_module_level=True)

    try:
        yield
    finally:
        # Best-effort: a dropped connection here must never mask the real test
        # outcome, and a failure to clean up is a (bounded, test-DB-only) leak,
        # not a correctness problem for the run that just finished.
        try:
            with psycopg.connect(superuser_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    force_drop_owned_test_db(cur, OWNED_TEST_DB)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _reset_recall_singleton() -> Generator[None, None, None]:
    """Reset the recall module's process-wide store singleton around every test.

    The singleton keeps a live PG connection between calls; a query interrupted
    mid-flight in one test (pytest-timeout SIGALRM) would otherwise leave the shared
    connection ACTIVE and hang every subsequent test that reuses it. Import lazily so
    tests that don't touch recall (and pre-recall-merge checkouts) are unaffected.
    """
    try:
        from omniagentos.knowledge import recall as _recall

        _recall.reset_process_state()
    except Exception:
        _recall = None
    yield
    if _recall is not None:
        try:
            _recall.reset_process_state()
        except Exception:
            pass


@pytest.fixture
def knowledge_store() -> Generator[KnowledgeStore, None, None]:
    """Fixture: AGENT-role knowledge store (so the quarantine trigger genuinely fires),
    with truncation between tests (run as admin — the agent role cannot TRUNCATE)."""
    store = KnowledgeStore(dsn=_role_dsn("knowledge_agent"), embedder=make_fake_embedder())
    try:
        yield store
    finally:
        store.close()
        _truncate_all()


@pytest.fixture
def knowledge_store_admin() -> Generator[KnowledgeStore, None, None]:
    """Fixture: ADMIN-role knowledge store."""
    store = KnowledgeStore(dsn=_role_dsn("knowledge_admin"), embedder=make_fake_embedder())
    try:
        yield store
    finally:
        store.close()
        _truncate_all()
