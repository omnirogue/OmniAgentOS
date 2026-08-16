from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate
from omniagentos.skills import (
    get_skill,
    index_vault_playbook,
    list_skills,
    list_tree,
    new_version,
    search,
    sync_playbook_from_repo,
    upsert_skill,
)

# Derive the expected final schema version from the migration files on disk so
# this assertion doesn't go stale each time a new NNN_*.sql migration lands.
LATEST_VERSION = max(version for version, _ in _migration_files())


def test_migration_032_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    first_run = migrate(str(db_path))
    second_run = migrate(str(db_path))
    assert first_run == LATEST_VERSION
    assert second_run == LATEST_VERSION
    assert first_run == second_run  # Verify idempotency
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        latest_version_count = connection.execute(
            f"SELECT COUNT(*) FROM schema_migrations WHERE version = {LATEST_VERSION}"
        ).fetchone()
    assert {"skills", "skill_versions", "update_proposals", "execution_evidence"} <= tables
    assert latest_version_count == (1,)


def test_index_vault_playbook_imports_curator_and_library_metadata(
    skills_environment: tuple[Path, Path],
) -> None:
    _, vault = skills_environment
    note = vault / "playbook" / "skill-captured.md"
    note.write_text(
        """---
id: captured
type: playbook
discipline: operations
created: '2026-07-21T00:00:00Z'
source_run: run_1
confidence: high
status: active
supersedes: null
---
<!-- skill-library
category: Operations
subcategory: Releases
preferred_method: Use the release checklist
fallback_method: Roll back the release
-->
# Captured Release

## Purpose
Ship a validated release safely.
""",
        encoding="utf-8",
    )
    assert index_vault_playbook() == 1
    assert index_vault_playbook() == 1
    skill = get_skill("captured")
    assert skill["title"] == "Captured Release"
    assert skill["category"] == "Operations"
    assert skill["subcategory"] == "Releases"
    assert skill["preferred_method"] == "Use the release checklist"
    assert len(skill["versions"]) == 1
    assert "Ship a validated release safely" in skill["version"]["content_snapshot"]


def test_tree_groups_category_subcategory_and_skill(
    skills_environment: tuple[Path, Path],
) -> None:
    upsert_skill(
        {
            "slug": "operations_release_canary",
            "category": "Operations",
            "subcategory": "Releases",
            "title": "Canary Release",
            "summary": "Release through a canary",
            "preferred_method": "Use staged traffic",
        }
    )
    tree = list_tree()
    operations = next(node for node in tree if node["category"] == "Operations")
    releases = next(node for node in operations["subcategories"] if node["name"] == "Releases")
    assert releases["skills"] == [
        {
            "id": "operations_release_canary",
            "slug": "operations_release_canary",
            "title": "Canary Release",
            "status": "active",
            "preferred_method": "Use staged traffic",
        }
    ]


def test_new_version_does_not_change_current_version(
    skills_environment: tuple[Path, Path],
) -> None:
    skill_id = upsert_skill(
        {
            "slug": "versioned",
            "category": "Tests",
            "subcategory": "Versions",
            "title": "Versioned Skill",
            "content_snapshot": "version one",
        }
    )
    assert (
        new_version(
            skill_id,
            content="version two",
            change_reason="Manual edit",
            author="tester",
        )
        == 2
    )
    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert [version["version"] for version in skill["versions"]] == [2, 1]
    assert skill["versions"][0]["status"] == "superseded"


def test_like_search_returns_ranked_results(skills_environment: tuple[Path, Path]) -> None:
    assert search("RunPod")
    result = search("Qwen")[0]
    assert result["id"] == "webinars_voice_qwen9b"
    assert 0.0 <= result["score"] <= 1.0


def test_sync_playbook_from_repo_copies_and_is_idempotent(
    skills_environment: tuple[Path, Path], tmp_path: Path
) -> None:
    _, vault = skills_environment
    source = tmp_path / "repo-playbook"
    source.mkdir()
    (source / "skill-alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (source / "general.md").write_text("# General\n", encoding="utf-8")

    assert sync_playbook_from_repo(source=source) == 2
    assert (vault / "playbook" / "skill-alpha.md").read_text(encoding="utf-8") == "# Alpha\n"
    assert (vault / "playbook" / "general.md").exists()
    # Size/mtime unchanged since the copy: a second run copies nothing.
    assert sync_playbook_from_repo(source=source) == 0
    # Source resolving to the destination is a no-op.
    assert sync_playbook_from_repo(source=vault / "playbook") == 0


def test_index_vault_playbook_refreshes_content(
    skills_environment: tuple[Path, Path],
) -> None:
    _, vault = skills_environment
    before = get_skill("webinars_script")
    assert "refreshed body" not in before["version"]["content_snapshot"]
    (vault / "playbook" / "skill-webinars_script.md").write_text(
        "# Webinars Script\n\n## Purpose\nrefreshed body from the vault note\n",
        encoding="utf-8",
    )
    assert index_vault_playbook() == 1
    skill = get_skill("webinars_script")
    assert "refreshed body from the vault note" in skill["version"]["content_snapshot"]
    # Upsert matched the 032 stub by id/slug — exactly one row per slug.
    assert [row["slug"] for row in list_skills()].count("webinars_script") == 1


def test_get_skill_malformed_evidence_json_does_not_look_empty(
    skills_environment: tuple[Path, Path],
) -> None:
    """Unparseable evidence_json must not render as genuine empty evidence {}.

    Counterfeit: catch JSONDecodeError and return default {} — missing and
    corrupted evidence become indistinguishable favourable empties.
    """
    skill_id = upsert_skill(
        {
            "slug": "evidence_corrupt",
            "category": "Tests",
            "subcategory": "Evidence",
            "title": "Corrupt Evidence",
            "content_snapshot": "body",
        }
    )
    db_path = skills_environment[0]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE skill_versions SET evidence_json = ? WHERE skill_id = ? AND version = 1",
            ("{malformed", skill_id),
        )
        connection.commit()
    with pytest.raises((ValueError, json.JSONDecodeError)):
        get_skill(skill_id)


def test_sync_missing_source_differs_from_empty_source(
    skills_environment: tuple[Path, Path], tmp_path: Path
) -> None:
    """Missing playbook source must not report the same success count as empty."""
    empty = tmp_path / "empty-playbook"
    empty.mkdir()
    assert sync_playbook_from_repo(source=empty) == 0
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        sync_playbook_from_repo(source=missing)


def test_index_missing_playbook_differs_from_empty_playbook(
    skills_environment: tuple[Path, Path],
) -> None:
    """Missing runtime playbook dir must not report the same success count as empty."""
    _, vault = skills_environment
    playbook = vault / "playbook"
    # Empty dir (no skill-*.md): genuine zero import count.
    for path in playbook.glob("skill-*.md"):
        path.unlink()
    assert playbook.is_dir()
    assert index_vault_playbook() == 0
    # Remove the directory entirely: must not look like a successful empty index.
    playbook.rmdir()
    assert not playbook.exists()
    with pytest.raises(FileNotFoundError):
        index_vault_playbook()
