"""The DB → files export bridge (`omniagentos/skills/export.py`) and its publisher.

Every skill here is seeded through the real DAL (`upsert_skill`), so the tests
exercise the same write path production uses — including its auto-quarantine
rules — rather than a hand-built table.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omniagentos.skills import upsert_skill
from omniagentos.skills.export import (
    DROP_SCANNER_FLAGGED,
    DROP_UNSAFE_SLUG,
    EXPORTABLE_STATUSES,
    ExportedSkill,
    SkippedSkill,
    export_skills,
)
from omniagentos.skills.resolve import DROP_DIGEST_MISMATCH, DROP_MISSING_DIGEST

REPO_ROOT = Path(__file__).resolve().parents[2]

CLEAN_BODY = """# Skill: Ship a lane

## Purpose
Take a lane branch from brief to a reviewable commit.

## Steps
1. Read the brief and the code it names.
2. Write the change and its tests.
3. Run the validation ladder.
"""

OTHER_BODY = """# Skill: Triage a red

## Purpose
Decide whether a red is a defect or a mechanic.

## Steps
1. Read the gate receipt.
2. Compare load against the perf-core count.
"""

# The slugs these tests seed. A migrated database is NOT empty: migration 032
# seeds six skills whose versions predate the content_digest column (110), so
# they are dropped as `missing_approval_digest` by the resolver and by the
# export alike (pinned in test_migration_seeded_corpus_is_dropped_for_no_digest).
# Assertions therefore speak about this lane's own slugs.
SEEDED_SLUGS = frozenset({"clean-skill", "second-skill", "archived-skill", "auto-capture"})

FRONTMATTER_KEYS = [
    "slug",
    "category",
    "subcategory",
    "title",
    "summary",
    "status",
    "version",
    "content_digest",
    "exported_at",
]


def _seed(slug: str, *, content: str, status: str = "active", **extra: object) -> str:
    payload: dict[str, object] = {
        "slug": slug,
        "category": "Coding",
        "subcategory": "Delivery",
        "title": f"Title for {slug}",
        "summary": f"Summary for {slug}",
        "status": status,
        "content_snapshot": content,
    }
    payload.update(extra)
    return upsert_skill(payload)


def _mine[Item: (ExportedSkill, SkippedSkill)](items: Sequence[Item]) -> list[Item]:
    """Only the skills these tests seeded (see :data:`SEEDED_SLUGS`)."""
    return [item for item in items if item.slug in SEEDED_SLUGS]


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict)
    return parsed


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---\n", 2)[2]


@pytest.fixture
def seeded(skills_environment: tuple[Path, Path]) -> Path:
    """A library holding one clean skill of every interesting shape."""
    db_path, _vault = skills_environment
    _seed("clean-skill", content=CLEAN_BODY)
    _seed("second-skill", content=OTHER_BODY)
    _seed("archived-skill", content=CLEAN_BODY, status="archived")
    return db_path


def test_exportable_statuses_are_the_ones_the_resolver_serves() -> None:
    # There is no 'approved' status in this schema (migration 109); the export
    # vocabulary is exactly what resolve.py will serve.
    assert EXPORTABLE_STATUSES == ("active", "deprecated", "experimental")


def test_exports_only_servable_statuses(seeded: Path, tmp_path: Path) -> None:
    report = export_skills(tmp_path / "skills-lib")

    assert [item.slug for item in report.exported] == ["clean-skill", "second-skill"]
    assert _mine(report.skipped) == []
    assert (tmp_path / "skills-lib" / "clean-skill" / "SKILL.md").is_file()
    assert not (tmp_path / "skills-lib" / "archived-skill").exists()


def test_migration_seeded_corpus_is_dropped_for_no_digest(seeded: Path, tmp_path: Path) -> None:
    """A migrated DB is not empty, and its seed rows are NOT exportable.

    Migration 032 seeds six skills; their versions predate the
    ``content_digest`` column added by 110, so the resolver refuses to serve
    them and the export must refuse to ship them for the same reason. If a
    later migration backfills those digests this test is the thing that says
    so out loud.
    """
    report = export_skills(tmp_path / "out")

    seeds = [item for item in report.skipped if item.slug not in SEEDED_SLUGS]
    assert seeds, "expected the migration-seeded corpus to be present"
    assert {item.reason for item in seeds} == {DROP_MISSING_DIGEST}
    for item in seeds:
        assert not (tmp_path / "out" / item.slug).exists()


def test_refuses_statuses_the_resolver_would_drop(seeded: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no 'approved' status"):
        export_skills(tmp_path / "out", ("approved", "active"))
    with pytest.raises(ValueError, match="only serves"):
        export_skills(tmp_path / "out", ("archived",))
    with pytest.raises(ValueError, match="at least one status"):
        export_skills(tmp_path / "out", ())
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


def test_frontmatter_carries_the_declared_fields(seeded: Path, tmp_path: Path) -> None:
    report = export_skills(tmp_path / "skills-lib")
    path = tmp_path / "skills-lib" / "clean-skill" / "SKILL.md"

    meta = _frontmatter(path)
    assert list(meta) == FRONTMATTER_KEYS
    assert meta["slug"] == "clean-skill"
    assert meta["category"] == "Coding"
    assert meta["subcategory"] == "Delivery"
    assert meta["title"] == "Title for clean-skill"
    assert meta["summary"] == "Summary for clean-skill"
    assert meta["status"] == "active"
    assert meta["version"] == 1
    assert _body(path) == CLEAN_BODY

    with sqlite3.connect(seeded) as connection:
        row = connection.execute(
            "SELECT v.content_digest, v.created_at FROM skills s JOIN skill_versions v "
            "ON v.skill_id = s.id AND v.version = s.current_version WHERE s.slug = ?",
            ("clean-skill",),
        ).fetchone()
    assert meta["content_digest"] == row[0]
    # exported_at is the version's own timestamp, never now().
    assert str(meta["exported_at"]) == row[1]
    assert report.exported[0].content_digest == row[0]


def test_reexport_is_byte_identical(seeded: Path, tmp_path: Path) -> None:
    first = export_skills(tmp_path / "a")
    again = export_skills(tmp_path / "a")
    other_dir = export_skills(tmp_path / "b")

    assert first.changed_count == 2
    # Nothing changed, so nothing was rewritten: that is what makes
    # "export N changed skill(s)" a true statement.
    assert again.changed_count == 0
    assert [item.changed for item in again.exported] == [False, False]
    for item in other_dir.exported:
        first_bytes = (tmp_path / "a" / item.slug / "SKILL.md").read_bytes()
        assert first_bytes == item.path.read_bytes()


def test_content_change_produces_a_diff(seeded: Path, tmp_path: Path) -> None:
    export_skills(tmp_path / "out")
    _seed("clean-skill", content=CLEAN_BODY + "\n4. Write the completion report.\n")

    report = export_skills(tmp_path / "out")

    changed = {item.slug for item in report.exported if item.changed}
    assert changed == {"clean-skill"}
    assert "Write the completion report." in _body(tmp_path / "out" / "clean-skill" / "SKILL.md")


def test_digest_mismatch_is_dropped_and_never_written(seeded: Path, tmp_path: Path) -> None:
    # Someone edited the body under the digest the write path recorded.
    with sqlite3.connect(seeded) as connection:
        connection.execute(
            "UPDATE skill_versions SET content_snapshot = ? WHERE skill_id = "
            "(SELECT id FROM skills WHERE slug = ?)",
            ("# tampered\n", "second-skill"),
        )

    report = export_skills(tmp_path / "out")

    assert [item.slug for item in report.exported] == ["clean-skill"]
    assert [(item.slug, item.reason) for item in _mine(report.skipped)] == [
        ("second-skill", DROP_DIGEST_MISMATCH)
    ]
    assert not (tmp_path / "out" / "second-skill").exists()


def test_traversal_slug_is_dropped_and_never_becomes_a_path(seeded: Path, tmp_path: Path) -> None:
    # The schema does not constrain slugs; a row written around the DAL (hand
    # edit, another tool) must still never become a filesystem path.
    with sqlite3.connect(seeded) as connection:
        connection.execute(
            "UPDATE skills SET slug = ? WHERE slug = ?", ("../escaped", "second-skill")
        )

    out = tmp_path / "nested" / "out"
    report = export_skills(out)

    assert [item.slug for item in report.exported] == ["clean-skill"]
    dropped = [item for item in report.skipped if item.slug == "../escaped"]
    assert [item.reason for item in dropped] == [DROP_UNSAFE_SLUG]
    assert not (tmp_path / "nested" / "escaped").exists()
    assert not (tmp_path / "escaped").exists()


def test_scanner_flagged_content_is_never_exported(
    seeded: Path, tmp_path: Path, content_with_private_key: str
) -> None:
    # upsert_skill scans on INSERT only; its UPDATE path rewrites the body and
    # recomputes the digest without rescanning, so this row is active, digest-
    # valid and dangerous — exactly what the export-time rescan exists for.
    _seed("second-skill", content=content_with_private_key)

    report = export_skills(tmp_path / "out")

    assert [item.slug for item in report.exported] == ["clean-skill"]
    flagged = [item for item in report.skipped if item.slug == "second-skill"]
    assert [item.reason for item in flagged] == [DROP_SCANNER_FLAGGED]
    assert "private_key_start" in flagged[0].detail
    assert not (tmp_path / "out" / "second-skill").exists()


def test_quarantined_capture_never_reaches_the_export(
    skills_environment: tuple[Path, Path], tmp_path: Path
) -> None:
    _seed(
        "auto-capture",
        content="No step-level detail was recorded in the ledger manifest.\n",
    )

    report = export_skills(tmp_path / "out")

    assert _mine(report.exported) == []
    # Quarantined rows are not candidates at all — they never even reach the
    # resolver, so they are not reported as drops either.
    assert _mine(report.skipped) == []
    assert list((tmp_path / "out").iterdir()) == []


def test_prune_removes_vanished_skills_but_spares_foreign_dirs(
    seeded: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    export_skills(out)
    handwritten = out / "operator-skill"
    handwritten.mkdir()
    (handwritten / "SKILL.md").write_text("---\nslug: operator-skill\n---\n", encoding="utf-8")
    (handwritten / "reference.md").write_text("notes\n", encoding="utf-8")

    with sqlite3.connect(seeded) as connection:
        connection.execute(
            "UPDATE skills SET status = 'archived' WHERE slug = ?", ("second-skill",)
        )

    report = export_skills(out)

    assert report.removed == ("second-skill",)
    assert not (out / "second-skill").exists()
    # A directory holding files this exporter never wrote is not ours to delete.
    assert (handwritten / "reference.md").is_file()
    assert report.changed_count == 1


def test_no_prune_keeps_stale_dirs(seeded: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    export_skills(out)
    with sqlite3.connect(seeded) as connection:
        connection.execute(
            "UPDATE skills SET status = 'archived' WHERE slug = ?", ("second-skill",)
        )

    report = export_skills(out, prune=False)

    assert report.removed == ()
    assert (out / "second-skill" / "SKILL.md").is_file()


def test_module_entry_point_runs(seeded: Path, tmp_path: Path) -> None:
    """`python -m omniagentos.skills export --out …` is the documented command.

    Calling ``main()`` in-process would not prove the module is reachable that
    way, which is the whole contract with the hub cron.
    """
    out = tmp_path / "skills-lib"
    result = subprocess.run(
        [sys.executable, "-m", "omniagentos.skills", "export", "--out", str(out)],
        cwd=REPO_ROOT,
        env={**os.environ, "OMNIAGENTOS_DB": str(seeded)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "skill(s) exported" in result.stdout
    assert (out / "clean-skill" / "SKILL.md").is_file()


def test_cli_export(seeded: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omniagentos.skills.__main__ import main

    assert main(["export", "--out", str(tmp_path / "skills-lib")]) == 0

    printed = capsys.readouterr().out
    assert "2 skill(s) exported" in printed
    assert (tmp_path / "skills-lib" / "clean-skill" / "SKILL.md").is_file()


def test_cli_export_json_and_strict(
    seeded: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from omniagentos.skills.__main__ import main

    with sqlite3.connect(seeded) as connection:
        connection.execute(
            "UPDATE skill_versions SET content_snapshot = 'tampered' WHERE skill_id = "
            "(SELECT id FROM skills WHERE slug = ?)",
            ("second-skill",),
        )

    assert main(["export", "--out", str(tmp_path / "out"), "--json", "--strict"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [item["slug"] for item in payload["exported"]] == ["clean-skill"]
    assert [item["reason"] for item in payload["skipped"] if item["slug"] in SEEDED_SLUGS] == [
        DROP_DIGEST_MISMATCH
    ]


# --------------------------------------------------------------------------
# scripts/team/skills_publish.py — the thin git wrapper around the export.
# --------------------------------------------------------------------------


def _load_publish() -> object:
    import importlib.util
    import sys

    path = REPO_ROOT / "scripts" / "team" / "skills_publish.py"
    spec = importlib.util.spec_from_file_location("skills_publish_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch", "main")
    _git(repo, "config", "user.email", "seat@example.invalid")
    _git(repo, "config", "user.name", "Test Seat")
    (repo / "README.md").write_text("clone\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def test_publish_commits_then_stays_silent_when_nothing_changed(
    seeded: Path, git_repo: Path
) -> None:
    publish = _load_publish()

    first = publish.publish(git_repo)  # type: ignore[attr-defined]
    assert first.committed is True
    assert first.message == "skills-lib: export 2 changed skill(s)"
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    tracked = _git(git_repo, "ls-files", "skills-lib").stdout.split()
    assert tracked == ["skills-lib/clean-skill/SKILL.md", "skills-lib/second-skill/SKILL.md"]

    second = publish.publish(git_repo)  # type: ignore[attr-defined]
    assert second.committed is False
    assert second.changed_paths == ()
    assert _git(git_repo, "rev-parse", "HEAD").stdout.strip() == head


def test_publish_never_sweeps_up_unrelated_staged_work(seeded: Path, git_repo: Path) -> None:
    publish = _load_publish()
    (git_repo / "unrelated.txt").write_text("someone else's work\n", encoding="utf-8")
    _git(git_repo, "add", "unrelated.txt")

    publish.publish(git_repo)  # type: ignore[attr-defined]

    committed = _git(git_repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert committed == ["skills-lib/clean-skill/SKILL.md", "skills-lib/second-skill/SKILL.md"]
    assert "unrelated.txt" in _git(git_repo, "diff", "--cached", "--name-only").stdout


def test_publish_writes_skills_lib_not_skills(seeded: Path, git_repo: Path) -> None:
    publish = _load_publish()
    handwritten = git_repo / "skills" / "operator-skill"
    handwritten.mkdir(parents=True)
    (handwritten / "SKILL.md").write_text("---\nslug: operator-skill\n---\n", encoding="utf-8")

    publish.publish(git_repo)  # type: ignore[attr-defined]

    assert (handwritten / "SKILL.md").read_text(encoding="utf-8").startswith("---\n")
    assert sorted(p.name for p in (git_repo / "skills").iterdir()) == ["operator-skill"]
