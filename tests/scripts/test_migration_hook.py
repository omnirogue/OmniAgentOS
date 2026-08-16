"""
Targeted tests for check-migration-versions.sh hook.

Tests cover:
- Accept: single new migration
- Accept: multiple contiguous new migrations
- Accept: restaging an uncommitted migration under a different filename
- Refuse: edits to an applied migration
- Refuse: non-contiguous ordinals
- Refuse: duplicates
- Refuse: deletion of committed migration
- Refuse: rename of a committed migration (same version, same content)
- Refuse: renumber of a committed migration (same content, next ordinal)
"""

import os
import shutil
import subprocess
from pathlib import Path

# The hook under test, from THIS checkout — never an absolute path to some
# other clone: these tests must run on any machine the repo is checked out on.
HOOK_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "git-hooks" / "check-migration-versions.sh"
)


def run_hook(repo_root: str, base_ref: str = "HEAD") -> tuple[int, str, str]:
    """Run the migration hook and return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["MIGRATION_AUTHORITY_BASE"] = base_ref
    result = subprocess.run(
        ["bash", "scripts/git-hooks/check-migration-versions.sh"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def setup_test_repo(tmp_path, hook_path: Path = HOOK_PATH):
    """Set up a test repository with the hook script."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True)

    # Copy the hook script
    scripts_dir = repo / "scripts" / "git-hooks"
    scripts_dir.mkdir(parents=True)
    shutil.copy(hook_path, scripts_dir / "check-migration-versions.sh")
    (scripts_dir / "check-migration-versions.sh").chmod(0o755)

    # Create migration directory
    mig_dir = repo / "omniagentos" / "db" / "migrations"
    mig_dir.mkdir(parents=True)

    return repo, mig_dir


def test_accept_single_new_migration(tmp_path):
    """Accept a single new migration with expected version number."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Add new migration in staging area
    (mig_dir / "002_next.sql").write_text("SELECT 2;")
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/002_next.sql"], cwd=repo, capture_output=True
    )

    # Run hook
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code == 0, f"Expected success, got stderr: {stderr}"


def test_accept_multiple_contiguous_migrations(tmp_path):
    """Accept multiple contiguous new migrations in one operation."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Add multiple contiguous migrations
    (mig_dir / "002_first.sql").write_text("SELECT 2;")
    (mig_dir / "003_second.sql").write_text("SELECT 3;")
    (mig_dir / "004_third.sql").write_text("SELECT 4;")
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/002_first.sql"], cwd=repo, capture_output=True
    )
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/003_second.sql"], cwd=repo, capture_output=True
    )
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/004_third.sql"], cwd=repo, capture_output=True
    )

    # Run hook
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code == 0, f"Expected success, got stderr: {stderr}"


def test_accept_restaged_uncommitted_migration_under_new_filename(tmp_path):
    """Accept restaging an uncommitted migration under a different filename.

    Named for what it actually exercises. Neither filename is in the baseline
    commit, so nothing is deleted and the deletion loop never runs — to the hook
    this is indistinguishable from staging one new migration at expected_next.
    The committed-rename case, which DOES reach that loop, is pinned by
    test_refuse_rename_of_committed_migration below.
    """
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Simulate a rename: add 002_foo.sql with some content, then rename it to 002_bar.sql
    content = "ALTER TABLE users ADD COLUMN email VARCHAR(255);"
    (mig_dir / "002_foo.sql").write_text(content)
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/002_foo.sql"], cwd=repo, capture_output=True
    )

    # Remove old file and add new one with same content
    (mig_dir / "002_foo.sql").unlink()
    (mig_dir / "002_bar.sql").write_text(content)
    subprocess.run(
        ["git", "add", "-A", "omniagentos/db/migrations/"], cwd=repo, capture_output=True
    )

    # Run hook - should pass because it detects the rename (same content, same version)
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code == 0, f"Expected success for rename, got stderr: {stderr}"


def test_refuse_edit_to_applied_migration(tmp_path):
    """Refuse to edit a migration that's already committed."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create and commit an initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Edit the committed migration
    (mig_dir / "001_init.sql").write_text("SELECT 2; -- EDITED")
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/001_init.sql"], cwd=repo, capture_output=True
    )

    # Run hook - should fail
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, "Expected failure for editing applied migration"
    assert "BLOCKED: historical migration modified" in stderr


def test_refuse_non_contiguous_ordinals(tmp_path):
    """Refuse non-contiguous migration version numbers."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Add migrations with a gap (002 and 004, skipping 003)
    (mig_dir / "002_first.sql").write_text("SELECT 2;")
    (mig_dir / "004_skip.sql").write_text("SELECT 4;")
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/002_first.sql"], cwd=repo, capture_output=True
    )
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/004_skip.sql"], cwd=repo, capture_output=True
    )

    # Run hook - should fail
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, "Expected failure for non-contiguous ordinals"
    assert "non-contiguous migration version" in stderr or "not the next allocation" in stderr


def test_refuse_duplicate_versions(tmp_path):
    """Refuse duplicate migration version numbers."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Add two files with the same version number (but different content/names)
    (mig_dir / "002_first.sql").write_text("SELECT 2;")
    (mig_dir / "002_second.sql").write_text("SELECT 22;")
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/002_first.sql"], cwd=repo, capture_output=True
    )
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/002_second.sql"], cwd=repo, capture_output=True
    )

    # Run hook - should fail
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, "Expected failure for duplicate versions"
    assert "duplicate migration version" in stderr


def test_refuse_delete_committed_migration(tmp_path):
    """Refuse to delete a committed migration without a corresponding rename."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create and commit two initial migrations
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    (mig_dir / "002_second.sql").write_text("SELECT 2;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Delete one migration without renaming
    (mig_dir / "002_second.sql").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)

    # Run hook - should fail
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, "Expected failure for deletion without rename"
    assert "BLOCKED: committed migration deleted" in stderr


def test_refuse_rename_of_committed_migration(tmp_path):
    """Refuse renaming a committed migration, even at the same version and content.

    A committed path leaving the index is a deletion regardless of what arrives
    alongside it. Nothing pinned this before: the hook carried an unreachable
    rename exception (it read an added-paths file written 54 lines later), and
    deleting that block entirely left the whole suite green. See issue #137.
    """
    repo, mig_dir = setup_test_repo(tmp_path)

    content = "SELECT 2;"
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    (mig_dir / "002_second.sql").write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Same version, byte-identical content, different filename.
    subprocess.run(
        [
            "git",
            "mv",
            "omniagentos/db/migrations/002_second.sql",
            "omniagentos/db/migrations/002_renamed.sql",
        ],
        cwd=repo,
        capture_output=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)

    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, f"Expected failure for committed rename, got stdout: {stdout}"
    assert "BLOCKED: committed migration deleted or renumbered" in stderr
    assert "002_second.sql" in stderr


def test_refuse_renumber_of_committed_migration(tmp_path):
    """Refuse renumbering a committed migration to the next free ordinal.

    This is the regression guard for the trap described in issue #137: the
    tempting one-line repair of the dead rename exception (hoist the added-paths
    pass above the deletion loop) admits exactly this input, because the
    exception matched on blob content alone with no same-version requirement.
    An applied 002 would silently become 003 and re-apply against stale
    bookkeeping — the 044 collision the phase exists to prevent.
    """
    repo, mig_dir = setup_test_repo(tmp_path)

    content = "SELECT 2;"
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    (mig_dir / "002_second.sql").write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Identical content, new ordinal — and 003 IS the next free allocation, so
    # every downstream contiguity check would happily pass it.
    subprocess.run(
        [
            "git",
            "mv",
            "omniagentos/db/migrations/002_second.sql",
            "omniagentos/db/migrations/003_second.sql",
        ],
        cwd=repo,
        capture_output=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)

    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, f"Expected failure for committed renumber, got stdout: {stdout}"
    assert "BLOCKED: committed migration deleted or renumbered" in stderr
    assert "002_second.sql" in stderr


def test_refuse_wrong_next_version(tmp_path):
    """Refuse when new migration skips the expected next version."""
    repo, mig_dir = setup_test_repo(tmp_path)

    # Create initial migration
    (mig_dir / "001_init.sql").write_text("SELECT 1;")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Add a migration with wrong version (003 instead of 002)
    (mig_dir / "003_wrong.sql").write_text("SELECT 3;")
    subprocess.run(
        ["git", "add", "omniagentos/db/migrations/003_wrong.sql"], cwd=repo, capture_output=True
    )

    # Run hook - should fail
    exit_code, stdout, stderr = run_hook(str(repo))
    assert exit_code != 0, "Expected failure for wrong version number"
    assert "not the next allocation" in stderr
