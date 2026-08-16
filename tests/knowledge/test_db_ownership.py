"""H-31 — unique per-session/worker test DB ownership and force-drop guards."""

from __future__ import annotations

import os

import pytest

from tests.knowledge.db_ownership import (
    TEST_ONLY_DB_PREFIX,
    assert_safe_force_drop,
    make_owned_test_db_name,
    parse_dbname_from_dsn,
)


def test_owned_name_uses_strict_test_only_prefix() -> None:
    name, token = make_owned_test_db_name(ownership_token="abc123def456")
    assert name.startswith(TEST_ONLY_DB_PREFIX)
    assert token == "abc123def456"
    assert token in name


def test_owned_names_differ_across_workers_and_tokens() -> None:
    a, ta = make_owned_test_db_name(
        ownership_token="aaaaaaaaaaaa", worker="gw0", cwd="l19-release-convergence"
    )
    b, tb = make_owned_test_db_name(
        ownership_token="bbbbbbbbbbbb", worker="gw1", cwd="l19-release-convergence"
    )
    c, tc = make_owned_test_db_name(
        ownership_token="cccccccccccc", worker="gw0", cwd="other-worktree"
    )
    assert a != b != c
    assert len({a, b, c}) == 3
    assert ta != tb != tc
    for name in (a, b, c):
        assert name.startswith(TEST_ONLY_DB_PREFIX)
        assert len(name) <= 63


def test_force_drop_requires_test_prefix() -> None:
    owned, token = make_owned_test_db_name(ownership_token="tokentoken01")
    # Names that are not on the deny-list, so the *prefix* rule is what rejects them.
    for outsider in ("some_app_production", "omniagentos_knowledge_staging"):
        with pytest.raises(RuntimeError, match="test-only prefix"):
            assert_safe_force_drop(
                outsider,
                ownership_token=token,
                owned_dbname=owned,
            )


def test_force_drop_requires_ownership_token_and_exact_owned_name() -> None:
    owned, token = make_owned_test_db_name(
        ownership_token="ownertoken001", worker="main", cwd="lane"
    )
    sibling, _ = make_owned_test_db_name(ownership_token="otherowner002", worker="main", cwd="lane")
    # Sibling shares the test prefix but not our ownership token / owned name.
    assert sibling.startswith(TEST_ONLY_DB_PREFIX)
    with pytest.raises(RuntimeError, match="not this session's owned DB"):
        assert_safe_force_drop(
            sibling,
            ownership_token=token,
            owned_dbname=owned,
        )
    with pytest.raises(RuntimeError, match="ownership token"):
        assert_safe_force_drop(
            owned,
            ownership_token="wrongtoken000",
            owned_dbname=owned,
        )
    # Exact owned name + matching token is allowed.
    assert_safe_force_drop(owned, ownership_token=token, owned_dbname=owned)


def test_force_drop_rejects_unsafe_identifiers() -> None:
    owned, token = make_owned_test_db_name(ownership_token="safetoken0001")
    with pytest.raises(RuntimeError, match="safe SQL identifier"):
        assert_safe_force_drop(
            f"{TEST_ONLY_DB_PREFIX}evil;drop",
            ownership_token=token,
            owned_dbname=owned,
        )
    with pytest.raises(RuntimeError, match="empty"):
        assert_safe_force_drop("", ownership_token=token, owned_dbname=owned)


def test_parse_dbname_from_dsn_url_and_keyword_forms() -> None:
    assert (
        parse_dbname_from_dsn("postgresql://localhost/omniagentos_knowledge_test_x")
        == "omniagentos_knowledge_test_x"
    )
    assert (
        parse_dbname_from_dsn(
            "postgresql://knowledge_agent@localhost:5432/omniagentos_knowledge_test_y?sslmode=disable"
        )
        == "omniagentos_knowledge_test_y"
    )
    assert (
        parse_dbname_from_dsn("host=localhost dbname=omniagentos_knowledge_test_z user=u")
        == "omniagentos_knowledge_test_z"
    )


def test_session_claimed_owned_db_is_test_only() -> None:
    """The live process-wide claim must be a uniquely owned test-only DB."""
    from tests.knowledge import db_ownership as own

    assert own.OWNED_TEST_DB.startswith(TEST_ONLY_DB_PREFIX)
    assert own.OWNERSHIP_TOKEN
    # No carve-out for an explicit DSN: the token is always a freshly generated
    # secret embedded in the name, never the name itself.
    assert own.OWNERSHIP_TOKEN != own.OWNED_TEST_DB
    assert own.OWNERSHIP_TOKEN in own.OWNED_TEST_DB
    assert_safe_force_drop(
        own.OWNED_TEST_DB,
        ownership_token=own.OWNERSHIP_TOKEN,
        owned_dbname=own.OWNED_TEST_DB,
    )
    own.assert_owned(own.OWNED_TEST_DB)


# ---------------------------------------------------------------------------
# Force-drop ordering tests: validate the correct sequence of checks.
# ---------------------------------------------------------------------------


def test_force_drop_checks_empty_before_prefix() -> None:
    """Force-drop must check empty name before checking prefix."""
    owned, token = make_owned_test_db_name(ownership_token="ordertoken01")
    # Empty name should fail with "empty" error, not "prefix" error
    with pytest.raises(RuntimeError, match="empty"):
        assert_safe_force_drop("", ownership_token=token, owned_dbname=owned)


def test_force_drop_checks_safe_identifier_before_exact_match() -> None:
    """Force-drop must reject unsafe identifiers even if they have correct prefix."""
    owned, token = make_owned_test_db_name(ownership_token="ordertoken02")
    # Name with injection attempt should fail with "safe SQL identifier" error
    unsafe_name = f"{TEST_ONLY_DB_PREFIX}valid_start; DROP TABLE users;"
    with pytest.raises(RuntimeError, match="safe SQL identifier"):
        assert_safe_force_drop(unsafe_name, ownership_token=token, owned_dbname=owned)


def test_force_drop_checks_prefix_before_ownership() -> None:
    """Force-drop must check test-only prefix before ownership token."""
    owned, token = make_owned_test_db_name(ownership_token="ordertoken03")
    # A production-like name should fail with "test-only prefix" error,
    # not "ownership token" error
    with pytest.raises(RuntimeError, match="test-only prefix"):
        assert_safe_force_drop(
            "omniagentos_reporting",  # Missing test prefix
            ownership_token=token,
            owned_dbname=owned,
        )


def test_force_drop_checks_exact_match_before_token_presence() -> None:
    """Force-drop must check exact owned name match before checking token in name."""
    owned, token = make_owned_test_db_name(ownership_token="matchtoken04")
    # A different test DB (with correct prefix) should fail with "not this session's owned DB"
    other_db = f"{TEST_ONLY_DB_PREFIX}other_db_{token}"  # Has token but wrong owned_dbname
    with pytest.raises(RuntimeError, match="not this session's owned DB"):
        assert_safe_force_drop(other_db, ownership_token=token, owned_dbname=owned)


# ---------------------------------------------------------------------------
# Query-DSN parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_dbname_from_dsn_empty_cases() -> None:
    """parse_dbname_from_dsn handles empty/None-ish inputs."""
    assert parse_dbname_from_dsn("") == ""
    assert parse_dbname_from_dsn("   ") == ""


def test_parse_dbname_from_dsn_url_variations() -> None:
    """parse_dbname_from_dsn handles URL format variations."""
    # Basic URL
    assert parse_dbname_from_dsn("postgresql://localhost/mydb") == "mydb"
    # URL with port
    assert parse_dbname_from_dsn("postgresql://localhost:5432/testdb") == "testdb"
    # URL with user
    assert parse_dbname_from_dsn("postgresql://user@localhost/userdb") == "userdb"
    # URL with user and password
    assert parse_dbname_from_dsn("postgresql://user:pass@localhost/authdb") == "authdb"
    # URL with query params
    assert parse_dbname_from_dsn("postgresql://localhost/paramdb?sslmode=require") == "paramdb"
    # Percent-encoded password and a non-default port are not confused for the name
    assert (
        parse_dbname_from_dsn("postgresql://user:p%40ss@host:5433/fulldb?sslmode=require")
        == "fulldb"
    )


def test_parse_dbname_from_dsn_keyword_variations() -> None:
    """parse_dbname_from_dsn reports what libpq resolves, not what a scan guesses."""
    # Standard keyword form
    assert parse_dbname_from_dsn("host=localhost dbname=kwdb user=test") == "kwdb"
    # dbname at start
    assert parse_dbname_from_dsn("dbname=startdb host=localhost") == "startdb"
    # dbname at end
    assert parse_dbname_from_dsn("host=localhost user=test dbname=enddb") == "enddb"
    # Single quotes are libpq's value quoting and are stripped...
    assert parse_dbname_from_dsn("host=localhost dbname='quoteddb' user=test") == "quoteddb"
    # ...but double quotes are literal characters, so the name genuinely
    # contains them. Reporting "dquoteddb" would name a database libpq never
    # connects to, and the ownership guard would then check the wrong one.
    assert parse_dbname_from_dsn('host=localhost dbname="dquoteddb"') == '"dquoteddb"'


def test_parse_dbname_from_dsn_does_not_invent_a_name_libpq_would_not_use() -> None:
    """Forms a regex scan mis-reads must yield nothing, or raise — never a guess.

    Each input below was previously reported as a concrete database name by a
    hand-rolled scan while libpq resolves something else entirely. Since this
    name selects the force-drop target, a confident wrong answer is the
    dangerous outcome; an empty result or a refusal is the safe one.
    """
    # ';' is not a libpq separator: the whole tail is part of the *host* value,
    # so no database is named at all (a scan reported "semidb").
    assert parse_dbname_from_dsn("host=localhost;dbname=semidb;user=test") == ""
    # Keywords are case-sensitive; libpq rejects these outright (a scan reported
    # "upperdb"/"mixeddb").
    for bad in ("host=localhost DBNAME=upperdb", "host=localhost DbName=mixeddb"):
        with pytest.raises(RuntimeError, match="refusing to interpret"):
            parse_dbname_from_dsn(bad)
    # An unknown URI query parameter is an error, not something to read past.
    with pytest.raises(RuntimeError, match="refusing to interpret"):
        parse_dbname_from_dsn("postgresql://user@host:5433/fulldb?pool=5")


def test_force_drop_rejects_forbidden_databases() -> None:
    """Every name on the deny-list is rejected *as protected*, not incidentally.

    Matching only ``protected database`` is the point: these names also lack the
    test prefix, so a deny-list evaluated after the prefix rule would never run
    and the check would be dead code that a later reordering could not resurrect.
    """
    from tests.knowledge.db_ownership import FORBIDDEN_DB_NAMES

    owned, token = make_owned_test_db_name(ownership_token="forbidtoken")
    assert FORBIDDEN_DB_NAMES, "deny-list must not be empty"
    for forbidden in sorted(FORBIDDEN_DB_NAMES):
        with pytest.raises(RuntimeError, match="protected database"):
            assert_safe_force_drop(forbidden, ownership_token=token, owned_dbname=owned)


# ---------------------------------------------------------------------------
# An explicit operator DSN selects the server, never the database name. Two
# workers handed the identical DSN must end up owning different databases.
# ---------------------------------------------------------------------------


def _claim_with_dsn(monkeypatch: pytest.MonkeyPatch, dsn: str, worker: str) -> tuple[str, str, str]:
    """Simulate one xdist worker claiming a DB from *dsn*. Returns (db, token, dsn)."""
    from tests.knowledge import db_ownership as own

    monkeypatch.setenv(own.TEST_DSN_ENV, dsn)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    dbname, token = own.claim_owned_test_db()
    return dbname, token, os.environ[own.TEST_DSN_ENV]


def test_two_workers_sharing_one_dsn_cannot_force_drop_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression for the shared-DSN ownership hole.

    Previously an explicit DSN returned ``(dbname, dbname)`` — the database name
    was its own ownership token — so both workers passed the exact-match and
    token checks against the *same* database and could drop it underneath each
    other mid-run. Each worker must now own a distinct database.
    """
    from tests.knowledge import db_ownership as own

    operator_dsn = f"postgresql://localhost/{TEST_ONLY_DB_PREFIX}shared"

    db_a, token_a, _ = _claim_with_dsn(monkeypatch, operator_dsn, "gw0")
    db_b, token_b, _ = _claim_with_dsn(monkeypatch, operator_dsn, "gw1")

    assert db_a != db_b, "both workers claimed the same database"
    assert token_a != token_b
    assert token_a not in db_b and token_b not in db_a

    # Neither worker's guard will authorise touching the other's database...
    with pytest.raises(RuntimeError, match="not this session's owned DB"):
        assert_safe_force_drop(db_b, ownership_token=token_a, owned_dbname=db_a)
    with pytest.raises(RuntimeError, match="not this session's owned DB"):
        assert_safe_force_drop(db_a, ownership_token=token_b, owned_dbname=db_b)

    # ...and the operator's own name is owned by nobody, so it is never dropped.
    shared = own.parse_dbname_from_dsn(operator_dsn)
    for dbname, token in ((db_a, token_a), (db_b, token_b)):
        with pytest.raises(RuntimeError, match="not this session's owned DB"):
            assert_safe_force_drop(shared, ownership_token=token, owned_dbname=dbname)

    # Each worker may still drop exactly its own.
    assert_safe_force_drop(db_a, ownership_token=token_a, owned_dbname=db_a)
    assert_safe_force_drop(db_b, ownership_token=token_b, owned_dbname=db_b)


def test_two_workers_sharing_a_shadowed_dsn_still_get_separate_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end form of the shadowing regression, through the real claim path.

    The unit-level normaliser test proves the rewrite; this proves the property
    operators actually depend on — that handing two workers one DSN, however it
    spells the database, lands them on two databases and never on the shared one.
    """
    from psycopg.conninfo import conninfo_to_dict

    shadowed = f"host=localhost dbname={TEST_ONLY_DB_PREFIX}a dbname={TEST_ONLY_DB_PREFIX}shared"
    db_a, _, dsn_a = _claim_with_dsn(monkeypatch, shadowed, "gw0")
    db_b, _, dsn_b = _claim_with_dsn(monkeypatch, shadowed, "gw1")

    assert db_a != db_b
    effective = {conninfo_to_dict(dsn_a).get("dbname"), conninfo_to_dict(dsn_b).get("dbname")}
    assert effective == {db_a, db_b}, f"workers connect to {effective}, not their owned databases"
    assert f"{TEST_ONLY_DB_PREFIX}shared" not in effective


def test_explicit_dsn_keeps_server_and_credentials_but_not_the_db_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substituting the owned name must not discard the operator's connection info."""
    from psycopg.conninfo import conninfo_to_dict

    from tests.knowledge import db_ownership as own

    dsn = f"postgresql://kn_user:sekret@db.internal:5433/{TEST_ONLY_DB_PREFIX}base?sslmode=require"
    dbname, _, published = _claim_with_dsn(monkeypatch, dsn, "gw0")

    assert own.parse_dbname_from_dsn(published) == dbname
    assert dbname != f"{TEST_ONLY_DB_PREFIX}base"
    # Compared as parameters, not as a string: normalising collapses a URI to
    # libpq's keyword spelling, and asserting the spelling would pin an
    # irrelevant detail while proving nothing about what survived.
    params = conninfo_to_dict(published)
    assert params["user"] == "kn_user"
    assert params["password"] == "sekret"
    assert params["host"] == "db.internal"
    assert params["port"] == "5433"
    assert params["sslmode"] == "require"


def test_explicit_dsn_must_still_be_test_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A production-looking DSN is refused outright, not silently redirected.

    The supplied name is the only evidence that the *server* is disposable, and
    the suite issues CREATE/DROP DATABASE against it.
    """
    from tests.knowledge import db_ownership as own

    monkeypatch.setenv(own.TEST_DSN_ENV, "postgresql://prod.internal/omniagentos_knowledge")
    with pytest.raises(RuntimeError, match="test-only prefix"):
        own.claim_owned_test_db()


def test_unrewritable_dsn_is_refused_rather_than_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN whose database component cannot be located fails closed.

    A bare name is not a DSN. The former parser fell back to treating it as one,
    so a string libpq cannot connect with was accepted as test-only and reused
    as its own target; libpq refuses it, and so must the claim.
    """
    from tests.knowledge import db_ownership as own

    monkeypatch.setenv(own.TEST_DSN_ENV, f"{TEST_ONLY_DB_PREFIX}bare")
    with pytest.raises(RuntimeError, match="refusing to interpret"):
        own.claim_owned_test_db()


@pytest.mark.parametrize(
    "shadowed",
    [
        pytest.param(
            f"host=localhost dbname={TEST_ONLY_DB_PREFIX}a dbname={TEST_ONLY_DB_PREFIX}shared",
            id="repeated-keyword",
        ),
        pytest.param(
            f"postgresql://localhost/{TEST_ONLY_DB_PREFIX}a?dbname={TEST_ONLY_DB_PREFIX}shared",
            id="uri-query-overrides-path",
        ),
    ],
)
def test_a_dsn_that_names_two_databases_cannot_leave_the_shared_one_effective(
    shadowed: str,
) -> None:
    """The regression for textual DSN rewriting.

    libpq resolves a repeated ``dbname=`` keyword, and a ``?dbname=`` URI query
    parameter overriding the URI path, with **last-one-wins** precedence.
    Substituting only the first occurrence (or only the URI path) therefore left
    ``..._shared`` effective: every worker connected to the same database while
    :func:`claim_owned_test_db` reported distinct owned names — exactly the
    ownership hole the guard exists to close, reintroduced one layer down.

    Asserted through libpq's parser rather than by string shape, so no rewriting
    strategy can satisfy this test without actually changing where the DSN
    points.
    """
    from psycopg.conninfo import conninfo_to_dict

    from tests.knowledge.db_ownership import normalize_dsn_with_dbname

    owned = f"{TEST_ONLY_DB_PREFIX}owned_by_me"
    rebuilt = normalize_dsn_with_dbname(shadowed, owned)

    effective = conninfo_to_dict(rebuilt).get("dbname")
    assert effective == owned, f"DSN still selects {effective!r}, not the owned database"
    assert f"{TEST_ONLY_DB_PREFIX}shared" not in rebuilt


def test_normalize_preserves_server_credentials_and_options() -> None:
    """Collapsing the DSN must not quietly drop the operator's connection info."""
    from psycopg.conninfo import conninfo_to_dict

    from tests.knowledge.db_ownership import normalize_dsn_with_dbname

    rebuilt = normalize_dsn_with_dbname(
        f"postgresql://alice:p%40ss@db.example:5433/{TEST_ONLY_DB_PREFIX}seed"
        "?sslmode=require&connect_timeout=7",
        f"{TEST_ONLY_DB_PREFIX}owned",
    )
    params = conninfo_to_dict(rebuilt)
    assert params["user"] == "alice"
    assert params["password"] == "p@ss"
    assert params["host"] == "db.example"
    assert params["port"] == "5433"
    assert params["sslmode"] == "require"
    assert params["connect_timeout"] == "7"
    assert params["dbname"] == f"{TEST_ONLY_DB_PREFIX}owned"


# ---------------------------------------------------------------------------
# Every destructive SQL helper routes through the guard. These use a recording
# fake cursor — no PostgreSQL connection, no live database writes.
# ---------------------------------------------------------------------------


class _RecordingCursor:
    """Records executed SQL so tests can prove what did (not) reach the server."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append(query)


def test_helpers_emit_no_sql_when_ownership_check_fails() -> None:
    """A rejected name must abort before *any* statement reaches the server."""
    from tests.knowledge import db_ownership as own

    for helper in (
        own.terminate_owned_backends,
        own.force_drop_owned_test_db,
        own.recreate_owned_test_db,
    ):
        for bad in ("postgres", "omniagentos_knowledge", "", f"{TEST_ONLY_DB_PREFIX}not_ours"):
            cur = _RecordingCursor()
            with pytest.raises(RuntimeError):
                helper(cur, bad)
            assert cur.executed == [], f"{helper.__name__} emitted SQL for {bad!r}"


def test_force_drop_owned_terminates_then_drops() -> None:
    """The owned path terminates stray backends before the FORCE drop."""
    from tests.knowledge import db_ownership as own

    cur = _RecordingCursor()
    own.force_drop_owned_test_db(cur, own.OWNED_TEST_DB)
    assert len(cur.executed) == 2
    assert "pg_terminate_backend" in cur.executed[0]
    assert cur.executed[1] == f'DROP DATABASE IF EXISTS "{own.OWNED_TEST_DB}" WITH (FORCE)'


def test_recreate_owned_drops_then_creates() -> None:
    """Recreate is drop-then-create, both scoped to the owned name."""
    from tests.knowledge import db_ownership as own

    cur = _RecordingCursor()
    own.recreate_owned_test_db(cur, own.OWNED_TEST_DB)
    assert len(cur.executed) == 3
    assert "pg_terminate_backend" in cur.executed[0]
    assert cur.executed[1].startswith("DROP DATABASE IF EXISTS")
    assert cur.executed[2] == f'CREATE DATABASE "{own.OWNED_TEST_DB}"'


def test_no_test_module_issues_unguarded_destructive_sql() -> None:
    """No test module may hand-roll DROP DATABASE / pg_terminate_backend.

    Destructive SQL belongs to ``tests.knowledge.db_ownership`` alone; a
    module-local fixture that shadows the parent conftest (as the perf module
    does) must not also shadow the H-31 ownership guard.
    """
    import re
    from pathlib import Path

    destructive = re.compile(r"DROP\s+DATABASE|pg_terminate_backend", re.IGNORECASE)
    here = Path(__file__).resolve()
    # The guard module owns the SQL; this module only names it in assertions.
    exempt = {here, here.parent / "db_ownership.py"}
    tests_root = here.parent.parent

    offenders = [
        str(path.relative_to(tests_root))
        for path in sorted(tests_root.rglob("*.py"))
        if path.resolve() not in exempt and destructive.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"unguarded destructive SQL in: {offenders}"
