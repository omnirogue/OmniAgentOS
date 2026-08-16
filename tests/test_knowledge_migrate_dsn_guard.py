"""Regression probe for the knowledge-migration CLI's production DDL fallback.

``omniagentos/knowledge/migrate.py`` runs schema DDL — CREATE TABLE, GRANT,
ALTER — as whatever role the resolved DSN names. Its CLI entry point
(``python -m omniagentos.knowledge.migrate``) used to fall back to the bare
literal ``postgresql://localhost/omniagentos_knowledge`` whenever neither
``--dsn`` nor ``OMNIAGENTOS_KNOWLEDGE_MIGRATE_DSN`` was set.

That name IS the operator's live knowledge Postgres
(``contracts.DEFAULT_DSN``'s database). Measured on the operator host before
this fix, with no writes performed (a bare ``SELECT count(*)``): the exact
fallback DSN connected — under local trust auth, with NO role specified, so
as the OS user, i.e. the database OWNER, a strictly more dangerous identity
than the ``knowledge_agent`` role the sibling ``tests/conftest.py`` guard
(``_refuse_production_knowledge_db``) was written against — and read 8,987
real episodes from the live corpus. That guard is IN-PROCESS ONLY (it patches
``psycopg.connect`` inside the current interpreter) and does not cross a
subprocess boundary, so it does not cover this CLI at all: any test, script,
or operator shell that spawned ``python -m omniagentos.knowledge.migrate``
bare would have run real DDL against production with nothing standing in the
way.

Every assertion below goes RED if ``_resolve_cli_dsn`` regains that fallback
(reintroduce ``dsn = dsn or "postgresql://localhost/omniagentos_knowledge"``
and rerun — the "no DSN resolves to the CLI raising" tests fail, and the
migrate() call in the closed-over fake would fire on a production-shaped
name).  No test here opens a real database connection: ``migrate()`` itself
is monkeypatched out, so even a reintroduced fallback cannot make this
specific test file touch a live database — the point is to pin the
*resolution*, not to re-run the exploit every CI invocation.
"""

from __future__ import annotations

import sys

import pytest

from omniagentos.knowledge import config as knowledge_config
from omniagentos.knowledge import migrate as migrate_module
from omniagentos.knowledge.contracts import DEFAULT_DSN, ENV_MIGRATE_DSN

#: The exact literal this module's CLI used to fall back to.
_OLD_FALLBACK_DSN = "postgresql://localhost/omniagentos_knowledge"


def _dbname(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(dsn).get("dbname") or "")


def test_old_fallback_dsn_named_the_production_database() -> None:
    """Pins the exposure for the record: the removed fallback named production.

    Parsing only — no connection attempt — so this assertion cannot itself
    reach a live database however the guard regresses.
    """
    assert _dbname(_OLD_FALLBACK_DSN) == _dbname(DEFAULT_DSN) == "omniagentos_knowledge"


@pytest.fixture(autouse=True)
def _clear_migrate_dsn_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee ``migrate_dsn()`` resolves empty regardless of the host env."""
    monkeypatch.delenv(ENV_MIGRATE_DSN, raising=False)
    monkeypatch.setattr(knowledge_config, "_SECRETS", {}, raising=False)


def test__resolve_cli_dsn_refuses_when_nothing_is_configured() -> None:
    """No ``--dsn``, no env var: a loud, non-zero-exit error — never a fallback."""
    with pytest.raises(SystemExit) as excinfo:
        migrate_module._resolve_cli_dsn("")
    message = str(excinfo.value)
    assert "refusing" in message
    assert ENV_MIGRATE_DSN in message
    assert "omniagentos_knowledge" not in message  # never even names the DB it refused


def test__resolve_cli_dsn_prefers_the_explicit_flag() -> None:
    resolved = migrate_module._resolve_cli_dsn("postgresql://localhost/some_owned_db")
    assert resolved == "postgresql://localhost/some_owned_db"


def test__resolve_cli_dsn_falls_back_to_env_var_not_a_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_MIGRATE_DSN, "postgresql://localhost/an_explicit_migrate_target")
    resolved = migrate_module._resolve_cli_dsn("")
    assert resolved == "postgresql://localhost/an_explicit_migrate_target"


def test_main_never_calls_migrate_when_dsn_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through ``_main()``: the CLI must exit before touching the DB.

    ``migrate()`` is replaced with a sentinel that fails the test if it is
    ever invoked, so a regression that resurrects the fallback is caught even
    if some future refactor stops routing through ``_resolve_cli_dsn``.
    """

    def _must_not_be_called(dsn: str) -> int:
        raise AssertionError(f"migrate() must not run without an explicit target, got dsn={dsn!r}")

    monkeypatch.setattr(migrate_module, "migrate", _must_not_be_called)
    monkeypatch.setattr(sys, "argv", ["migrate.py"])

    with pytest.raises(SystemExit):
        migrate_module._main()


def test_main_runs_migrate_with_the_explicit_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive case: an explicit ``--dsn`` reaches ``migrate()`` unchanged."""
    seen: list[str] = []

    def _record(dsn: str) -> int:
        seen.append(dsn)
        return 3

    monkeypatch.setattr(migrate_module, "migrate", _record)
    monkeypatch.setattr(
        sys, "argv", ["migrate.py", "--dsn", "postgresql://localhost/an_owned_test_db"]
    )

    migrate_module._main()

    assert seen == ["postgresql://localhost/an_owned_test_db"]
