"""TN.5 — promote: deterministic, verified, non-agentic copy of APPROVED artifacts.

The tests that carry the weight:

``test_source_drift_stops_the_whole_promotion``
    A file that changed between approval and promotion is the thing a hash check
    exists for, and phase 1 must abort with ZERO files copied rather than
    shipping the drifted one and reporting it afterwards.

``test_wildcard_dest_must_be_a_directory``
    A wildcard whose destination is a single file can only be correct by
    accident; it is refused instead of resolved.

``test_promote_approved_ignores_a_newer_unapproved_version``
    The reason ``version``/``lineage_id`` exist at all.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.contracts import TaskMode
from omniagentos.db.store import SqliteStore
from omniagentos.workmodes.manifest import ManifestEntry, WorkArtifactStore, build_manifest
from omniagentos.workmodes.promote import (
    PromoteError,
    PromoteMapping,
    entries_from_rows,
    plan_promotion,
    promote,
    promote_approved,
)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts" / "tsk_1"
    (root / "reports" / "q1").mkdir(parents=True)
    (root / "reports" / "summary.md").write_text("summary body\n" * 60, encoding="utf-8")
    (root / "reports" / "q1" / "detail.md").write_text("detail body\n" * 60, encoding="utf-8")
    (root / "notes.txt").write_text("notes\n" * 60, encoding="utf-8")
    return root


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    root = tmp_path / "final"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    created = SqliteStore(str(tmp_path / "promote.db"))
    yield created
    created.close()


def _entries(src: Path) -> tuple[ManifestEntry, ...]:
    return build_manifest(TaskMode.REPORT, str(src)).entries


# --- planning --------------------------------------------------------------


def test_literal_mapping_copies_one_file(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/summary.md", "delivered.md")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert plan.ok
    assert [(c.source_rel, c.dest_rel) for c in plan.copies] == [
        ("reports/summary.md", "delivered.md")
    ]
    result = promote(plan)
    assert result.ok
    assert (dest / "delivered.md").read_text(encoding="utf-8").startswith("summary body")


def test_wildcard_preserves_structure_under_the_dest_directory(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/**/*.md", "out/")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert plan.ok
    assert {c.dest_rel for c in plan.copies} == {"out/summary.md", "out/q1/detail.md"}
    assert promote(plan).ok
    assert (dest / "out" / "q1" / "detail.md").is_file()


def test_star_does_not_cross_a_slash(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/*.md", "out/")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert {c.source_rel for c in plan.copies} == {"reports/summary.md"}


def test_wildcard_dest_must_be_a_directory(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/*.md", "delivered.md")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert not plan.ok
    assert [e.kind for e in plan.errors] == ["wildcard_dest_not_dir"]
    with pytest.raises(PromoteError, match="wildcard_dest_not_dir"):
        promote(plan)


def test_wildcard_dest_that_already_exists_as_a_directory_is_accepted(
    src: Path, dest: Path
) -> None:
    (dest / "out").mkdir()
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/*.md", "out")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert plan.ok


def test_no_match_is_an_error(src: Path, dest: Path) -> None:
    """A promotion that silently copies nothing is a lie told to whoever approved it."""
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/nope.md", "x.md")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in plan.errors] == ["no_match"]


@pytest.mark.parametrize("pattern", ["../escape.md", "/etc/passwd", "~/secrets.md", "."])
def test_pattern_traversal_is_rejected(src: Path, dest: Path, pattern: str) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping(pattern, "x.md")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in plan.errors] == ["bad_pattern"]


@pytest.mark.parametrize("bad_dest", ["../escape.md", "/etc/passwd", "~/secrets.md"])
def test_dest_traversal_is_rejected(src: Path, dest: Path, bad_dest: str) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/summary.md", bad_dest)],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in plan.errors] == ["bad_dest"]


def test_symlinked_dest_escape_is_rejected(src: Path, dest: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest / "link").symlink_to(outside, target_is_directory=True)
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("reports/summary.md", "link/leak.md")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in plan.errors] == ["dest_escape"]


def test_dest_collision_is_rejected(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [
            PromoteMapping("reports/summary.md", "same.md"),
            PromoteMapping("notes.txt", "same.md"),
        ],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in plan.errors] == ["dest_collision"]


def test_mappings_accept_tuples_and_dicts(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [("reports/summary.md", "a.md"), {"pattern": "notes.txt", "dest": "b.txt"}],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert plan.ok
    assert {c.dest_rel for c in plan.copies} == {"a.md", "b.txt"}


def test_a_manifest_can_be_passed_directly(src: Path, dest: Path) -> None:
    manifest = build_manifest(TaskMode.REPORT, str(src))
    plan = plan_promotion(
        manifest,
        [PromoteMapping("notes.txt", "notes.txt")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert plan.ok


# --- executing -------------------------------------------------------------


def test_source_drift_stops_the_whole_promotion(src: Path, dest: Path) -> None:
    entries = _entries(src)
    plan = plan_promotion(
        entries,
        [PromoteMapping("reports/**/*.md", "out/"), PromoteMapping("notes.txt", "notes.txt")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert plan.ok
    (src / "reports" / "summary.md").write_text("TAMPERED", encoding="utf-8")
    result = promote(plan)
    assert result.ok is False
    assert [e.kind for e in result.errors] == ["source_drift"]
    # Phase 1 aborts before ANY copy: the clean files did not ship either.
    assert list(dest.iterdir()) == []


def test_missing_source_stops_the_promotion(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("notes.txt", "notes.txt")],
        source_root=str(src),
        dest_root=str(dest),
    )
    (src / "notes.txt").unlink()
    result = promote(plan)
    assert [e.kind for e in result.errors] == ["source_missing"]


def test_existing_dest_refuses_unless_overwrite(src: Path, dest: Path) -> None:
    (dest / "notes.txt").write_text("older", encoding="utf-8")
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("notes.txt", "notes.txt")],
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in promote(plan).errors] == ["dest_exists"]
    assert (dest / "notes.txt").read_text(encoding="utf-8") == "older"
    assert promote(plan, overwrite=True).ok
    assert (dest / "notes.txt").read_text(encoding="utf-8").startswith("notes")


def test_dry_run_copies_nothing(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("notes.txt", "notes.txt")],
        source_root=str(src),
        dest_root=str(dest),
    )
    result = promote(plan, dry_run=True)
    assert result.ok
    assert list(dest.iterdir()) == []


def test_copy_is_hash_verified(src: Path, dest: Path) -> None:
    entries = _entries(src)
    plan = plan_promotion(
        entries,
        [PromoteMapping("notes.txt", "notes.txt")],
        source_root=str(src),
        dest_root=str(dest),
    )
    result = promote(plan)
    assert result.copied[0].verified is True
    assert result.copied[0].sha256 == plan.copies[0].sha256


def test_promote_refuses_a_plan_with_errors(src: Path, dest: Path) -> None:
    plan = plan_promotion(
        _entries(src),
        [PromoteMapping("nope.md", "x.md")],
        source_root=str(src),
        dest_root=str(dest),
    )
    with pytest.raises(PromoteError):
        promote(plan)


# --- approved-version targeting -------------------------------------------


def test_entries_from_rows_drops_paths_outside_the_source_root(src: Path) -> None:
    rows = [
        {"id": "wart_1", "file_path": str(src / "notes.txt"), "sha256": "a" * 64, "byte_size": 1},
        {"id": "wart_2", "file_path": "/etc/passwd", "sha256": "b" * 64, "byte_size": 1},
    ]
    entries = entries_from_rows(rows, str(src))
    assert [entry.rel_path for entry in entries] == ["notes.txt"]
    assert entries[0].artifact_id == "wart_1"


def test_promote_approved_ignores_a_newer_unapproved_version(
    store: SqliteStore, src: Path, dest: Path
) -> None:
    dal = WorkArtifactStore(store)
    notes = src / "notes.txt"
    from omniagentos.workmodes.manifest import file_sha256

    v1 = dal.record(
        task_mode=TaskMode.REPORT,
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        file_path=str(notes),
        sha256=file_sha256(str(notes)),
        byte_size=notes.stat().st_size,
    )
    dal.approve(v1["id"])
    # A revision lands on disk and in the registry, but nobody approved it.
    notes.write_text("v2 notes\n" * 60, encoding="utf-8")
    dal.record(
        task_mode=TaskMode.REPORT,
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        file_path=str(notes),
        sha256=file_sha256(str(notes)),
        byte_size=notes.stat().st_size,
        lineage_id=v1["lineage_id"],
    )
    plan, result = promote_approved(
        dal,
        [PromoteMapping("notes.txt", "notes.txt")],
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        source_root=str(src),
        dest_root=str(dest),
    )
    # The approved v1 no longer matches what is on disk -> the promotion stops
    # instead of shipping the unapproved v2 bytes.
    assert plan.ok
    assert [e.kind for e in result.errors] == ["source_drift"]
    assert list(dest.iterdir()) == []


def test_promote_approved_copies_the_approved_version(
    store: SqliteStore, src: Path, dest: Path
) -> None:
    from omniagentos.workmodes.manifest import file_sha256

    dal = WorkArtifactStore(store)
    notes = src / "notes.txt"
    row = dal.record(
        task_mode=TaskMode.REPORT,
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        file_path=str(notes),
        sha256=file_sha256(str(notes)),
        byte_size=notes.stat().st_size,
    )
    dal.approve(row["id"])
    plan, result = promote_approved(
        dal,
        [PromoteMapping("notes.txt", "final-notes.txt")],
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        source_root=str(src),
        dest_root=str(dest),
    )
    assert result.ok
    assert (dest / "final-notes.txt").is_file()
    assert plan.copies[0].artifact_id == row["id"]


def test_promote_approved_with_nothing_approved(store: SqliteStore, src: Path, dest: Path) -> None:
    dal = WorkArtifactStore(store)
    dal.record(work_ref_type="board_task", work_ref_id="tsk_1", file_path=str(src / "notes.txt"))
    plan, result = promote_approved(
        dal,
        [PromoteMapping("notes.txt", "x.txt")],
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        source_root=str(src),
        dest_root=str(dest),
    )
    assert [e.kind for e in result.errors] == ["no_approved_artifacts"]
    assert not plan.ok
    assert list(dest.iterdir()) == []


def test_promote_is_not_agentic(src: Path) -> None:
    """No model, no network, no shell: promote is stdlib file IO by design."""
    source = (
        Path(__file__).resolve().parents[2] / "omniagentos" / "workmodes" / "promote.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "requests", "httpx", "adapter", "llm", "prompt("):
        assert forbidden not in source
    assert os.path.basename(__file__) == "test_promote.py"
