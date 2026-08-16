"""TN.3/TN.4 — the manifest, its work_artifacts rows, and manifest GRADING.

The two subtleties this suite exists to pin down:

``test_fast_pass_when_the_manifest_is_complete_and_clean``
    Existence is settled by evidence, so an assessor call can only add latency
    and variance. ``fast_pass`` is reported separately from ``verdict`` because
    it answers "is it there and valid", never "is it good".

``test_fail_override_*``
    A FAIL whose reasons are ALL "missing / not found / cannot be confirmed" is
    an assessor that looked in the wrong place. It is overridden — to PASS when
    the manifest independently clears the work, to NEEDS_REVIEW when it does not
    — and it is NOT overridden when even one reason is a substantive defect. The
    third of those is the important one: without it, the override would launder
    real quality failures into passes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.contracts import AssessmentVerdict, TaskMode
from omniagentos.db.store import SqliteStore
from omniagentos.workmodes.manifest import (
    ArtifactManifest,
    ManifestEntry,
    WorkArtifactStore,
    apply_fail_override,
    build_manifest,
    file_sha256,
    grade_manifest,
    is_lookup_failure_reason,
    load_manifest,
    manifest_path,
    write_manifest,
)
from omniagentos.workmodes.modes import WorkModeError
from omniagentos.workmodes.protocols import MANIFEST_FILENAME, PROMPT_FILENAME, build_acceptance


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    created = SqliteStore(str(tmp_path / "wm.db"))
    yield created
    created.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts" / "tsk_1"
    path.mkdir(parents=True)
    return path


# --- building --------------------------------------------------------------


def test_manifest_hashes_and_sorts(root: Path) -> None:
    (root / "b.md").write_text("second file body\n" * 40, encoding="utf-8")
    (root / "a.md").write_text("first file body\n" * 40, encoding="utf-8")
    manifest = build_manifest(TaskMode.REPORT, str(root), scope="tsk_1")
    assert manifest.rel_paths == ("a.md", "b.md")  # sorted: manifests must diff
    assert manifest.entry("a.md").sha256 == file_sha256(str(root / "a.md"))  # type: ignore[union-attr]
    assert manifest.ok is True


def test_nested_files_are_recorded_with_posix_paths(root: Path) -> None:
    (root / "q1").mkdir()
    (root / "q1" / "summary.md").write_text("body\n" * 60, encoding="utf-8")
    manifest = build_manifest(TaskMode.REPORT, str(root))
    assert manifest.rel_paths == ("q1/summary.md",)


def test_dotfiles_and_symlinks_are_skipped(root: Path, tmp_path: Path) -> None:
    (root / "r.md").write_text("body\n" * 60, encoding="utf-8")
    (root / ".hidden").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("y", encoding="utf-8")
    (root / "link.md").symlink_to(outside)
    manifest = build_manifest(TaskMode.REPORT, str(root))
    assert manifest.rel_paths == ("r.md",)


def test_protocol_violations_land_in_the_manifest(root: Path) -> None:
    (root / "hero.gif").write_bytes(b"\x00" * 4096)
    manifest = build_manifest(TaskMode.IMAGE, str(root), prompt="a hero image", probe=False)
    assert manifest.ok is False
    assert any("not allowed" in v for v in manifest.violations)


def test_prompt_is_read_from_the_prompt_file(root: Path) -> None:
    (root / PROMPT_FILENAME).write_text("a neon poster of a fox", encoding="utf-8")
    (root / "a.png").write_bytes(b"\x89PNG" + b"\x00" * 4096)
    manifest = build_manifest(TaskMode.IMAGE, str(root), probe=False)
    assert manifest.prompt == "a neon poster of a fox"
    assert manifest.ok is True
    assert manifest.entry(PROMPT_FILENAME).artifact_type == "prompt"  # type: ignore[union-attr]


def test_expected_file_absent_is_a_violation(root: Path) -> None:
    (root / "other.md").write_text("body\n" * 60, encoding="utf-8")
    manifest = build_manifest(TaskMode.REPORT, str(root), expected_files=["summary.md"])
    assert manifest.missing_expected == ("summary.md",)
    assert any("expected file absent" in v for v in manifest.violations)


def test_empty_file_counts_as_missing(root: Path) -> None:
    (root / "summary.md").write_text("", encoding="utf-8")
    manifest = build_manifest(TaskMode.REPORT, str(root), expected_files=["summary.md"])
    assert manifest.missing_expected == ("summary.md",)


def test_missing_root_yields_an_empty_manifest(tmp_path: Path) -> None:
    manifest = build_manifest(TaskMode.REPORT, str(tmp_path / "nope"))
    assert manifest.entries == ()
    assert any("no deliverable" in v for v in manifest.violations)


def test_acceptance_criteria_are_recorded(root: Path) -> None:
    (root / "ads.json").write_text(json.dumps({"headline": ["x"]}), encoding="utf-8")
    acceptance = build_acceptance(
        TaskMode.CONTENT, expected_files=["ads.json"], platform="meta", notes="three angles"
    )
    manifest = build_manifest(TaskMode.CONTENT, str(root), acceptance=acceptance)
    assert manifest.expected_files == ("ads.json",)
    assert manifest.acceptance["platform"] == "meta"
    assert manifest.acceptance["rubric"]


def test_write_and_load_round_trip(root: Path) -> None:
    (root / "r.md").write_text("body\n" * 60, encoding="utf-8")
    manifest = build_manifest(TaskMode.REPORT, str(root), scope="tsk_1")
    path = write_manifest(manifest)
    assert path == manifest_path(str(root))
    assert not os.path.exists(path + ".tmp")  # atomic replace left no debris
    loaded = load_manifest(path)
    assert loaded.rel_paths == manifest.rel_paths
    assert loaded.task_mode is TaskMode.REPORT
    assert loaded.to_json() == manifest.to_json()


def test_unknown_mode_is_refused(root: Path) -> None:
    with pytest.raises(WorkModeError):
        build_manifest("brochure", str(root))


# --- grading ---------------------------------------------------------------


def _manifest(entries: tuple[ManifestEntry, ...], **kwargs: object) -> ArtifactManifest:
    return ArtifactManifest(
        task_mode=TaskMode.REPORT,
        scope="tsk_1",
        artifact_root="/var/artifacts/tsk_1",
        entries=entries,
        **kwargs,  # type: ignore[arg-type]
    )


def test_fast_pass_when_the_manifest_is_complete_and_clean() -> None:
    manifest = _manifest(
        (ManifestEntry("summary.md", "a" * 64, 4096),), expected_files=("summary.md",)
    )
    grade = grade_manifest(manifest)
    assert grade.verdict is AssessmentVerdict.PASS
    assert grade.fast_pass is True
    assert grade.clean is True


def test_missing_expected_file_fails() -> None:
    manifest = _manifest((ManifestEntry("other.md", "a" * 64, 10),), expected_files=("summary.md",))
    grade = grade_manifest(manifest)
    assert grade.verdict is AssessmentVerdict.FAIL
    assert grade.fast_pass is False
    assert grade.missing == ("summary.md",)


def test_protocol_violation_blocks_the_fast_pass() -> None:
    manifest = _manifest(
        (ManifestEntry("a.md", "a" * 64, 10),), violations=("a.md: 10 bytes is below the floor",)
    )
    grade = grade_manifest(manifest)
    assert grade.verdict is AssessmentVerdict.FAIL
    assert grade.fast_pass is False


def test_manifest_with_only_bookkeeping_files_is_not_a_pass() -> None:
    manifest = _manifest(
        (
            ManifestEntry(MANIFEST_FILENAME, "a" * 64, 100),
            ManifestEntry(PROMPT_FILENAME, "b" * 64, 20),
        )
    )
    grade = grade_manifest(manifest)
    assert grade.verdict is AssessmentVerdict.FAIL
    assert any("no deliverable" in r for r in grade.reasons)


def test_no_manifest_is_needs_review_not_fail() -> None:
    """Unmeasured is not bad -- the same distinction ObservedChange makes."""
    grade = grade_manifest(None)
    assert grade.verdict is AssessmentVerdict.NEEDS_REVIEW
    assert grade.fast_pass is False


def test_expected_files_can_be_overridden_at_grade_time() -> None:
    manifest = _manifest((ManifestEntry("summary.md", "a" * 64, 4096),))
    assert grade_manifest(manifest, expected_files=["other.md"]).verdict is AssessmentVerdict.FAIL


# --- fail-override ---------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "No files were found in the working directory",
        "the report is missing",
        "summary.md not found",
        "I cannot confirm the deliverable was produced",
        "unable to locate any output",
        "the diff is empty; nothing was produced",
        "no evidence of any artifacts",
        "the output does not exist",
    ],
)
def test_lookup_failure_reasons_are_recognized(reason: str) -> None:
    assert is_lookup_failure_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "the report is missing the required disclaimer",
        "the copy is missing a call to action",
        "headline exceeds the character limit",
        "the analysis is inaccurate",
        "the deliverable does not meet the brief",
        "contains a banned claim",
        "the body is placeholder lorem ipsum text",
        "missing citations for the central claim",
    ],
)
def test_quality_defects_are_never_lookup_failures(reason: str) -> None:
    assert is_lookup_failure_reason(reason) is False


def test_fail_override_to_pass_when_the_manifest_clears_the_work() -> None:
    manifest = _manifest(
        (ManifestEntry("summary.md", "a" * 64, 4096),), expected_files=("summary.md",)
    )
    override = apply_fail_override(
        AssessmentVerdict.FAIL,
        ["No changed files were found", "cannot confirm the report was written"],
        manifest=manifest,
    )
    assert override.overridden is True
    assert override.verdict is AssessmentVerdict.PASS
    assert override.original is AssessmentVerdict.FAIL


def test_fail_override_to_needs_review_without_a_manifest() -> None:
    override = apply_fail_override(AssessmentVerdict.FAIL, ["no files found"], manifest=None)
    assert override.overridden is True
    assert override.verdict is AssessmentVerdict.NEEDS_REVIEW


def test_fail_override_to_needs_review_when_the_manifest_is_dirty() -> None:
    manifest = _manifest(
        (ManifestEntry("summary.md", "a" * 64, 4096),), violations=("summary.md: bad format",)
    )
    override = apply_fail_override(AssessmentVerdict.FAIL, ["nothing was found"], manifest=manifest)
    assert override.verdict is AssessmentVerdict.NEEDS_REVIEW


def test_one_substantive_reason_blocks_the_override() -> None:
    """The guard that stops the override laundering real defects into passes."""
    manifest = _manifest(
        (ManifestEntry("summary.md", "a" * 64, 4096),), expected_files=("summary.md",)
    )
    override = apply_fail_override(
        AssessmentVerdict.FAIL,
        ["no files found", "the report is missing the required disclaimer"],
        manifest=manifest,
    )
    assert override.overridden is False
    assert override.verdict is AssessmentVerdict.FAIL
    assert "substantive" in override.note


def test_non_fail_verdicts_pass_through() -> None:
    for verdict in (
        AssessmentVerdict.PASS,
        AssessmentVerdict.NEEDS_REVIEW,
        AssessmentVerdict.BLOCKED,
    ):
        result = apply_fail_override(verdict, ["not found"])
        assert result.overridden is False
        assert result.verdict is verdict


def test_fail_with_no_reasons_is_untouched() -> None:
    result = apply_fail_override(AssessmentVerdict.FAIL, [])
    assert result.overridden is False
    assert result.verdict is AssessmentVerdict.FAIL


# --- work_artifacts registry ----------------------------------------------


def test_record_starts_a_lineage_at_v1(store: SqliteStore) -> None:
    dal = WorkArtifactStore(store)
    row = dal.record(
        task_mode=TaskMode.REPORT,
        work_ref_type="board_task",
        work_ref_id="tsk_1",
        file_path="/var/artifacts/tsk_1/summary.md",
        sha256="a" * 64,
        byte_size=4096,
    )
    assert row["version"] == 1
    assert row["lineage_id"] == row["id"]  # v1's id IS the lineage key
    assert dal.get(row["id"])["version"] == 1  # type: ignore[index]


def test_revision_shares_the_lineage_and_bumps_the_version(store: SqliteStore) -> None:
    dal = WorkArtifactStore(store)
    v1 = dal.record(task_mode=TaskMode.REPORT, file_path="/a/summary.md", sha256="a" * 64)
    v2 = dal.record(
        task_mode=TaskMode.REPORT,
        file_path="/a/summary.md",
        sha256="b" * 64,
        lineage_id=v1["lineage_id"],
    )
    assert v2["version"] == 2
    assert v2["lineage_id"] == v1["lineage_id"]
    assert dal.latest_version(v1["lineage_id"])["id"] == v2["id"]  # type: ignore[index]


def test_promote_targets_the_approved_version_not_the_newest(store: SqliteStore) -> None:
    """The whole point of version/lineage: an unapproved v2 must not ship."""
    dal = WorkArtifactStore(store)
    v1 = dal.record(task_mode=TaskMode.REPORT, file_path="/a/summary.md", sha256="a" * 64)
    dal.approve(v1["id"])
    v2 = dal.record(
        task_mode=TaskMode.REPORT,
        file_path="/a/summary.md",
        sha256="b" * 64,
        lineage_id=v1["lineage_id"],
    )
    assert dal.latest_version(v1["lineage_id"])["id"] == v2["id"]  # type: ignore[index]
    assert dal.approved_version(v1["lineage_id"])["id"] == v1["id"]  # type: ignore[index]


def test_approve_is_idempotent_and_keeps_the_first_timestamp(store: SqliteStore) -> None:
    dal = WorkArtifactStore(store)
    row = dal.record(task_mode=TaskMode.REPORT, file_path="/a/x.md")
    assert dal.approve(row["id"], now="2026-01-01T00:00:00Z") is True
    assert dal.approve(row["id"], now="2026-02-02T00:00:00Z") is True
    assert dal.get(row["id"])["approved_at"] == "2026-01-01T00:00:00Z"  # type: ignore[index]
    assert dal.approve("wart_nope") is False


def test_approved_for_ref_returns_one_row_per_lineage(store: SqliteStore) -> None:
    dal = WorkArtifactStore(store)
    a1 = dal.record(work_ref_type="board_task", work_ref_id="t", file_path="/a/a.md")
    dal.approve(a1["id"])
    a2 = dal.record(
        work_ref_type="board_task",
        work_ref_id="t",
        file_path="/a/a.md",
        lineage_id=a1["lineage_id"],
    )
    dal.approve(a2["id"])
    b1 = dal.record(work_ref_type="board_task", work_ref_id="t", file_path="/a/b.md")
    approved = dal.approved_for_ref("board_task", "t")
    assert {row["id"] for row in approved} == {a2["id"]}
    dal.approve(b1["id"])
    assert {row["id"] for row in dal.approved_for_ref("board_task", "t")} == {a2["id"], b1["id"]}


def test_record_manifest_registers_every_deliverable(store: SqliteStore, root: Path) -> None:
    (root / "a.md").write_text("body\n" * 60, encoding="utf-8")
    (root / "b.md").write_text("body\n" * 60, encoding="utf-8")
    manifest = build_manifest(TaskMode.REPORT, str(root), scope="tsk_1")
    write_manifest(manifest)
    manifest = build_manifest(TaskMode.REPORT, str(root), scope="tsk_1")
    dal = WorkArtifactStore(store)
    rows = dal.record_manifest(manifest, work_ref_type="board_task", work_ref_id="tsk_1")
    # manifest.json itself is bookkeeping, not a deliverable
    assert {os.path.basename(str(row["file_path"])) for row in rows} == {"a.md", "b.md"}
    assert all(row["task_mode"] == "report" for row in rows)
    assert all(row["sha256"] for row in rows)
    assert len(dal.list_for_ref("board_task", "tsk_1")) == 2


def test_record_manifest_continues_a_lineage(store: SqliteStore, root: Path) -> None:
    (root / "a.md").write_text("v1 body\n" * 60, encoding="utf-8")
    dal = WorkArtifactStore(store)
    first = dal.record_manifest(build_manifest(TaskMode.REPORT, str(root)), work_ref_id="t")
    (root / "a.md").write_text("v2 body\n" * 60, encoding="utf-8")
    second = dal.record_manifest(
        build_manifest(TaskMode.REPORT, str(root)),
        work_ref_id="t",
        lineage_by_path={"a.md": str(first[0]["lineage_id"])},
    )
    assert second[0]["version"] == 2
    assert second[0]["lineage_id"] == first[0]["lineage_id"]


def test_unknown_artifact_type_is_refused_before_sqlite(store: SqliteStore) -> None:
    with pytest.raises(WorkModeError, match="artifact_type"):
        WorkArtifactStore(store).record(artifact_type="brochure")


def test_unknown_task_mode_is_refused(store: SqliteStore) -> None:
    with pytest.raises(WorkModeError, match="task mode"):
        WorkArtifactStore(store).record(task_mode="brochure")


def test_unknown_storage_kind_is_refused(store: SqliteStore) -> None:
    with pytest.raises(WorkModeError, match="storage_kind"):
        WorkArtifactStore(store).record(storage_kind="s3")


@pytest.mark.parametrize(
    "reason",
    [
        "the report gives no evidence for its central claim",
        "no citations for the revenue figure",
        "there is no evidence that the numbers were checked",
    ],
)
def test_evidence_judgements_are_not_lookup_failures(reason: str) -> None:
    """'no evidence OF ANY ARTIFACTS' is a lookup failure; 'no evidence FOR the
    claim' is a judgement about content and must never trigger the override."""
    assert is_lookup_failure_reason(reason) is False
