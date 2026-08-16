"""H-31 — the single PostgreSQL force-drop guard for the whole test suite.

Concurrent pytest sessions (and xdist workers) must never terminate each
other's backends or force-drop a shared database. Every destructive path in
the suite routes through :func:`assert_safe_force_drop`, which requires:

  1. a non-empty, safe SQL identifier
  2. a strict test-only name prefix
  3. an exact match against *this* session/worker's owned database name
  4. an ownership token that is embedded in that owned name

This module owns the process-wide claim (:data:`OWNED_TEST_DB`,
:data:`OWNERSHIP_TOKEN`) so that every importer — ``conftest.py`` and the
module-local perf fixture alike — shares one identity and one guard rather
than reimplementing the checks.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Protocol

TEST_ONLY_DB_PREFIX = "omniagentos_knowledge_test_"
_SAFE_DB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: Names that must never be dropped even if a caller forges the test prefix.
FORBIDDEN_DB_NAMES = frozenset({"postgres", "template0", "template1", "omniagentos_knowledge"})

TEST_DSN_ENV = "OMNIAGENTOS_KNOWLEDGE_TEST_DSN"


class _Cursor(Protocol):
    """The narrow slice of a DB-API cursor this module uses."""

    def execute(self, query: str, params: Any = ..., /) -> Any: ...


def _sanitize_ident(part: str, max_len: int) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", part.lower()).strip("_")
    return (cleaned or "x")[:max_len]


def new_ownership_token() -> str:
    """Return a fresh opaque ownership token (embedded in the DB name)."""
    return uuid.uuid4().hex[:12]


def make_owned_test_db_name(
    *,
    ownership_token: str | None = None,
    worker: str | None = None,
    cwd: str | None = None,
) -> tuple[str, str]:
    """Build a uniquely owned test DB name.

    Returns ``(dbname, ownership_token)``. The name always starts with
    :data:`TEST_ONLY_DB_PREFIX` and embeds the ownership token so force-drop
    can verify both prefix and owner before any destructive SQL.
    """
    token = ownership_token or new_ownership_token()
    worker_id = _sanitize_ident(
        worker if worker is not None else os.environ.get("PYTEST_XDIST_WORKER", "main"),
        8,
    )
    cwd_slug = _sanitize_ident(
        cwd if cwd is not None else os.path.basename(os.getcwd()),
        12,
    )
    # prefix(26) + cwd(<=12) + _ + worker(<=8) + _ + token(12)  <= 61
    name = f"{TEST_ONLY_DB_PREFIX}{cwd_slug}_{worker_id}_{token}"
    return name[:63], token


def _conninfo_tools() -> tuple[Any, Any]:
    """Return libpq's own ``(parse, build)`` pair, or refuse.

    Imported lazily so importing this module does not require psycopg; it is
    needed only once a real DSN has to be interpreted, which is already a
    PostgreSQL code path.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
    except ImportError as exc:  # pragma: no cover - psycopg is a test dependency
        raise RuntimeError(
            f"psycopg is required to interpret {TEST_DSN_ENV}: only libpq's own parser "
            f"can say which database a DSN actually selects"
        ) from exc
    return conninfo_to_dict, make_conninfo


def parse_dbname_from_dsn(dsn: str) -> str:
    """Return the database libpq will *actually* connect to.

    Delegated to libpq's parser rather than pattern-matched, because a
    hand-rolled scan and libpq disagree on real inputs — and this name decides
    what gets force-dropped. libpq accepts repeated keywords *and* URI query
    parameters with **last-one-wins** precedence, treats keywords as
    case-sensitive, does not split on ``;``, and keeps double quotes as literal
    characters. A scan returning the first ``dbname=`` (or the URI path) can
    therefore name a database nobody connects to, so the ownership guard would
    check one database while psycopg opened another.

    An unparseable DSN raises: guessing is what let a shared database be
    mistaken for an owned one.
    """
    raw = (dsn or "").strip()
    if not raw:
        return ""
    conninfo_to_dict, _ = _conninfo_tools()
    try:
        params = conninfo_to_dict(raw)
    except Exception as exc:
        raise RuntimeError(f"refusing to interpret {TEST_DSN_ENV} {raw!r}: {exc}") from exc
    return str(params.get("dbname") or "")


def normalize_dsn_with_dbname(dsn: str, dbname: str) -> str:
    """Rebuild *dsn* so *dbname* is the one database it can possibly select.

    Editing a DSN textually is not safe. libpq lets a later ``dbname=`` keyword
    or a ``?dbname=`` URI query parameter override an earlier one, so replacing
    the first occurrence — or only the URI path — leaves the *original* database
    effective and every worker sharing it, while :func:`claim_owned_test_db`
    reports distinct owned names. Parsing to a parameter mapping and rebuilding
    collapses every spelling to a single effective value.

    The result is re-parsed and checked, so this cannot quietly return a DSN
    that still points somewhere else.
    """
    conninfo_to_dict, make_conninfo = _conninfo_tools()
    raw = (dsn or "").strip()
    try:
        params = conninfo_to_dict(raw)
    except Exception as exc:
        raise RuntimeError(
            f"refusing to reuse {TEST_DSN_ENV}: cannot parse {raw!r}: {exc}"
        ) from exc
    params["dbname"] = dbname
    rebuilt = str(make_conninfo(**params))
    effective = parse_dbname_from_dsn(rebuilt)
    if effective != dbname:  # pragma: no cover - defensive
        raise RuntimeError(
            f"refusing to reuse {TEST_DSN_ENV}: rebuilt DSN still selects "
            f"{effective!r} rather than {dbname!r}"
        )
    return rebuilt


def assert_safe_force_drop(
    dbname: str,
    *,
    ownership_token: str,
    owned_dbname: str,
) -> None:
    """Refuse force-drop unless the name is test-only and owned by this session.

    Raises :class:`RuntimeError` on any mismatch so teardown cannot target
    production databases or another suite's resources.
    """
    if not dbname:
        raise RuntimeError("refusing force-drop: empty database name")
    # Absolute deny-list first, so no later check-ordering mistake can expose a
    # production name. Evaluated before the prefix test precisely so this branch
    # is reachable: every forbidden name would otherwise be rejected earlier for
    # lacking the test prefix, leaving the deny-list unexercised dead code.
    if dbname in FORBIDDEN_DB_NAMES:
        raise RuntimeError(f"refusing force-drop of protected database {dbname!r}")
    if not _SAFE_DB_NAME.fullmatch(dbname):
        raise RuntimeError(f"refusing force-drop of {dbname!r}: name must be a safe SQL identifier")
    if not dbname.startswith(TEST_ONLY_DB_PREFIX):
        raise RuntimeError(
            f"refusing force-drop of {dbname!r}: must start with test-only prefix "
            f"{TEST_ONLY_DB_PREFIX!r}"
        )
    if dbname != owned_dbname:
        raise RuntimeError(
            f"refusing force-drop of {dbname!r}: not this session's owned DB {owned_dbname!r}"
        )
    if not ownership_token or ownership_token not in dbname:
        raise RuntimeError(f"refusing force-drop of {dbname!r}: ownership token mismatch")


def claim_owned_test_db() -> tuple[str, str]:
    """Resolve the DB this pytest process uniquely owns and publish its DSN.

    An explicit :data:`TEST_DSN_ENV` selects the *server* and its connection
    parameters — never the database name. The name is always regenerated with a
    fresh ownership token and substituted into the supplied DSN.

    Reusing the operator's name would make that name its own ownership token, so
    concurrent sessions or xdist workers handed the same DSN would each believe
    they owned the same database and could force-drop one another mid-run. The
    supplied name is still required to be test-only: that is what proves the DSN
    points at a disposable server rather than a production cluster on which the
    suite would happily CREATE and DROP.
    """
    dbname, token = make_owned_test_db_name()

    explicit = os.environ.get(TEST_DSN_ENV)
    if explicit:
        supplied = parse_dbname_from_dsn(explicit)
        if not supplied.startswith(TEST_ONLY_DB_PREFIX):
            raise RuntimeError(
                f"{TEST_DSN_ENV} database {supplied!r} must start with "
                f"test-only prefix {TEST_ONLY_DB_PREFIX!r}"
            )
        if not _SAFE_DB_NAME.fullmatch(supplied):
            raise RuntimeError(f"{TEST_DSN_ENV} database {supplied!r} is not a safe identifier")
        os.environ[TEST_DSN_ENV] = normalize_dsn_with_dbname(explicit, dbname)
    else:
        os.environ[TEST_DSN_ENV] = f"postgresql://localhost/{dbname}"

    return dbname, token


#: This process's owned test database and the token proving that ownership.
OWNED_TEST_DB, OWNERSHIP_TOKEN = claim_owned_test_db()


def assert_owned(dbname: str) -> None:
    """Guard *dbname* against this process's claim. Raises on any mismatch."""
    assert_safe_force_drop(
        dbname,
        ownership_token=OWNERSHIP_TOKEN,
        owned_dbname=OWNED_TEST_DB,
    )


def terminate_owned_backends(cur: _Cursor, dbname: str) -> None:
    """Kill other backends on *dbname*, but only after the ownership guard passes."""
    assert_owned(dbname)
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (dbname,),
    )


def force_drop_owned_test_db(cur: _Cursor, dbname: str) -> None:
    """Terminate backends and DROP DATABASE only after ownership checks pass."""
    terminate_owned_backends(cur, dbname)
    # DROP DATABASE IF EXISTS ... WITH (FORCE) also disconnects, belt+braces.
    cur.execute(f"DROP DATABASE IF EXISTS {_quote_owned(dbname)} WITH (FORCE)")


def recreate_owned_test_db(cur: _Cursor, dbname: str) -> None:
    """Force-drop then re-create this session's owned test database."""
    force_drop_owned_test_db(cur, dbname)
    cur.execute(f"CREATE DATABASE {_quote_owned(dbname)}")


def _quote_owned(dbname: str) -> str:
    """Quote a name that has already passed :func:`assert_owned`."""
    assert_owned(dbname)
    return '"' + dbname + '"'
