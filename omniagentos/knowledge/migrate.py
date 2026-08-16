"""PostgreSQL migration runner for the knowledge subsystem."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import psycopg

from omniagentos.knowledge.config import migrate_dsn
from omniagentos.knowledge.contracts import ENV_MIGRATE_DSN

_MIGRATION_NAME = re.compile(r"^(\d{3})_.*\.sql$")


def _migration_files() -> list[tuple[int, Path]]:
    """Find all numbered migration files."""
    migrations_dir = Path(__file__).parent / "migrations"
    found: list[tuple[int, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is not None:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    versions = [version for version, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version")
    return found


def _create_migrations_table(conn: psycopg.Connection) -> None:
    """Create the schema_migrations table if it doesn't exist."""
    with conn.cursor() as cur:
        # Ensure the roles exist BEFORE granting to them — on a fresh cluster / CI box /
        # restored DB, 001 is the only creator of these roles, but this bootstrap runs
        # first, so a bare GRANT would fail with "role does not exist" (council portability
        # finding). Idempotent (no-op if 001 already created them).
        # Roles are CLUSTER-global while every other object here is per-database:
        # concurrent first-bootstraps (xdist workers migrating their own per-worker
        # test DBs) can race the pg_authid insert, which surfaces as
        # unique_violation rather than duplicate_object — treat both as "exists".
        cur.execute(
            "DO $$ BEGIN CREATE ROLE knowledge_agent LOGIN; "
            "EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $$"
        )
        cur.execute(
            "DO $$ BEGIN CREATE ROLE knowledge_admin LOGIN; "
            "EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $$"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version int PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        # Grant SELECT to knowledge_admin only (not agent)
        cur.execute("GRANT SELECT ON schema_migrations TO knowledge_admin")
    conn.commit()


def _applied_versions(conn: psycopg.Connection) -> set[int]:
    """Get the set of already-applied migration versions."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            return {int(row[0]) for row in cur.fetchall()}
    except psycopg.ProgrammingError:
        # Table doesn't exist yet
        return set()


def _contains_create_table(sql: str) -> bool:
    """Check if SQL contains CREATE TABLE."""
    # Simple regex check
    return bool(re.search(r"\bCREATE\s+TABLE\b", sql, re.IGNORECASE))


def _contains_grant(sql: str) -> bool:
    """Check if SQL contains GRANT statement."""
    return bool(re.search(r"\bGRANT\b", sql, re.IGNORECASE))


def migrate_connection(conn: psycopg.Connection) -> int:
    """Apply pending migrations on an existing connection."""
    # Create the migrations table first
    _create_migrations_table(conn)

    applied = _applied_versions(conn)
    final_version = max(applied, default=0)

    for version, path in _migration_files():
        if version in applied:
            continue

        script = path.read_text(encoding="utf-8")

        # Guard: refuse to apply migrations with CREATE TABLE but no GRANT
        # (except 001 which is exempt because it already has grants)
        if version > 1 and _contains_create_table(script) and not _contains_grant(script):
            raise RuntimeError(
                f"Migration {version} contains CREATE TABLE without GRANT stanza; "
                "all table creations must include explicit grant statements"
            )

        try:
            with conn.cursor() as cur:
                # Execute the migration script
                cur.execute(script)
                # Record the migration
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            conn.commit()
            applied.add(version)
            final_version = version
        except Exception:
            conn.rollback()
            raise

    return final_version


def migrate(dsn: str) -> int:
    """Connect and apply all pending migrations."""
    with psycopg.connect(dsn) as conn:
        return migrate_connection(conn)


def _resolve_cli_dsn(explicit_dsn: str) -> str:
    """Resolve the DSN the CLI will run DDL against, or refuse loudly.

    FAIL CLOSED — no production fallback. This used to fall back to
    ``postgresql://localhost/omniagentos_knowledge`` (no role, no scheme
    guard) whenever neither ``--dsn`` nor ``OMNIAGENTOS_KNOWLEDGE_MIGRATE_DSN``
    was set. That name IS the operator's live knowledge Postgres —
    confirmed reachable, as the database owner, via a bare local connection
    (see tests/test_knowledge_migrate_dsn_guard.py) — so any bare invocation
    of ``python -m omniagentos.knowledge.migrate`` (a fresh shell, a CI
    runner missing secrets, a test that shells out) would run schema DDL
    against production with nothing standing in the way. An unset or
    ambiguous target must be a loud, non-zero-exit error, never a silent
    default.
    """
    dsn = explicit_dsn or migrate_dsn()
    if not dsn:
        raise SystemExit(
            "refusing to run knowledge migrations: no DSN was given via --dsn "
            f"and {ENV_MIGRATE_DSN} is unset. DDL never defaults to a "
            "database — pass --dsn explicitly or set the migrate DSN env var."
        )
    return dsn


def _main() -> None:
    parser = argparse.ArgumentParser(description="Apply OmniAgentOS knowledge migrations")
    parser.add_argument(
        "--dsn", default="", help=f"migration DSN (required; or set {ENV_MIGRATE_DSN})"
    )
    args = parser.parse_args()

    dsn = _resolve_cli_dsn(args.dsn)

    version = migrate(dsn)
    print(version)


if __name__ == "__main__":
    _main()
