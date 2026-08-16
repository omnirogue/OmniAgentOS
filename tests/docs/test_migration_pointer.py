"""AGENTS.md migration sequence pointer must track the filesystem.

The next free migration number is derived from omniagentos/db/migrations/*.sql
(max integer prefix + 1, zero-padded to 3 digits). AGENTS.md must name exactly
that number so a landed migration cannot leave the house rule stale.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "omniagentos" / "db" / "migrations"
_AGENTS_MD = _REPO_ROOT / "AGENTS.md"

# Anchored to the append-only migrations sentence so other 3-digit tokens in
# AGENTS.md cannot satisfy the check.
_AGENTS_NEXT_RE = re.compile(
    r"Append-only Migrations.*?currently `(\d{3})`",
    re.DOTALL,
)
_SQL_PREFIX_RE = re.compile(r"^(\d+)_")


def _derived_next_migration_number() -> str:
    """Max numeric prefix among migrations/*.sql, plus one, zero-padded to 3 digits."""
    prefixes: list[int] = []
    for path in _MIGRATIONS_DIR.glob("*.sql"):
        match = _SQL_PREFIX_RE.match(path.name)
        if match:
            prefixes.append(int(match.group(1)))
    if not prefixes:
        raise AssertionError(f"no migration SQL files found under {_MIGRATIONS_DIR}")
    return f"{max(prefixes) + 1:03d}"


def _documented_next_migration_number() -> str:
    text = _AGENTS_MD.read_text(encoding="utf-8")
    match = _AGENTS_NEXT_RE.search(text)
    if match is None:
        raise AssertionError(
            "AGENTS.md has no anchored 'Append-only Migrations' sentence naming "
            "the next sequence as currently `NNN`"
        )
    return match.group(1)


def test_agents_md_next_migration_matches_filesystem() -> None:
    documented = _documented_next_migration_number()
    derived = _derived_next_migration_number()
    assert documented == derived, (
        f"AGENTS.md documents next migration {documented!r} but the filesystem "
        f"derives {derived!r} (max prefix in omniagentos/db/migrations/ + 1). "
        f"Update the Append-only Migrations sentence in AGENTS.md."
    )
