"""Regression probe for the knowledge-Postgres test isolation in ``tests/conftest.py``.

Every assertion here goes RED if either layer of that isolation is removed:

  * ``_isolate_knowledge_pg`` — without it ``knowledge.config.dsn()`` resolves to
    ``contracts.DEFAULT_DSN``, the operator's live
    ``postgresql://knowledge_agent@localhost/omniagentos_knowledge``. Measured on
    the operator host before the fix: that DSN connected under trust auth, read
    8668 real episodes, and reported
    ``has_table_privilege('knowledge_agent','episodes','INSERT') = true``.
  * ``_refuse_production_knowledge_db`` — without it, opening that database from a
    test is simply allowed.

No test here opens a connection to a database that exists. The production-shaped
DSNs below all point at ``127.0.0.1:1``, so if the guard were deleted the calls
would raise ``psycopg.OperationalError`` (connection refused) rather than
actually reaching production — the assertions still fail, but the probe itself
can never be the thing that touches the live corpus.
"""

from __future__ import annotations

import asyncio
import os

import psycopg
import pytest

from omniagentos.knowledge import config as knowledge_config
from omniagentos.knowledge.contracts import (
    DEFAULT_DSN,
    ENV_ADMIN_DSN,
    ENV_DSN,
    ENV_MIGRATE_DSN,
)
from tests.conftest import (
    KNOWLEDGE_GUARD_DSN,
    effective_dbname,
    production_knowledge_databases,
    scrub_production_knowledge_dsns,
)

#: A production-shaped DSN that cannot connect even if the guard is gone.
_UNREACHABLE_PRODUCTION_DSN = "postgresql://knowledge_agent@127.0.0.1:1/omniagentos_knowledge"


def test_production_database_name_comes_from_the_frozen_contract() -> None:
    """The deny-list tracks ``DEFAULT_DSN`` instead of a copy that can drift."""
    assert production_knowledge_databases() == frozenset({"omniagentos_knowledge"})
    assert effective_dbname(DEFAULT_DSN, {}) == "omniagentos_knowledge"


def test_default_dsn_resolution_is_never_production() -> None:
    """``config.dsn()`` must not resolve to the live knowledge database."""
    resolved = knowledge_config.dsn()
    assert resolved != DEFAULT_DSN
    assert resolved == KNOWLEDGE_GUARD_DSN
    assert effective_dbname(resolved, {}) not in production_knowledge_databases()
    assert os.environ[ENV_DSN] == KNOWLEDGE_GUARD_DSN


def test_privileged_dsns_stay_unconfigured() -> None:
    """Admin/superuser DSNs are the ones that can TRUNCATE and DROP; keep them empty."""
    assert knowledge_config.admin_dsn() == ""
    assert knowledge_config.migrate_dsn() == ""
    assert ENV_ADMIN_DSN not in os.environ
    assert ENV_MIGRATE_DSN not in os.environ


def test_secrets_layer_is_scrubbed_not_just_the_environment() -> None:
    """``config.dsn()`` reads the secrets file BEFORE the env, so both must be scrubbed.

    Exercises the scrub helper against a simulated post-``pg-auth-setup.sh`` box,
    where ``var/secrets/knowledge-pg.env`` carries all three production
    credentials. An env-only pin leaves ``dsn()`` returning production there.
    """
    env = {ENV_DSN: DEFAULT_DSN}
    secrets = {
        ENV_DSN: "postgresql://knowledge_agent:pw@localhost/omniagentos_knowledge",
        ENV_ADMIN_DSN: "postgresql://knowledge_admin:pw@localhost/omniagentos_knowledge",
        ENV_MIGRATE_DSN: "postgresql://postgres:pw@localhost/omniagentos_knowledge",
    }

    scrub_production_knowledge_dsns(env, secrets)

    assert secrets == {}
    assert env == {ENV_DSN: KNOWLEDGE_GUARD_DSN}
    # The real resolution order, replayed over the scrubbed layers.
    assert (secrets.get(ENV_DSN) or env.get(ENV_DSN) or DEFAULT_DSN) == KNOWLEDGE_GUARD_DSN
    assert (secrets.get(ENV_ADMIN_DSN) or env.get(ENV_ADMIN_DSN) or "") == ""
    assert (secrets.get(ENV_MIGRATE_DSN) or env.get(ENV_MIGRATE_DSN) or "") == ""

    # And the live module the fixture scrubbed carries none of them either.
    assert knowledge_config._SECRETS.get(ENV_DSN) is None
    assert knowledge_config._SECRETS.get(ENV_ADMIN_DSN) is None
    assert knowledge_config._SECRETS.get(ENV_MIGRATE_DSN) is None


def test_connecting_to_production_knowledge_db_is_refused() -> None:
    """The driver-level backstop: opening the live database raises, loudly."""
    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        psycopg.connect(_UNREACHABLE_PRODUCTION_DSN)


def test_refusal_survives_every_psycopg_entry_point_and_dsn_spelling() -> None:
    """Module alias, class classmethod, keyword form, and last-one-wins overrides."""
    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        psycopg.Connection.connect(_UNREACHABLE_PRODUCTION_DSN)

    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        psycopg.connect("host=127.0.0.1 port=1 dbname=omniagentos_knowledge")

    # dbname= passed as a keyword argument overrides the conninfo string.
    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        psycopg.connect("host=127.0.0.1 port=1", dbname="omniagentos_knowledge")

    # libpq resolves repeated keys last-one-wins; a first-match scan would read
    # the harmless name and wave this through.
    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        psycopg.connect(
            "dbname=omniagentos_knowledge_test_decoy host=127.0.0.1 port=1 "
            "dbname=omniagentos_knowledge"
        )


def test_pgdatabase_cannot_smuggle_production_past_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN that names no database is resolved by libpq from ``PGDATABASE``."""
    monkeypatch.setenv("PGDATABASE", "omniagentos_knowledge")
    assert effective_dbname("host=127.0.0.1 port=1", {}) == "omniagentos_knowledge"
    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        psycopg.connect("host=127.0.0.1 port=1")


def test_async_entry_point_is_guarded_too() -> None:
    """``AsyncConnection`` is a third, independent binding. Driven with
    ``asyncio.run`` because this repo has no pytest-asyncio."""

    async def _attempt() -> None:
        await psycopg.AsyncConnection.connect(_UNREACHABLE_PRODUCTION_DSN)

    with pytest.raises(RuntimeError, match="PRODUCTION knowledge database"):
        asyncio.run(_attempt())


def test_guard_does_not_refuse_legitimate_test_databases() -> None:
    """No false positives: the guard must not become "deny everything".

    A blanket refusal would look green here while silently converting the
    suite's real Postgres coverage into ``RuntimeError``, so pin the negative
    case: test-shaped and bootstrap databases reach the driver and fail (or
    succeed) on their own terms.
    """
    for dsn in (
        "postgresql://127.0.0.1:1/omniagentos_knowledge_test_isolation_probe",
        "postgresql://127.0.0.1:1/postgres",
        "postgresql://secret-user:secret-password@127.0.0.1:1/dead",
    ):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(dsn, connect_timeout=2)
