"""Regression test: quarantined skills survive re-index operations.

Verifies that when a skill is archived (quarantined) and then re-indexed
(e.g., via index_vault_playbook or direct upsert_skill with default metadata),
the non-active status is preserved rather than being silently reverted to 'active'.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.skills import get_skill, upsert_skill


def test_quarantine_preserved_across_upsert_with_default_status(
    skills_environment: tuple[Path, Path],
) -> None:
    """Verify that archived status survives upsert with default active status.

    This is the core regression test for the quarantine persistence bug:
    - Create a skill with status='active'
    - Manually change it to status='archived' (quarantine)
    - Call upsert_skill with default status='active' (as _metadata_for_note does)
    - Verify the skill is still archived (not reverted to active)
    """
    # Create a new skill with active status
    skill_id = upsert_skill(
        {
            "slug": "test_quarantine",
            "category": "Testing",
            "subcategory": "Persistence",
            "title": "Quarantine Persistence Test",
            "summary": "Verify quarantine survives re-index",
        }
    )
    skill = get_skill(skill_id)
    assert skill["status"] == "active"

    # Simulate quarantine by directly changing status to archived
    from omniagentos.contracts import default_db_path
    from omniagentos.db.store import _connect

    connection = _connect(str(default_db_path()))
    try:
        connection.execute(
            "UPDATE skills SET status = 'archived' WHERE id = ?",
            (skill_id,),
        )
        connection.commit()
    finally:
        connection.close()

    # Verify quarantine was applied
    skill = get_skill(skill_id)
    assert skill["status"] == "archived", "Quarantine should be applied before re-index"

    # Simulate re-index by calling upsert_skill with default metadata
    # (this is what index_vault_playbook does via _metadata_for_note)
    upsert_skill(
        {
            "id": skill_id,
            "slug": "test_quarantine",
            "category": "Testing",
            "subcategory": "Persistence",
            "title": "Quarantine Persistence Test",
            "summary": "Verify quarantine survives re-index",
            "status": "active",  # This is the default from _metadata_for_note
        }
    )

    # Verify quarantine is still in place
    skill = get_skill(skill_id)
    assert (
        skill["status"] == "archived"
    ), "Quarantine should survive re-index (not be reverted to active)"


def test_explicit_status_change_is_honored(
    skills_environment: tuple[Path, Path],
) -> None:
    """Verify that explicit status changes are still honored.

    When upsert_skill is called with an explicit status='archived' (not the default),
    it should update the skill's status even if it's already active.
    """
    # Create a skill with active status
    skill_id = upsert_skill(
        {
            "slug": "test_explicit",
            "category": "Testing",
            "subcategory": "StatusChanges",
            "title": "Explicit Status Change Test",
            "summary": "Verify explicit status changes work",
        }
    )
    skill = get_skill(skill_id)
    assert skill["status"] == "active"

    # Explicitly change to archived
    upsert_skill(
        {
            "id": skill_id,
            "slug": "test_explicit",
            "category": "Testing",
            "subcategory": "StatusChanges",
            "title": "Explicit Status Change Test",
            "summary": "Verify explicit status changes work",
            "status": "archived",  # Explicit change
        }
    )

    skill = get_skill(skill_id)
    assert skill["status"] == "archived", "Explicit status change should be honored"


def test_active_skill_remains_active_with_default_status(
    skills_environment: tuple[Path, Path],
) -> None:
    """Verify that active skills remain active when re-indexed with default status.

    This is the normal case: an active skill should stay active when
    upsert_skill is called with the default status='active'.
    """
    skill_id = upsert_skill(
        {
            "slug": "test_normal",
            "category": "Testing",
            "subcategory": "Normal",
            "title": "Normal Active Skill",
            "summary": "Verify normal behavior",
        }
    )
    skill = get_skill(skill_id)
    assert skill["status"] == "active"

    # Re-index with default status
    upsert_skill(
        {
            "id": skill_id,
            "slug": "test_normal",
            "category": "Testing",
            "subcategory": "Normal",
            "title": "Normal Active Skill",
            "summary": "Verify normal behavior updated",
            "status": "active",  # Default
        }
    )

    skill = get_skill(skill_id)
    assert skill["status"] == "active"
    assert skill["summary"] == "Verify normal behavior updated"  # Other fields update normally
