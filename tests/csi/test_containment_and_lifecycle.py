"""Behavioral CSI containment, fencing, idempotence, and recovery tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

import omniagentos.csi.implement as implement_module
from omniagentos.csi.conflict import ConflictForecastService
from omniagentos.csi.frozen import is_frozen_path
from omniagentos.csi.implement import (
    _secure_write_text,
    approve_run,
    cleanup_implementation,
    implement_approved,
    recover_implementation,
)
from omniagentos.csi.models import EvidencePacket, PlannerPlan, PlannerProposal, SynthesisResult
from omniagentos.csi.planner import PlannerExecution, RoutinePlanner
from omniagentos.csi.store import CsiStore, approval_binding
from omniagentos.csi.synthesis import PlanSynthesisService
from tests.support.db_template import migrated_db


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _seed(
    db: Path,
    repo: Path,
    *,
    paths: list[str] | None = None,
) -> str:
    # _seed is the sole creator of every ``db`` path in this module (each test
    # gets its own tmp_path), so materialising the pre-migrated template here
    # replaces one full 86-migration apply per test with a file copy.
    store = CsiStore(migrated_db(CsiStore, db))
    base_sha = _sha(repo)
    run_id = store.create_run(
        routine_id="self_learning",
        window_days=7,
        codebase_sha=base_sha,
        evidence={"codebase_sha": base_sha, "signal": "repeat failure"},
    )
    proposal = PlannerProposal(
        change="record a durable recovery note",
        affected_paths=paths or ["vault/skills/self-learning/SKILL.md"],
        baseline="five repeats",
        target="zero repeats",
        measured_by="repeat scan",
        rollback="revert the note",
        confidence=0.9,
    )
    store.finish_run(
        run_id,
        status="AWAITING_HUMAN",
        verdict="propose",
        synthesis=SynthesisResult(
            verdict="propose",
            accepted_proposals=[proposal],
            panel_size=2,
        ),
        conflict={"safe_to_implement": True},
        approval_status="proposed",
    )
    assert approve_run(run_id=run_id, db_path=db, approved_by="owner")
    return run_id


def _preserve_meta_status(
    store: CsiStore,
    run_id: str,
    status: str,
    *,
    repo_root: Path | None = None,
) -> None:
    row = store.get_run(run_id)
    assert row is not None
    assert store.set_status(
        run_id,
        status,
        implement_json=json.loads(row["implement_json"]),
        repo_root=repo_root,
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape.md",
        "vault/../escape.md",
        "vault/skills/../../escape.md",
        "~/escape.md",
        "/tmp/csi-escape.md",
        "file:///tmp/csi-escape.md",
        "Vault/skills/escape.md",
        "vault/Skills/self-learning/SKILL.md",
        "vault//skills/escape.md",
        "vault/./skills/escape.md",
        r"vault\skills\escape.md",
        " vault/skills/escape.md",
        "vault/skills/escape.md ",
        "vault/%2e%2e/escape.md",
        "vault/skills/\x00escape.md",
    ],
)
def test_implement_rejects_every_escape_family_before_worktree_creation(
    tmp_path: Path,
    unsafe: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo, paths=[unsafe])

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert result.status == "REJECTED"
    assert not (repo / "var" / "csi" / "worktrees" / run_id).exists()
    assert _git(repo, "branch", "--list", f"csi/{run_id[:16]}").stdout.strip() == ""


def test_existing_symlink_escape_is_rejected_without_touching_target(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / "vault" / "link", target_is_directory=True)
    _git(repo, "add", "vault/link")
    _git(repo, "commit", "-m", "tracked symlink")
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo, paths=["vault/link/escaped.md"])

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert "escape" in result.error or "symlink" in result.error
    assert not (outside / "escaped.md").exists()


def test_frozen_rules_are_case_folded() -> None:
    assert is_frozen_path("OMNIAGENTOS/CSI/implement.py")
    assert is_frozen_path("Configs/Policy.yaml")


def test_secure_writer_refuses_symlink_swapped_after_realpath_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    skills = worktree / "vault" / "skills"
    skills.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_check = implement_module.assert_canonical_destination
    calls = 0

    def _race(path, *, root, allowed_prefix="vault"):
        nonlocal calls
        result = original_check(path, root=root, allowed_prefix=allowed_prefix)
        calls += 1
        if calls == 3:
            skills.rename(worktree / "vault" / "skills-before-race")
            os.symlink(outside, skills, target_is_directory=True)
        return result

    monkeypatch.setattr(implement_module, "assert_canonical_destination", _race)

    with pytest.raises(PermissionError, match="symlink"):
        _secure_write_text(worktree, "vault/skills/escaped.md", "unsafe")
    assert not (outside / "escaped.md").exists()


def test_secure_writer_rolls_back_when_open_parent_is_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    skills = worktree / "vault" / "skills"
    safe = worktree / "vault" / "safe"
    skills.mkdir(parents=True)
    safe.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / "moved-skills"
    original_rename = implement_module._atomic_rename_at
    raced = False

    def _move_parent_then_publish(dir_fd, source, destination, *, exchange):
        nonlocal raced
        if not raced:
            skills.rename(moved)
            os.symlink(safe, skills, target_is_directory=True)
            raced = True
        return original_rename(
            dir_fd,
            source,
            destination,
            exchange=exchange,
        )

    monkeypatch.setattr(implement_module, "_atomic_rename_at", _move_parent_then_publish)

    with pytest.raises(PermissionError, match="renamed directory"):
        _secure_write_text(worktree, "vault/skills/escaped.md", "unsafe")

    assert raced
    assert not (moved / "escaped.md").exists()
    assert not (safe / "escaped.md").exists()
    assert list(moved.glob(".csi-*.tmp")) == []


def test_repeated_implement_preserves_uncommitted_reviewer_edits(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    first = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert first.ok is True
    target = Path(first.worktree) / "vault/skills/self-learning/SKILL.md"
    reviewed = target.read_text(encoding="utf-8") + "\nHuman reviewer note.\n"
    target.write_text(reviewed, encoding="utf-8")

    second = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert second.ok is True
    assert second.idempotent is True
    assert Path(second.worktree) == Path(first.worktree)
    assert target.read_text(encoding="utf-8") == reviewed
    assert (
        "Human reviewer note."
        in _git(Path(first.worktree), "diff", "--", "vault/skills/self-learning/SKILL.md").stdout
    )
    _git(Path(first.worktree), "add", "vault/skills/self-learning/SKILL.md")
    _git(Path(first.worktree), "commit", "-m", "human reviewer follow-up")
    reviewer_sha = _sha(Path(first.worktree))

    third = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert third.ok is False
    assert third.status == "INCIDENT"
    assert _sha(Path(first.worktree)) == reviewer_sha
    assert target.read_text(encoding="utf-8") == reviewed
    assert CsiStore(db).get_run(run_id)["status"] == "INCIDENT"


def test_publish_cas_preserves_reviewer_edit_created_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    original_write = implement_module._secure_write_text
    reviewer_body = "# Concurrent human reviewer edit\n"
    injected = False

    def _inject_before_publish(root, relative_path, body, **kwargs):
        nonlocal injected
        target = Path(root) / relative_path
        target.write_text(reviewer_body, encoding="utf-8")
        injected = True
        return original_write(root, relative_path, body, **kwargs)

    monkeypatch.setattr(implement_module, "_secure_write_text", _inject_before_publish)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert injected
    assert result.ok is False
    assert "reviewer_edits_present" in result.error
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    assert target.read_text(encoding="utf-8") == reviewer_body
    assert _sha(Path(result.worktree)) == _sha(repo)


def test_publish_exchange_restores_edit_created_at_rename_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    target = worktree / "vault" / "skills" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Original\n", encoding="utf-8")
    expected = implement_module._snapshot_destination(  # noqa: SLF001
        worktree,
        "vault/skills/SKILL.md",
    )
    reviewer_body = "# Reviewer changed this during publication\n"
    original_rename = implement_module._atomic_rename_at
    raced = False

    def _edit_then_exchange(dir_fd, source, destination, *, exchange):
        nonlocal raced
        if not raced:
            target.write_text(reviewer_body, encoding="utf-8")
            raced = True
        return original_rename(
            dir_fd,
            source,
            destination,
            exchange=exchange,
        )

    monkeypatch.setattr(implement_module, "_atomic_rename_at", _edit_then_exchange)

    with pytest.raises(RuntimeError, match="reviewer_edits_present"):
        _secure_write_text(
            worktree,
            "vault/skills/SKILL.md",
            "# Generated\n",
            expected_snapshot=expected,
        )

    assert raced
    assert target.read_text(encoding="utf-8") == reviewer_body
    assert list(target.parent.glob(".csi-*.tmp")) == []


def test_plan_apply_fence_rejects_run_state_evidence_and_sha_changes(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    # Run state
    state_repo = _repo_factory(tmp_path / "state")
    state_db = tmp_path / "state.db"
    state_run = _seed(state_db, state_repo)
    CsiStore(state_db).set_status(state_run, "CANCELLED")
    state_result = implement_approved(run_id=state_run, db_path=state_db, repo_root=state_repo)
    assert state_result.error == "invalid_run_state:CANCELLED"

    # Approved evidence bundle
    evidence_repo = _repo_factory(tmp_path / "evidence")
    evidence_db = tmp_path / "evidence.db"
    evidence_run = _seed(evidence_db, evidence_repo)
    with sqlite3.connect(evidence_db) as conn:
        conn.execute(
            "UPDATE csi_runs SET evidence_json=? WHERE id=?",
            (json.dumps({"codebase_sha": _sha(evidence_repo), "tampered": True}), evidence_run),
        )
    evidence_result = implement_approved(
        run_id=evidence_run,
        db_path=evidence_db,
        repo_root=evidence_repo,
    )
    assert evidence_result.error == "approval_evidence_changed"

    # Base/code SHA
    sha_repo = _repo_factory(tmp_path / "sha")
    sha_db = tmp_path / "sha.db"
    sha_run = _seed(sha_db, sha_repo)
    (sha_repo / "after-approval.txt").write_text("new base\n", encoding="utf-8")
    _git(sha_repo, "add", "after-approval.txt")
    _git(sha_repo, "commit", "-m", "advance base")
    sha_result = implement_approved(
        run_id=sha_run,
        db_path=sha_db,
        repo_root=sha_repo,
    )
    assert sha_result.error.startswith("codebase_sha_changed:")

    for repo, run_id in (
        (state_repo, state_run),
        (evidence_repo, evidence_run),
        (sha_repo, sha_run),
    ):
        assert not (repo / "var" / "csi" / "worktrees" / run_id).exists()


def test_store_mutators_cannot_override_active_implementation_claim(tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    observed = store.get_run(run_id)
    assert observed is not None
    original_implement_json = observed["implement_json"]
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=observed,
    )

    store.set_status(run_id, "CANCELLED")
    assert not store.set_approval(run_id, status="rejected")
    store.finish_run(run_id, status="CANCELLED", verdict="no_change")
    assert not store.patch_implement_json(run_id, {"unexpected": True})

    claimed = store.get_run(run_id)
    assert claimed is not None
    assert claimed["status"] == "IMPLEMENTING"
    assert claimed["approval_status"] == "approved"
    assert claimed["implement_json"] == original_implement_json


def test_conflict_revalidation_detects_reviewer_edit_after_approval(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    reviewer = tmp_path / "reviewer"
    _git(repo, "worktree", "add", "-b", "reviewer/change", str(reviewer), "HEAD")
    target = reviewer / "vault/skills/self-learning/SKILL.md"
    target.write_text("# Human is reviewing this path\n", encoding="utf-8")

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert "conflict_revalidation_failed" in result.error
    assert "reviewer_edits_overlap_proposal" in result.error
    assert target.read_text(encoding="utf-8") == "# Human is reviewing this path\n"
    assert not (repo / "var" / "csi" / "worktrees" / run_id).exists()


def test_evidence_is_revalidated_after_worktree_creation_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    original_git = implement_module._git
    tampered = False

    def _tamper_after_add(cwd, *args, check=True):
        nonlocal tampered
        result = original_git(cwd, *args, check=check)
        if not tampered and args[:2] == ("worktree", "add") and result.returncode == 0:
            tampered = True
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE csi_runs SET evidence_json=? WHERE id=?",
                    (json.dumps({"codebase_sha": _sha(repo), "late": "change"}), run_id),
                )
        return result

    monkeypatch.setattr(implement_module, "_git", _tamper_after_add)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert result.status == "INCIDENT"
    assert "approval_evidence_changed" in result.error
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    assert target.read_text(encoding="utf-8") == "# Original\n"
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_conflicts_are_revalidated_again_at_content_mutation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    reviewer = tmp_path / "late-reviewer"
    original_git = implement_module._git
    reviewer_created = False

    def _add_reviewer_after_csi_worktree(cwd, *args, check=True):
        nonlocal reviewer_created
        result = original_git(cwd, *args, check=check)
        if (
            not reviewer_created
            and args[:2] == ("worktree", "add")
            and "-b" in args
            and result.returncode == 0
        ):
            reviewer_created = True
            _git(
                repo,
                "worktree",
                "add",
                "-b",
                "reviewer/late-change",
                str(reviewer),
                "HEAD",
            )
            (reviewer / "vault/skills/self-learning/SKILL.md").write_text(
                "# Late reviewer edit\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(implement_module, "_git", _add_reviewer_after_csi_worktree)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert result.status == "INCIDENT"
    assert "conflict_changed_before_write" in result.error
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    assert target.read_text(encoding="utf-8") == "# Original\n"
    assert (reviewer / "vault/skills/self-learning/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Late reviewer edit\n"


def test_run_state_is_revalidated_inside_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    original_write = implement_module._secure_write_text

    def _cancel_before_publish(root, relative_path, body, **kwargs):
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE csi_runs SET status='CANCELLED' WHERE id=?", (run_id,))
        return original_write(root, relative_path, body, **kwargs)

    monkeypatch.setattr(implement_module, "_secure_write_text", _cancel_before_publish)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert result.status == "CANCELLED"
    assert "run_state_changed_before_mutation" in result.error
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    assert target.read_text(encoding="utf-8") == "# Original\n"
    assert _sha(Path(result.worktree)) == _sha(repo)


def test_code_sha_is_revalidated_inside_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    approved_sha = _sha(repo)
    original_write = implement_module._secure_write_text

    def _advance_main_before_publish(root, relative_path, body, **kwargs):
        (repo / "README.md").write_text("# Concurrent main revision\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "concurrent main revision")
        return original_write(root, relative_path, body, **kwargs)

    monkeypatch.setattr(implement_module, "_secure_write_text", _advance_main_before_publish)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert "codebase_sha_changed_before_mutation" in result.error
    assert _sha(repo) != approved_sha
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    assert target.read_text(encoding="utf-8") == "# Original\n"
    assert _sha(Path(result.worktree)) == approved_sha


def test_conflicts_are_revalidated_inside_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    reviewer = tmp_path / "publish-reviewer"
    original_write = implement_module._secure_write_text

    def _add_reviewer_before_publish(root, relative_path, body, **kwargs):
        _git(repo, "worktree", "add", "-b", "reviewer/publish", str(reviewer), "HEAD")
        (reviewer / relative_path).write_text("# Reviewer owns this path\n", encoding="utf-8")
        return original_write(root, relative_path, body, **kwargs)

    monkeypatch.setattr(implement_module, "_secure_write_text", _add_reviewer_before_publish)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert "conflict_changed_before_write" in result.error
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    assert target.read_text(encoding="utf-8") == "# Original\n"
    assert (reviewer / "vault/skills/self-learning/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Reviewer owns this path\n"


def test_state_is_revalidated_after_staging_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    base_sha = _sha(repo)
    original_git = implement_module._git
    cancelled = False

    def _cancel_after_add(cwd, *args, check=True):
        nonlocal cancelled
        result = original_git(cwd, *args, check=check)
        if not cancelled and args[:2] == ("add", "--"):
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE csi_runs SET status='CANCELLED' WHERE id=?", (run_id,))
            cancelled = True
        return result

    monkeypatch.setattr(implement_module, "_git", _cancel_after_add)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert cancelled
    assert result.ok is False
    assert result.status == "CANCELLED"
    assert "run_state_changed_before_mutation" in result.error
    assert _sha(Path(result.worktree)) == base_sha


def test_commit_boundary_locks_index_and_rejects_concurrent_reviewer_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    base_sha = _sha(repo)
    relative_path = "vault/skills/self-learning/SKILL.md"
    reviewer_body = "# Reviewer changed this at the commit boundary\n"
    original_git = implement_module._git
    attempted = False
    reviewer_add_returncode: int | None = None

    def _stage_at_commit_boundary(cwd, *args, check=True):
        nonlocal attempted, reviewer_add_returncode
        if not attempted and "commit-tree" in args:
            attempted = True
            target = Path(cwd) / relative_path
            target.write_text(reviewer_body, encoding="utf-8")
            add = original_git(cwd, "add", "--", relative_path, check=False)
            reviewer_add_returncode = add.returncode
        return original_git(cwd, *args, check=check)

    monkeypatch.setattr(implement_module, "_git", _stage_at_commit_boundary)

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert attempted
    assert reviewer_add_returncode != 0
    assert result.ok is False
    assert result.status == "INCIDENT"
    assert "reviewer_edits_present_before_ref_update" in result.error
    worktree = Path(result.worktree)
    assert (worktree / relative_path).read_text(encoding="utf-8") == reviewer_body
    assert _sha(worktree) == base_sha
    metadata = json.loads(CsiStore(db).get_run(run_id)["implement_json"])
    assert metadata["recovery_required"] is True
    assert set(metadata).intersection(_RESERVED_PROVENANCE_KEYS) == {"approval_binding"}
    dangling = _git(
        repo,
        "fsck",
        "--unreachable",
        "--no-reflogs",
        check=False,
    )
    assert dangling.returncode == 0
    generated_commits = [
        line.split()[2]
        for line in dangling.stdout.splitlines()
        if line.startswith("unreachable commit ")
    ]
    assert len(generated_commits) == 1
    generated_commit = generated_commits[0]
    committed_body = _git(
        worktree,
        "show",
        f"{generated_commit}:{relative_path}",
    ).stdout
    assert committed_body.startswith("# CSI Self-Learning note")
    assert committed_body != reviewer_body
    assert _git(
        worktree,
        "rev-list",
        "--parents",
        "-n",
        "1",
        generated_commit,
    ).stdout.split() == [generated_commit, base_sha]
    committed_tree = _git(
        worktree,
        "rev-parse",
        f"{generated_commit}^{{tree}}",
    ).stdout.strip()
    assert len(committed_tree) == 40
    git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()


def test_success_records_exact_committed_tree_and_payload_provenance(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    base_sha = _sha(repo)
    relative_path = "vault/skills/self-learning/SKILL.md"

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is True
    metadata = json.loads(CsiStore(db).get_run(run_id)["implement_json"])
    commit = metadata["implementation_commit"]
    tree = _git(Path(result.worktree), "rev-parse", f"{commit}^{{tree}}").stdout.strip()
    parent = _git(Path(result.worktree), "rev-parse", f"{commit}^").stdout.strip()
    committed_body = _git(
        Path(result.worktree),
        "show",
        f"{commit}:{relative_path}",
    ).stdout
    assert metadata["implementation_tree"] == tree
    assert metadata["implementation_parent"] == parent == base_sha
    assert metadata["committed_payload_sha256"] == {
        relative_path: hashlib.sha256(committed_body.encode("utf-8")).hexdigest()
    }
    assert len(metadata["staged_index_sha256"]) == 64
    assert _git(
        Path(result.worktree),
        "diff",
        "--name-only",
        base_sha,
        commit,
    ).stdout.splitlines() == [relative_path]


def test_finalization_does_not_overwrite_concurrent_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    original_finalize = CsiStore.finalize_implementation_claim

    def _cancel_before_finalize(self, target_run_id, **kwargs):
        self._conn.execute(  # noqa: SLF001 - deliberate concurrent-state simulation
            "UPDATE csi_runs SET status='CANCELLED' WHERE id=?",
            (target_run_id,),
        )
        self._conn.commit()  # noqa: SLF001
        return original_finalize(self, target_run_id, **kwargs)

    monkeypatch.setattr(
        CsiStore,
        "finalize_implementation_claim",
        _cancel_before_finalize,
    )

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok is False
    assert result.status == "CANCELLED"
    assert "implementation_finalization_claim_lost" in result.error
    row = CsiStore(db).get_run(run_id)
    assert row is not None and row["status"] == "CANCELLED"
    # The generated commit remains on its branch for explicit recovery.
    assert Path(result.worktree).exists()
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_ref_replacement_immediately_before_guard_cannot_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    base_sha = _sha(repo)
    original_guard = implement_module._hold_expected_branch_ref
    replaced = False

    @contextmanager
    def _replace_before_guard(root, *, branch, expected_commit):
        nonlocal replaced
        _git(
            root,
            "update-ref",
            f"refs/heads/{branch}",
            base_sha,
            expected_commit,
        )
        replaced = True
        with original_guard(
            root,
            branch=branch,
            expected_commit=expected_commit,
        ):
            yield

    monkeypatch.setattr(
        implement_module,
        "_hold_expected_branch_ref",
        _replace_before_guard,
    )

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert replaced
    assert result.ok is False
    assert result.status == "INCIDENT"
    assert "git_ref_guard_failed" in result.error
    row = CsiStore(db).get_run(run_id)
    assert row is not None
    assert row["status"] == "INCIDENT"
    assert row["status"] != "AWAITING_MERGE"
    assert _git(repo, "rev-parse", result.branch).stdout.strip() == base_sha


def test_ref_lock_blocks_replacement_during_database_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    base_sha = _sha(repo)
    original_finalize = CsiStore.finalize_implementation_claim
    replacement_returncode: int | None = None

    def _attempt_replacement_while_finalizing(self, target_run_id, **kwargs):
        nonlocal replacement_returncode
        metadata = kwargs["implement_json"]
        replacement = _git(
            repo,
            "update-ref",
            f"refs/heads/{metadata['branch']}",
            base_sha,
            metadata["implementation_commit"],
            check=False,
        )
        replacement_returncode = replacement.returncode
        return original_finalize(self, target_run_id, **kwargs)

    monkeypatch.setattr(
        CsiStore,
        "finalize_implementation_claim",
        _attempt_replacement_while_finalizing,
    )

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert replacement_returncode not in {None, 0}
    assert result.ok is True
    assert result.status == "AWAITING_MERGE"
    metadata = json.loads(CsiStore(db).get_run(run_id)["implement_json"])
    assert (
        _git(repo, "rev-parse", f"refs/heads/{result.branch}").stdout.strip()
        == metadata["implementation_commit"]
    )


def test_ref_drift_before_final_readiness_guard_becomes_incident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    base_sha = _sha(repo)
    original_validate = implement_module._validate_merge_readiness
    drifted = False

    def _drift_before_readiness_validation(**kwargs):
        nonlocal drifted
        row = CsiStore(db).get_run(run_id)
        metadata = json.loads(row["implement_json"])
        if not drifted and row["status"] == "AWAITING_MERGE":
            moved = _git(
                repo,
                "update-ref",
                f"refs/heads/{metadata['branch']}",
                base_sha,
                metadata["implementation_commit"],
                check=False,
            )
            assert moved.returncode == 0
            drifted = True
        return original_validate(**kwargs)

    monkeypatch.setattr(
        implement_module,
        "_validate_merge_readiness",
        _drift_before_readiness_validation,
    )

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert drifted
    assert result.ok is False
    assert result.status == "INCIDENT"
    assert "git_ref_guard_failed" in result.error
    assert CsiStore(db).get_run(run_id)["status"] == "INCIDENT"


def test_idempotent_ref_drift_invalidates_merge_readiness(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    row = CsiStore(db).get_run(run_id)
    metadata = json.loads(row["implement_json"])
    base_sha = metadata["base_sha"]
    moved = _git(
        repo,
        "update-ref",
        f"refs/heads/{result.branch}",
        base_sha,
        metadata["implementation_commit"],
        check=False,
    )
    assert moved.returncode == 0

    repeated = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert repeated.ok is False
    assert repeated.idempotent is False
    assert repeated.status == "INCIDENT"
    assert CsiStore(db).get_run(run_id)["status"] == "INCIDENT"


def test_merge_ready_metadata_is_immutable_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    original_current = CsiStore.merge_readiness_is_current
    mutation_refused = False

    def _attempt_metadata_mutation(self, target_run_id, **kwargs):
        nonlocal mutation_refused
        mutation_refused = not self.patch_implement_json(
            target_run_id,
            {"concurrent_patch": "must_not_land"},
        )
        return original_current(self, target_run_id, **kwargs)

    monkeypatch.setattr(
        CsiStore,
        "merge_readiness_is_current",
        _attempt_metadata_mutation,
    )

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert result.ok
    assert mutation_refused
    row = CsiStore(db).get_run(run_id)
    assert row["status"] == "AWAITING_MERGE"
    assert "concurrent_patch" not in json.loads(row["implement_json"])


def test_public_status_transition_requires_exact_merge_ready_provenance(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None and ready["status"] == "AWAITING_MERGE"
    exact_json = ready["implement_json"]
    exact_metadata = json.loads(exact_json)
    provenance = {
        key: exact_metadata[key]
        for key in (
            "approval_binding",
            "implementation_commit",
            "implementation_tree",
            "implementation_parent",
            "base_sha",
            "written_paths",
            "committed_payload_sha256",
        )
    }

    # A public transition that does not present the exact observed provenance
    # is refused rather than inferring that mutable readiness data is current.
    assert store.set_status(run_id, "MERGED") is False
    omitted = store.get_run(run_id)
    assert omitted is not None
    assert omitted["status"] == "AWAITING_MERGE"
    assert omitted["implement_json"] == exact_json

    tampered = dict(exact_metadata)
    tampered["implementation_commit"] = tampered["base_sha"]
    assert (
        store.set_status(
            run_id,
            "MERGED",
            implement_json=tampered,
        )
        is False
    )
    refused = store.get_run(run_id)
    assert refused is not None
    assert refused["status"] == "AWAITING_MERGE"
    assert refused["implement_json"] == exact_json
    assert not store.claim_implementation(
        run_id,
        expected_status="AWAITING_MERGE",
        observed=refused,
    )
    assert not store.claim_cleanup(
        run_id,
        expected_status="AWAITING_MERGE",
        observed_implement_json=exact_json,
    )
    store.finish_run(run_id, status="CANCELLED", verdict="no_change")
    assert not store.set_approval(run_id, status="rejected")
    assert not store.patch_implement_json(run_id, {"must_not_land": True})
    still_ready = store.get_run(run_id)
    assert still_ready is not None
    assert still_ready["status"] == "AWAITING_MERGE"
    assert still_ready["implement_json"] == exact_json

    _git(repo, "merge", "--ff-only", result.branch)
    assert (
        store.set_status(
            run_id,
            "MERGED",
            implement_json=exact_metadata,
            repo_root=repo,
        )
        is True
    )
    merged = store.get_run(run_id)
    assert merged is not None and merged["status"] == "MERGED"
    assert merged["implement_json"] == exact_json
    merged_metadata = json.loads(merged["implement_json"])
    assert {key: merged_metadata[key] for key in provenance} == provenance


@pytest.mark.parametrize("reserved_status", ["IMPLEMENTING", "CLEANING"])
def test_public_status_cannot_enter_reserved_claim_or_replace_provenance(
    tmp_path: Path,
    reserved_status: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_json = ready["implement_json"]
    exact_metadata = json.loads(exact_json)

    assert (
        store.set_status(
            run_id,
            reserved_status,
            implement_json=exact_metadata,
        )
        is False
    )
    assert (
        store.mark_implementation_incident(
            run_id,
            implement_json={"forged": "public-api-bypass"},
            error="must_not_land",
        )
        is False
    )
    after = store.get_run(run_id)
    assert after is not None
    assert after["status"] == "AWAITING_MERGE"
    assert after["implement_json"] == exact_json


def test_merge_terminal_requires_current_locked_ref_and_merged_commit(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_json = ready["implement_json"]
    exact_metadata = json.loads(exact_json)

    # Exact metadata alone cannot fabricate a merge before the implementation
    # commit is in the repository's current HEAD.
    assert (
        store.set_status(
            run_id,
            "MERGED",
            implement_json=exact_metadata,
            repo_root=repo,
        )
        is False
    )
    still_ready = store.get_run(run_id)
    assert still_ready is not None
    assert still_ready["status"] == "AWAITING_MERGE"
    assert still_ready["implement_json"] == exact_json

    # Even after the exact commit lands, moving its approved branch ref makes
    # the readiness proof stale and must never produce a MERGED status.
    _git(repo, "merge", "--ff-only", exact_metadata["implementation_commit"])
    assert (
        _git(
            repo,
            "update-ref",
            f"refs/heads/{result.branch}",
            exact_metadata["base_sha"],
            exact_metadata["implementation_commit"],
            check=False,
        ).returncode
        == 0
    )
    assert (
        store.set_status(
            run_id,
            "MERGED",
            implement_json=exact_metadata,
            repo_root=repo,
        )
        is False
    )
    after_drift = store.get_run(run_id)
    assert after_drift is not None
    assert after_drift["status"] != "MERGED"
    assert after_drift["implement_json"] == exact_json


def test_terminal_mutators_cannot_replace_approved_provenance(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_json = ready["implement_json"]
    exact_metadata = json.loads(exact_json)
    assert store.set_status(
        run_id,
        "REJECTED",
        implement_json=exact_metadata,
        repo_root=repo,
    )

    assert not store.patch_implement_json(
        run_id,
        {"implementation_commit": exact_metadata["base_sha"]},
    )
    forged = dict(exact_metadata)
    forged["implementation_tree"] = forged["base_sha"]
    assert not store.set_status(
        run_id,
        "CANCELLED",
        implement_json=forged,
    )
    claimed_json = store.claim_cleanup(
        run_id,
        expected_status="REJECTED",
        observed_implement_json=exact_json,
    )
    assert claimed_json is not None
    assert not store.release_cleanup_claim(
        run_id,
        restore_status="MERGED",
        observed_implement_json=claimed_json,
    )
    assert not store.finalize_cleanup_claim(
        run_id,
        restore_status="MERGED",
        observed_implement_json=claimed_json,
        implement_json=forged,
    )
    assert store.release_cleanup_claim(
        run_id,
        restore_status="REJECTED",
        observed_implement_json=claimed_json,
    )
    terminal = store.get_run(run_id)
    assert terminal is not None
    assert terminal["status"] == "REJECTED"
    assert terminal["implement_json"] == exact_json


def test_public_terminal_transition_preserves_complete_cleanup_journal(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    stale_precleanup_metadata = json.loads(ready["implement_json"])
    assert store.set_status(
        run_id,
        "REJECTED",
        implement_json=stale_precleanup_metadata,
        repo_root=repo,
    )
    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert cleaned.ok
    terminal = store.get_run(run_id)
    assert terminal is not None
    exact_postcleanup_json = terminal["implement_json"]
    exact_postcleanup_metadata = json.loads(exact_postcleanup_json)
    journal = exact_postcleanup_metadata["cleanup_quarantine"]
    tombstone = Path(journal["path"])
    assert tombstone.is_dir()
    assert not store.patch_implement_json(
        run_id,
        {"cleanup_quarantine": {"forged": True}},
    )

    # A caller retaining the formerly exact merge-ready object must not erase
    # the cleanup journal added after that observation.
    assert not store.set_status(
        run_id,
        "CANCELLED",
        implement_json=stale_precleanup_metadata,
    )
    unchanged = store.get_run(run_id)
    assert unchanged is not None
    assert unchanged["status"] == "REJECTED"
    assert unchanged["implement_json"] == exact_postcleanup_json
    type_changed = json.loads(exact_postcleanup_json)
    type_changed["cleanup_quarantine"]["device"] = float(
        type_changed["cleanup_quarantine"]["device"]
    )
    assert not store.set_status(
        run_id,
        "CANCELLED",
        implement_json=type_changed,
    )

    # The exact current object remains a legal disposition transition, and the
    # stored bytes (including the tombstone pointer) remain unchanged.
    restarted = CsiStore(db)
    assert restarted.set_status(
        run_id,
        "CANCELLED",
        implement_json=exact_postcleanup_metadata,
    )
    transitioned = restarted.get_run(run_id)
    assert transitioned is not None
    assert transitioned["status"] == "CANCELLED"
    assert transitioned["implement_json"] == exact_postcleanup_json
    assert Path(json.loads(transitioned["implement_json"])["cleanup_quarantine"]["path"]) == (
        tombstone
    )
    repeated = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert repeated.ok
    assert repeated.action == "retained"
    assert repeated.retained_reason == "worktree_quarantine_retained_for_safe_unlink"


def test_cleanup_finalizer_refuses_registered_worktree_and_arbitrary_result(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_metadata = json.loads(ready["implement_json"])
    assert store.set_status(
        run_id,
        "REJECTED",
        implement_json=exact_metadata,
        repo_root=repo,
    )
    terminal = store.get_run(run_id)
    assert terminal is not None
    claimed_json = store.claim_cleanup(
        run_id,
        expected_status="REJECTED",
        observed_implement_json=terminal["implement_json"],
    )
    assert claimed_json is not None
    false_result = json.loads(claimed_json)
    false_result.pop("cleanup_claim")
    false_result.update(
        {
            "cleanup_action": "nothing_to_clean",
            "cleanup_retained_reason": "",
        }
    )

    assert (
        store.journal_cleanup_quarantine(
            run_id,
            observed_implement_json=claimed_json,
            cleanup_quarantine={
                "path": str(Path(result.worktree).with_name(".csi-cleanup-forged.quarantine")),
                "source_path": result.worktree,
                "device": 1,
                "inode": 1,
                "state": "bound_retained_at_persist",
                "path_verification": "forged",
                "unlink_pending": True,
                "restore_status": "REJECTED",
            },
        )
        is None
    )
    assert not store.finalize_cleanup_claim(
        run_id,
        restore_status="REJECTED",
        observed_implement_json=claimed_json,
        implement_json=false_result,
    )
    assert not store.finalize_cleanup_claim(
        run_id,
        restore_status="REJECTED",
        observed_implement_json=claimed_json,
        implement_json=false_result,
        repo_root=repo,
    )
    arbitrary = dict(false_result)
    arbitrary["untyped_phase_result"] = True
    assert not store.finalize_cleanup_claim(
        run_id,
        restore_status="REJECTED",
        observed_implement_json=claimed_json,
        implement_json=arbitrary,
        repo_root=repo,
    )
    unchanged = store.get_run(run_id)
    assert unchanged is not None
    assert unchanged["status"] == "CLEANING"
    assert unchanged["implement_json"] == claimed_json
    assert Path(result.worktree).is_dir()
    assert (
        result.worktree
        in _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
        ).stdout
    )
    assert store.release_cleanup_claim(
        run_id,
        restore_status="REJECTED",
        observed_implement_json=claimed_json,
    )


def test_repeated_cleanup_preserves_legacy_bound_journal_shape(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    assert cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo).ok
    row = CsiStore(db).get_run(run_id)
    assert row is not None
    metadata = json.loads(row["implement_json"])
    journal = dict(metadata["cleanup_quarantine"])
    journal["state"] = "bound_empty_at_persist"
    journal.pop("source_path")
    journal.pop("registration")
    journal.pop("restore_status")
    metadata["cleanup_quarantine"] = journal
    legacy_json = json.dumps(metadata, sort_keys=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=?",
            (legacy_json, run_id),
        )

    repeated = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert repeated.ok
    assert repeated.action == "retained"
    after = CsiStore(db).get_run(run_id)
    assert after is not None and after["status"] == "REJECTED"
    after_journal = json.loads(after["implement_json"])["cleanup_quarantine"]
    assert after_journal["state"] == "bound_empty_at_persist"
    assert "source_path" not in after_journal
    assert "registration" not in after_journal
    assert "restore_status" not in after_journal
    assert Path(after_journal["path"]).is_dir()


def test_cleanup_finalizer_cannot_create_missing_reserved_provenance(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    assert store.set_status(run_id, "CANCELLED")
    terminal = store.get_run(run_id)
    assert terminal is not None
    terminal_json = terminal["implement_json"]
    claimed_json = store.claim_cleanup(
        run_id,
        expected_status="CANCELLED",
        observed_implement_json=terminal_json,
    )
    assert claimed_json is not None
    forged = json.loads(claimed_json)
    forged["implementation_commit"] = "a" * 40

    assert not store.finalize_cleanup_claim(
        run_id,
        restore_status="CANCELLED",
        observed_implement_json=claimed_json,
        implement_json=forged,
    )
    claimed = store.get_run(run_id)
    assert claimed is not None
    assert claimed["status"] == "CLEANING"
    assert claimed["implement_json"] == claimed_json
    assert store.release_cleanup_claim(
        run_id,
        restore_status="CANCELLED",
        observed_implement_json=claimed_json,
    )


def test_cleanup_incident_fallback_cas_preserves_contaminated_source(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    ready_meta = json.loads(ready["implement_json"])
    assert store.set_status(
        run_id,
        "REJECTED",
        implement_json=ready_meta,
        repo_root=repo,
    )
    terminal = store.get_run(run_id)
    assert terminal is not None
    claimed_json = store.claim_cleanup(
        run_id,
        expected_status="REJECTED",
        observed_implement_json=terminal["implement_json"],
    )
    assert claimed_json is not None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET approved_at=? WHERE id=?",
            ("2099-01-01T00:00:00Z", run_id),
        )

    assert not store.release_cleanup_claim(
        run_id,
        restore_status="REJECTED",
        observed_implement_json=claimed_json,
    )
    contaminated = store.get_run(run_id)
    assert contaminated is not None
    assert contaminated["status"] == "CLEANING"
    restarted = cleanup_implementation(
        run_id=run_id,
        db_path=db,
        repo_root=repo,
    )
    assert restarted.ok is False
    assert restarted.retained_reason == "cleanup_recovery_source_changed"
    fenced = store.get_run(run_id)
    assert fenced is not None
    assert fenced["status"] == "INCIDENT"
    assert fenced["approved_at"] == "2099-01-01T00:00:00Z"
    assert fenced["implement_json"] == claimed_json
    assert Path(result.worktree).is_dir()
    assert (
        result.worktree
        in _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
        ).stdout
    )


@pytest.mark.parametrize("replacement", ["rejected", "proposed", "none"])
def test_terminal_approval_mutator_cannot_erase_reviewer_provenance(
    tmp_path: Path,
    replacement: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_metadata = json.loads(ready["implement_json"])
    assert store.set_status(
        run_id,
        "REJECTED",
        implement_json=exact_metadata,
        repo_root=repo,
    )
    terminal = store.get_run(run_id)
    assert terminal is not None

    store.finish_run(
        run_id,
        status="AWAITING_HUMAN",
        verdict="propose",
        conflict={"safe_to_implement": False, "forged": True},
        approval_status="proposed",
    )
    assert not store.set_approval(run_id, status=replacement)

    after = store.get_run(run_id)
    assert after is not None
    for field_name in (
        "status",
        "approval_status",
        "approved_by",
        "approved_at",
        "codebase_sha",
        "evidence_json",
        "synthesis_json",
        "conflict_json",
        "implement_json",
    ):
        assert after[field_name] == terminal[field_name]


def test_approval_can_still_be_revoked_before_implementation_claim(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)

    assert store.set_approval(run_id, status="rejected")

    row = store.get_run(run_id)
    assert row is not None
    assert row["status"] == "AWAITING_HUMAN"
    assert row["approval_status"] == "rejected"
    assert row["approved_by"] is None
    assert row["approved_at"] is None
    assert "approval_binding" not in json.loads(row["implement_json"])


@pytest.mark.parametrize(
    ("source_status", "target_status"),
    [
        ("AWAITING_HUMAN", "DEFERRED"),
        ("DEFERRED", "AWAITING_HUMAN"),
    ],
)
@pytest.mark.parametrize("generic_mutator", ["patch", "set_status"])
@pytest.mark.parametrize("provenance_change", ["add", "replace", "delete"])
def test_generic_preimplementation_mutators_cannot_manufacture_provenance(
    tmp_path: Path,
    source_status: str,
    target_status: str,
    generic_mutator: str,
    provenance_change: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    if source_status == "DEFERRED":
        assert store.set_status(run_id, "DEFERRED")
    before = store.get_run(run_id)
    assert before is not None
    assert before["status"] == source_status
    forged = json.loads(before["implement_json"])
    if provenance_change == "add":
        patch = {"implementation_commit": "a" * 40}
        forged.update(patch)
    elif provenance_change == "replace":
        patch = {"approval_binding": {"forged": "replacement"}}
        forged.update(patch)
    else:
        patch = {"approval_binding": None}
        forged.pop("approval_binding")

    if generic_mutator == "patch":
        changed = store.patch_implement_json(run_id, patch)
    else:
        changed = store.set_status(
            run_id,
            target_status,
            implement_json=forged,
        )

    assert changed is False
    unchanged = store.get_run(run_id)
    assert unchanged is not None
    assert unchanged["status"] == source_status
    assert unchanged["implement_json"] == before["implement_json"]
    assert store.set_approval(run_id, status="rejected")
    revoked = store.get_run(run_id)
    assert revoked is not None
    assert revoked["approval_status"] == "rejected"
    assert revoked["approved_by"] is None
    assert revoked["approved_at"] is None
    assert "approval_binding" not in json.loads(revoked["implement_json"])
    assert "implementation_commit" not in json.loads(revoked["implement_json"])


@pytest.mark.parametrize("status", ["AWAITING_HUMAN", "DEFERRED"])
def test_legacy_preimplementation_provenance_cannot_wedge_revocation_or_claim(
    tmp_path: Path,
    status: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    if status == "DEFERRED":
        assert store.set_status(run_id, "DEFERRED")
    approved = store.get_run(run_id)
    assert approved is not None
    contaminated = json.loads(approved["implement_json"])
    contaminated["implementation_commit"] = "a" * 40
    contaminated_json = json.dumps(contaminated, sort_keys=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=?",
            (contaminated_json, run_id),
        )

    legacy = store.get_run(run_id)
    assert legacy is not None
    assert not store.claim_implementation(
        run_id,
        expected_status=status,
        observed=legacy,
    )
    assert store.set_approval(run_id, status="rejected")

    revoked = store.get_run(run_id)
    assert revoked is not None
    assert revoked["status"] == status
    assert revoked["approval_status"] == "rejected"
    assert revoked["approved_by"] is None
    assert revoked["approved_at"] is None
    retained = json.loads(revoked["implement_json"])
    assert "approval_binding" not in retained
    assert retained["implementation_commit"] == "a" * 40
    assert not store.set_approval(run_id, status="approved", approved_by="owner")


def test_implementation_claim_cannot_launder_a_stale_reviewer_observation(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    stale = store.get_run(run_id)
    assert stale is not None
    assert stale["approved_by"] == "owner"
    assert store.set_approval(run_id, status="approved", approved_by="reviewer-2")

    assert not store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=stale,
    )
    current = store.get_run(run_id)
    assert current is not None
    assert current["status"] == "AWAITING_HUMAN"
    assert current["approved_by"] == "reviewer-2"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET approved_at=? WHERE id=?",
            ("2099-01-01T00:00:00Z", run_id),
        )
    assert not store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=current,
    )
    current = store.get_run(run_id)
    assert current is not None
    assert current["approved_at"] == "2099-01-01T00:00:00Z"
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=current,
    )


_RESERVED_PROVENANCE_KEYS = {
    "approval_binding",
    "approved_by",
    "base_sha",
    "branch",
    "committed_payload_sha256",
    "implementation_commit",
    "implementation_parent",
    "implementation_tree",
    "staged_index_sha256",
    "worktree",
    "written_paths",
}


@pytest.mark.parametrize(
    "mutation",
    ["add_all", "replace", "delete", "unapproved_diagnostic", "wrong_type"],
)
def test_active_incident_api_cannot_manufacture_or_change_provenance(
    tmp_path: Path,
    mutation: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    approved = store.get_run(run_id)
    assert approved is not None
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=approved,
    )
    claimed = store.get_run(run_id)
    assert claimed is not None
    before_json = claimed["implement_json"]
    replacement = json.loads(before_json)
    if mutation == "add_all":
        replacement.update(
            {
                "approved_by": "mallory",
                "base_sha": "0" * 40,
                "branch": "forged",
                "committed_payload_sha256": {"outside": "1" * 64},
                "implementation_commit": "2" * 40,
                "implementation_parent": "3" * 40,
                "implementation_tree": "4" * 40,
                "staged_index_sha256": "5" * 64,
                "worktree": "/tmp/forged",
                "written_paths": ["outside"],
            }
        )
    elif mutation == "replace":
        replacement["approval_binding"] = {"forged": "replacement"}
    elif mutation == "delete":
        replacement.pop("approval_binding")
    elif mutation == "unapproved_diagnostic":
        replacement["forged_diagnostic"] = True
    else:
        replacement["recovery_required"] = "yes"

    assert not store.mark_implementation_incident(
        run_id,
        observed=claimed,
        implement_json=replacement,
        error="must_not_land",
    )
    unchanged = store.get_run(run_id)
    assert unchanged is not None
    assert unchanged["status"] == "IMPLEMENTING"
    assert unchanged["implement_json"] == before_json


def test_incident_diagnostic_is_append_only_and_survives_restart(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    approved = store.get_run(run_id)
    assert approved is not None
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=approved,
    )
    claimed = store.get_run(run_id)
    assert claimed is not None
    before = json.loads(claimed["implement_json"])
    incident = dict(before)
    incident["recovery_required"] = True

    assert store.mark_implementation_incident(
        run_id,
        observed=claimed,
        implement_json=incident,
        error="durable_failure",
    )
    store.close()
    restarted = CsiStore(db).get_run(run_id)
    assert restarted is not None
    assert restarted["status"] == "INCIDENT"
    after = json.loads(restarted["implement_json"])
    assert after["recovery_required"] is True
    assert {key: value for key, value in after.items() if key in _RESERVED_PROVENANCE_KEYS} == {
        key: value for key, value in before.items() if key in _RESERVED_PROVENANCE_KEYS
    }


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("approval_status", "rejected"),
        ("approved_by", "mallory"),
        ("approved_at", "2099-01-01T00:00:00Z"),
        ("codebase_sha", "0" * 40),
        ("evidence_json", '{"forged":true}'),
        ("synthesis_json", '{"forged":true}'),
        ("conflict_json", '{"forged":true}'),
    ],
)
def test_incident_api_rejects_stale_source_but_can_fence_current_row(
    tmp_path: Path,
    column: str,
    replacement: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    approved = store.get_run(run_id)
    assert approved is not None
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=approved,
    )
    claimed = store.get_run(run_id)
    assert claimed is not None
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"UPDATE csi_runs SET {column}=? WHERE id=?",  # noqa: S608
            (replacement, run_id),
        )

    stale_meta = json.loads(claimed["implement_json"])
    stale_meta["recovery_required"] = True
    assert not store.release_implementation_claim(
        run_id,
        restore_status="AWAITING_HUMAN",
        observed=claimed,
        error="stale_source",
    )
    assert not store.mark_implementation_incident(
        run_id,
        observed=claimed,
        implement_json=stale_meta,
        error="stale_source",
    )
    current = store.get_run(run_id)
    assert current is not None
    exact_meta = json.loads(current["implement_json"])
    exact_meta["recovery_required"] = True
    assert store.mark_implementation_incident(
        run_id,
        observed=current,
        implement_json=exact_meta,
        error="source_changed",
    )
    fenced = store.get_run(run_id)
    assert fenced is not None
    assert fenced["status"] == "INCIDENT"
    assert fenced[column] == replacement


def test_corrupt_active_metadata_is_fenced_without_rewriting_bytes(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    approved = store.get_run(run_id)
    assert approved is not None
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=approved,
    )
    corrupt_json = '{"approval_binding":'
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=?",
            (corrupt_json, run_id),
        )
    corrupt = store.get_run(run_id)
    assert corrupt is not None

    assert not store.mark_implementation_incident(
        run_id,
        observed=corrupt,
        implement_json={"recovery_required": True},
        error="must_not_rewrite",
    )
    assert store.mark_implementation_incident(
        run_id,
        observed=corrupt,
        error="corrupt_metadata",
    )
    fenced = store.get_run(run_id)
    assert fenced is not None
    assert fenced["status"] == "INCIDENT"
    assert fenced["implement_json"] == corrupt_json


@pytest.mark.parametrize(
    "legacy_metadata",
    [
        {},
        {"implementation_commit": "a" * 40},
        {"approval_binding": {}, "branch": "legacy-partial"},
    ],
)
def test_partial_active_metadata_is_retained_not_laundered(
    tmp_path: Path,
    legacy_metadata: dict[str, object],
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    store = CsiStore(db)
    approved = store.get_run(run_id)
    assert approved is not None
    assert store.claim_implementation(
        run_id,
        expected_status="AWAITING_HUMAN",
        observed=approved,
    )
    legacy_json = json.dumps(legacy_metadata, sort_keys=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=?",
            (legacy_json, run_id),
        )
    legacy = store.get_run(run_id)
    assert legacy is not None
    incident_metadata = dict(legacy_metadata)
    incident_metadata["recovery_required"] = True

    assert store.mark_implementation_incident(
        run_id,
        observed=legacy,
        implement_json=incident_metadata,
        error="legacy_partial_metadata",
    )
    fenced = store.get_run(run_id)
    assert fenced is not None
    assert fenced["status"] == "INCIDENT"
    retained = json.loads(fenced["implement_json"])
    assert {key: value for key, value in retained.items() if key in _RESERVED_PROVENANCE_KEYS} == {
        key: value for key, value in legacy_metadata.items() if key in _RESERVED_PROVENANCE_KEYS
    }


def test_stale_public_provenance_cannot_win_merge_ready_transition_cas(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_metadata = json.loads(ready["implement_json"])
    concurrently_changed = dict(exact_metadata)
    concurrently_changed["implementation_tree"] = concurrently_changed["base_sha"]
    changed_json = json.dumps(concurrently_changed, sort_keys=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=?",
            (changed_json, run_id),
        )

    assert (
        store.set_status(
            run_id,
            "MERGED",
            implement_json=exact_metadata,
        )
        is False
    )
    assert (
        store.invalidate_finalized_implementation(
            run_id,
            observed_implement_json=exact_metadata,
            error="stale_primary_transition",
        )
        is False
    )
    assert (
        store.force_implementation_incident(
            run_id,
            observed_implement_json=exact_metadata,
            error="stale_fallback_transition",
        )
        is False
    )
    after = store.get_run(run_id)
    assert after is not None
    assert after["status"] == "AWAITING_MERGE"
    assert after["implement_json"] == changed_json


def test_merge_ready_transition_cas_refuses_changed_approval_bundle(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_json = ready["implement_json"]
    exact_metadata = json.loads(exact_json)
    changed_evidence = json.loads(ready["evidence_json"])
    changed_evidence["concurrent_change"] = True
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE csi_runs SET evidence_json=? WHERE id=?",
            (json.dumps(changed_evidence, sort_keys=True), run_id),
        )

    assert (
        store.set_status(
            run_id,
            "MERGED",
            implement_json=exact_metadata,
        )
        is False
    )
    assert (
        store.invalidate_finalized_implementation(
            run_id,
            observed_implement_json=exact_json,
            error="approval_bundle_changed",
        )
        is False
    )
    assert (
        store.force_implementation_incident(
            run_id,
            observed_implement_json=exact_json,
            error="approval_bundle_changed",
        )
        is False
    )
    after = store.get_run(run_id)
    assert after is not None
    assert after["status"] == "AWAITING_MERGE"
    assert after["implement_json"] == exact_json
    repeated = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert repeated.ok is False
    assert repeated.status == "AWAITING_MERGE"
    assert "implementation_approval_binding_changed" in repeated.error
    assert "implementation_non_merge_ready_transition_failed" in repeated.error


def test_invalidation_cas_failure_uses_exact_provenance_incident_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    original_validate = implement_module._validate_merge_readiness
    original_invalidate = CsiStore.invalidate_finalized_implementation
    drifted = False
    invalidation_attempted = False

    def _drift_before_readiness_validation(**kwargs):
        nonlocal drifted
        row = CsiStore(db).get_run(run_id)
        metadata = json.loads(row["implement_json"])
        if not drifted and row["status"] == "AWAITING_MERGE":
            assert (
                _git(
                    repo,
                    "update-ref",
                    f"refs/heads/{metadata['branch']}",
                    metadata["base_sha"],
                    metadata["implementation_commit"],
                    check=False,
                ).returncode
                == 0
            )
            drifted = True
        return original_validate(**kwargs)

    def _lose_primary_invalidation(self, target_run_id, **kwargs):
        nonlocal invalidation_attempted
        invalidation_attempted = True
        del self, target_run_id, kwargs
        return False

    monkeypatch.setattr(
        implement_module,
        "_validate_merge_readiness",
        _drift_before_readiness_validation,
    )
    monkeypatch.setattr(
        CsiStore,
        "invalidate_finalized_implementation",
        _lose_primary_invalidation,
    )

    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert drifted
    assert invalidation_attempted
    assert result.ok is False
    assert result.status == "INCIDENT"
    assert CsiStore(db).get_run(run_id)["status"] == "INCIDENT"
    monkeypatch.setattr(
        CsiStore,
        "invalidate_finalized_implementation",
        original_invalidate,
    )


def test_offline_aliases_have_one_lineage_and_cannot_form_consensus() -> None:
    proposal = PlannerProposal(
        change="same deterministic answer",
        affected_paths=["vault/skills/self-learning/SKILL.md"],
        confidence=0.9,
    )

    def deterministic(_packet, name: str) -> PlannerPlan:
        return PlannerPlan(
            routine="self_learning",
            verdict="propose",
            proposals=[proposal],
            planner=name,
            lineage=f"self-claimed-{name}",
        )

    plans = RoutinePlanner(
        use_live=False,
        planner_fn=deterministic,
    ).run_panel(EvidencePacket(routine="self_learning", window_days=7), ["grok", "sol"])
    assert len({plan.lineage for plan in plans}) == 1

    result = PlanSynthesisService(min_panel_size=2).synthesize(plans)

    assert result.verdict == "insufficient_panel"
    assert result.panel_size == 1
    assert "independent_lineages" in result.reason


def test_live_panel_requires_trusted_execution_provenance() -> None:
    proposal = PlannerProposal(
        change="same deterministic answer",
        affected_paths=["vault/skills/self-learning/SKILL.md"],
        confidence=0.9,
    )

    def unverified(_packet, name: str) -> PlannerPlan:
        return PlannerPlan(
            routine="self_learning",
            verdict="propose",
            proposals=[proposal],
            planner=name,
            lineage=f"self-claimed-{name}",
        )

    plans = RoutinePlanner(
        use_live=True,
        planner_fn=unverified,
    ).run_panel(EvidencePacket(routine="self_learning", window_days=7), ["grok", "sol"])
    result = PlanSynthesisService(min_panel_size=2).synthesize(plans)

    assert all(plan.provenance_verified is False for plan in plans)
    assert all(plan.lineage == "" for plan in plans)
    assert result.verdict == "insufficient_panel"
    assert result.panel_size == 0


def test_live_panel_accepts_distinct_verified_provider_executions() -> None:
    proposal = PlannerProposal(
        change="verified proposal",
        affected_paths=["vault/skills/self-learning/SKILL.md"],
        confidence=0.9,
    )
    provider = {"grok": "xai", "sol": "openai"}

    def verified(_packet, name: str) -> PlannerExecution:
        lineage = provider[name]
        return PlannerExecution(
            plan=PlannerPlan(
                routine="self_learning",
                verdict="propose",
                proposals=[proposal],
                planner=name,
            ),
            provider=lineage,
            lineage=lineage,
            execution_id=f"request-{name}",
        )

    plans = RoutinePlanner(
        use_live=True,
        planner_fn=verified,
    ).run_panel(EvidencePacket(routine="self_learning", window_days=7), ["grok", "sol"])
    result = PlanSynthesisService(min_panel_size=2).synthesize(plans)

    assert {plan.lineage for plan in plans} == {"xai", "openai"}
    assert all(plan.provenance_verified for plan in plans)
    assert result.verdict == "propose"
    assert result.panel_size == 2


def test_rejected_proposal_lineage_cannot_supply_path_consensus() -> None:
    path = "vault/skills/self-learning/SKILL.md"
    strong = PlannerPlan(
        routine="self_learning",
        verdict="propose",
        planner="grok",
        lineage="xai",
        execution_id="request-grok",
        provenance_verified=True,
        proposals=[
            PlannerProposal(
                change="strong",
                affected_paths=[path],
                confidence=0.9,
            )
        ],
    )
    rejected = PlannerPlan(
        routine="self_learning",
        verdict="propose",
        planner="sol",
        lineage="openai",
        execution_id="request-sol",
        provenance_verified=True,
        proposals=[
            PlannerProposal(
                change="weak",
                affected_paths=[path],
                confidence=0.1,
            )
        ],
    )

    result = PlanSynthesisService(min_panel_size=2).synthesize([strong, rejected])

    assert result.verdict == "no_change"
    assert result.accepted_proposals == []
    assert any(item.get("reason") == "below_confidence_floor" for item in result.rejected_items)


def test_cleanup_retains_active_and_dirty_reviewer_state(tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok

    active = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert active.action == "retained"
    assert active.retained_reason == "active_or_recoverable_state:AWAITING_MERGE"
    target = Path(result.worktree) / "vault/skills/self-learning/SKILL.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nReviewer edit.\n",
        encoding="utf-8",
    )
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)

    dirty = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert dirty.action == "retained"
    assert dirty.retained_reason == "reviewer_edits_present"
    assert target.exists()
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_cleanup_retains_and_restart_recovers_clean_reviewer_followup(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    worktree = Path(result.worktree)
    target = worktree / "vault/skills/self-learning/SKILL.md"
    reviewer_body = target.read_text(encoding="utf-8") + "\nReviewer follow-up.\n"
    target.write_text(reviewer_body, encoding="utf-8")
    _git(worktree, "add", "vault/skills/self-learning/SKILL.md")
    _git(worktree, "commit", "-m", "reviewer follow-up")
    reviewer_sha = _sha(worktree)

    # The merge-readiness guard makes the drift non-authoritative, while
    # retaining the human-owned branch and worktree for terminal disposition.
    repeated = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert not repeated.ok
    assert repeated.status == "INCIDENT"
    store = CsiStore(db)
    assert store.set_status(run_id, "REJECTED")

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert cleaned.action == "retained"
    assert cleaned.retained_reason.startswith("reviewer_branch_sha_changed:")
    assert cleaned.error == ""
    assert worktree.is_dir()
    assert target.read_text(encoding="utf-8") == reviewer_body
    assert _sha(worktree) == reviewer_sha
    assert (
        str(worktree)
        in _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
        ).stdout
    )

    # A later restart can safely recreate a manually removed clean reviewer
    # worktree from its drifted branch without treating it as merge-ready.
    _git(repo, "worktree", "remove", str(worktree))
    assert not worktree.exists()
    recovered = recover_implementation(
        run_id=run_id,
        db_path=db,
        repo_root=repo,
    )
    assert recovered.ok
    assert recovered.status == "REJECTED"
    assert _sha(Path(recovered.worktree)) == reviewer_sha
    assert (Path(recovered.worktree) / "vault/skills/self-learning/SKILL.md").read_text(
        encoding="utf-8"
    ) == reviewer_body


def test_cleanup_holds_approved_branch_ref_at_destructive_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    exact_metadata = json.loads(ready["implement_json"])
    assert store.set_status(
        run_id,
        "REJECTED",
        implement_json=exact_metadata,
        repo_root=repo,
    )
    original_verify = implement_module._verify_stored_implementation_provenance
    replacement_returncode: int | None = None
    verification_calls = 0

    def _attempt_ref_drift_while_cleanup_is_guarded(root, metadata):
        nonlocal replacement_returncode, verification_calls
        verification_calls += 1
        if verification_calls == 2:
            replacement = _git(
                repo,
                "update-ref",
                f"refs/heads/{metadata['branch']}",
                metadata["base_sha"],
                metadata["implementation_commit"],
                check=False,
            )
            replacement_returncode = replacement.returncode
        return original_verify(root, metadata)

    monkeypatch.setattr(
        implement_module,
        "_verify_stored_implementation_provenance",
        _attempt_ref_drift_while_cleanup_is_guarded,
    )

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert replacement_returncode not in {None, 0}
    assert cleaned.ok
    assert cleaned.action == ("worktree_quarantined,worktree_registration_retired")
    assert not Path(result.worktree).exists()


@pytest.mark.parametrize("substitution", ["worktree", "parent"])
def test_cleanup_refuses_symlink_substitution_without_touching_reviewer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    expected_wt = Path(result.worktree)
    implementation_sha = _sha(expected_wt)
    outside_parent = tmp_path / "outside-review"
    outside_parent.mkdir()
    outside_wt = (
        outside_parent / run_id
        if substitution == "parent"
        else outside_parent / "reviewer-worktree"
    )
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        f"reviewer/{substitution}",
        str(outside_wt),
        "HEAD",
    )
    reviewer_artifact = outside_wt / "reviewer-artifact.txt"
    reviewer_artifact.write_text("preserve me\n", encoding="utf-8")
    reviewer_sha = _sha(outside_wt)

    if substitution == "worktree":
        preserved_csi = tmp_path / "preserved-csi-worktree"
        expected_wt.rename(preserved_csi)
        os.symlink(outside_wt, expected_wt, target_is_directory=True)
    else:
        preserved_parent = tmp_path / "preserved-csi-parent"
        expected_wt.parent.rename(preserved_parent)
        preserved_csi = preserved_parent / run_id
        os.symlink(outside_parent, expected_wt.parent, target_is_directory=True)

    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    original_git = implement_module._git
    destructive_calls: list[tuple[str, ...]] = []

    def _record_destructive_calls(cwd, *args, check=True):
        if args[:2] == ("worktree", "remove") or (args and args[0] == "clean"):
            destructive_calls.append(args)
        return original_git(cwd, *args, check=check)

    monkeypatch.setattr(implement_module, "_git", _record_destructive_calls)

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert cleaned.ok is False
    assert cleaned.action == "retained"
    assert cleaned.retained_reason == "worktree_symlink_or_identity_substitution"
    assert "cleanup_worktree_identity_refused" in cleaned.error
    assert destructive_calls == []
    assert expected_wt.is_symlink() or expected_wt.parent.is_symlink()
    assert preserved_csi.exists()
    assert _sha(preserved_csi) == implementation_sha
    assert reviewer_artifact.read_text(encoding="utf-8") == "preserve me\n"
    assert _sha(outside_wt) == reviewer_sha
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert _git(repo, "branch", "--list", f"reviewer/{substitution}").stdout.strip()
    assert CsiStore(db).get_run(run_id)["status"] == "REJECTED"


def test_cleanup_rechecks_worktree_identity_after_reviewer_state_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    expected_wt = Path(result.worktree)
    original_sha = _sha(expected_wt)
    reviewer_wt = tmp_path / "reviewer-race"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "reviewer/cleanup-race",
        str(reviewer_wt),
        "HEAD",
    )
    reviewer_artifact = reviewer_wt / "reviewer-artifact.txt"
    reviewer_artifact.write_text("preserve race artifact\n", encoding="utf-8")
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)

    original_git = implement_module._git
    preserved_csi = tmp_path / "preserved-csi-race"
    swapped = False
    remove_called = False

    def _swap_after_status(cwd, *args, check=True):
        nonlocal swapped, remove_called
        proc = original_git(cwd, *args, check=check)
        if not swapped and Path(cwd) == expected_wt and args[:2] == ("status", "--porcelain=v1"):
            expected_wt.rename(preserved_csi)
            os.symlink(reviewer_wt, expected_wt, target_is_directory=True)
            swapped = True
        if args[:2] == ("worktree", "remove"):
            remove_called = True
        return proc

    monkeypatch.setattr(implement_module, "_git", _swap_after_status)

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert swapped
    assert cleaned.ok is False
    assert cleaned.retained_reason == "worktree_symlink_or_identity_substitution"
    assert remove_called is False
    assert preserved_csi.exists()
    assert _sha(preserved_csi) == original_sha
    assert reviewer_artifact.read_text(encoding="utf-8") == "preserve race artifact\n"
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert CsiStore(db).get_run(run_id)["status"] == "REJECTED"


def test_cleanup_rechecks_source_identity_after_rename_intent_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    expected_wt = Path(result.worktree)
    preserved_original = expected_wt.with_name("preserved-original")
    reviewer_wt = tmp_path / "reviewer-substitution"
    reviewer_wt.mkdir()
    reviewer_artifact = reviewer_wt / "reviewer-artifact.txt"
    reviewer_artifact.write_text("do not move\n", encoding="utf-8")
    original_journal = CsiStore.journal_cleanup_quarantine
    swapped = False

    def _swap_after_intent(self, target_run_id, **kwargs):
        nonlocal swapped
        updated = original_journal(self, target_run_id, **kwargs)
        if (
            updated is not None
            and not swapped
            and kwargs["cleanup_quarantine"]["state"] == "rename_intent_bound"
        ):
            expected_wt.rename(preserved_original)
            reviewer_wt.rename(expected_wt)
            swapped = True
        return updated

    monkeypatch.setattr(
        CsiStore,
        "journal_cleanup_quarantine",
        _swap_after_intent,
    )

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert swapped
    assert cleaned.ok is False
    assert "cleanup_worktree_identity_changed_before_rename" in cleaned.error
    substituted_artifact = expected_wt / reviewer_artifact.name
    assert substituted_artifact.read_text(encoding="utf-8") == "do not move\n"
    assert preserved_original.is_dir()
    row = CsiStore(db).get_run(run_id)
    journal = json.loads(row["implement_json"])["cleanup_quarantine"]
    assert journal["state"] == "rename_intent_bound"
    assert row["status"] == "REJECTED"
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_cleanup_quarantine_binds_last_validation_to_exact_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    expected_wt = Path(result.worktree)
    original_sha = _sha(expected_wt)
    reviewer_wt = tmp_path / "reviewer-last-boundary"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "reviewer/last-boundary",
        str(reviewer_wt),
        "HEAD",
    )
    reviewer_artifact = reviewer_wt / "reviewer-artifact.txt"
    reviewer_artifact.write_text("preserve last-boundary artifact\n", encoding="utf-8")
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)

    original_retain_tombstone = implement_module._retain_bound_directory_tombstone
    original_git = implement_module._git
    preserved_csi = tmp_path / "preserved-csi-last-boundary"
    swapped = False
    destructive_git_calls: list[tuple[str, ...]] = []

    def _substitute_after_last_identity_check(*args, **kwargs):
        nonlocal swapped
        tombstone = original_retain_tombstone(*args, **kwargs)
        quarantine = tombstone.path
        quarantine.rename(preserved_csi)
        reviewer_wt.rename(quarantine)
        swapped = True
        return tombstone

    def _record_destructive_git(cwd, *args, check=True):
        if args[:2] in {("worktree", "prune"), ("branch", "-d")}:
            destructive_git_calls.append(args)
        return original_git(cwd, *args, check=check)

    monkeypatch.setattr(
        implement_module,
        "_retain_bound_directory_tombstone",
        _substitute_after_last_identity_check,
    )
    monkeypatch.setattr(implement_module, "_git", _record_destructive_git)

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert swapped
    assert cleaned.ok is False
    assert cleaned.action == "retained"
    assert cleaned.retained_reason == (
        "worktree_quarantine_path_mismatch:identity_mismatch_after_persist"
    )
    assert not expected_wt.exists()
    assert preserved_csi.exists()
    assert list(preserved_csi.iterdir()) == []
    metadata = json.loads(CsiStore(db).get_run(run_id)["implement_json"])
    quarantine = Path(metadata["cleanup_quarantine"]["path"])
    preserved_identity = os.stat(preserved_csi, follow_symlinks=False)
    current_path_identity = os.stat(quarantine, follow_symlinks=False)
    assert metadata["cleanup_quarantine"]["state"] == "bound_retained_at_persist"
    assert metadata["cleanup_quarantine"]["path_verification"] == (
        "identity_mismatch_after_persist"
    )
    assert metadata["cleanup_quarantine"]["unlink_pending"] is True
    assert (
        metadata["cleanup_quarantine"]["device"],
        metadata["cleanup_quarantine"]["inode"],
    ) == (preserved_identity.st_dev, preserved_identity.st_ino)
    assert (
        metadata["cleanup_quarantine"]["device"],
        metadata["cleanup_quarantine"]["inode"],
    ) != (current_path_identity.st_dev, current_path_identity.st_ino)
    assert (quarantine / reviewer_artifact.name).read_text(encoding="utf-8") == (
        "preserve last-boundary artifact\n"
    )
    assert _git(repo, "rev-parse", result.branch).stdout.strip() == original_sha
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert _git(repo, "branch", "--list", "reviewer/last-boundary").stdout.strip()
    assert CsiStore(db).get_run(run_id)["status"] == "REJECTED"
    assert destructive_git_calls == []

    repeated = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert repeated.ok is False
    assert repeated.action == "retained"
    assert repeated.retained_reason == (
        "worktree_quarantine_path_mismatch:identity_mismatch_at_current_observation"
    )
    assert (quarantine / reviewer_artifact.name).read_text(encoding="utf-8") == (
        "preserve last-boundary artifact\n"
    )
    assert destructive_git_calls == []


def test_cleanup_preserves_file_added_through_preopened_directory_after_bound_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    store = CsiStore(db)
    _preserve_meta_status(store, run_id, "REJECTED", repo_root=repo)
    reviewer_directory_fd = os.open(
        result.worktree,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    original_journal = CsiStore.journal_cleanup_quarantine
    added = False

    def _add_after_quarantine_is_durably_bound(self, target_run_id, **kwargs):
        nonlocal added
        updated = original_journal(self, target_run_id, **kwargs)
        if (
            updated is not None
            and not added
            and kwargs["cleanup_quarantine"]["state"] == "quarantined_bound"
        ):
            artifact_fd = os.open(
                "late-reviewer-artifact.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=reviewer_directory_fd,
            )
            try:
                os.write(artifact_fd, b"preserve after durable bound journal\n")
            finally:
                os.close(artifact_fd)
            added = True
        return updated

    monkeypatch.setattr(
        CsiStore,
        "journal_cleanup_quarantine",
        _add_after_quarantine_is_durably_bound,
    )
    try:
        cleaned = cleanup_implementation(
            run_id=run_id,
            db_path=db,
            repo_root=repo,
        )
    finally:
        os.close(reviewer_directory_fd)

    assert added
    assert cleaned.ok
    row = CsiStore(db).get_run(run_id)
    assert row is not None and row["status"] == "REJECTED"
    journal = json.loads(row["implement_json"])["cleanup_quarantine"]
    quarantine = Path(journal["path"])
    artifact = quarantine / "late-reviewer-artifact.txt"
    assert artifact.read_text(encoding="utf-8") == ("preserve after durable bound journal\n")
    assert (
        result.worktree
        not in _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
        ).stdout
    )

    monkeypatch.undo()
    recovered = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert recovered.ok
    assert recovered.action == "retained"
    assert recovered.retained_reason == ("worktree_quarantine_reviewer_content_retained")
    assert artifact.read_text(encoding="utf-8") == ("preserve after durable bound journal\n")


def test_cleanup_restart_preserves_late_bound_content_and_path_substitute(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    reviewer_directory_fd = os.open(
        result.worktree,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    child = os.fork()
    if child == 0:
        original_journal = CsiStore.journal_cleanup_quarantine

        def _exit_after_late_bound_write(self, target_run_id, **kwargs):
            updated = original_journal(self, target_run_id, **kwargs)
            if updated is not None and kwargs["cleanup_quarantine"]["state"] == "quarantined_bound":
                artifact_fd = os.open(
                    "late-before-restart.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=reviewer_directory_fd,
                )
                os.write(artifact_fd, b"preserve original inode\n")
                os.close(artifact_fd)
                os._exit(88)
            return updated

        CsiStore.journal_cleanup_quarantine = _exit_after_late_bound_write
        cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
        os._exit(99)

    _, wait_status = os.waitpid(child, 0)
    os.close(reviewer_directory_fd)
    assert os.waitstatus_to_exitcode(wait_status) == 88
    interrupted = CsiStore(db).get_run(run_id)
    assert interrupted is not None and interrupted["status"] == "CLEANING"
    journal = json.loads(interrupted["implement_json"])["cleanup_quarantine"]
    quarantine = Path(journal["path"])
    preserved_bound_inode = tmp_path / "preserved-bound-inode"
    quarantine.rename(preserved_bound_inode)
    quarantine.mkdir()
    substitute_artifact = quarantine / "substitute-reviewer-artifact.txt"
    substitute_artifact.write_text("preserve substitute\n", encoding="utf-8")

    recovered = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert recovered.ok is False
    assert recovered.action == "retained"
    assert recovered.retained_reason.startswith("interrupted_cleanup_path_mismatch:")
    assert (preserved_bound_inode / "late-before-restart.txt").read_text(
        encoding="utf-8"
    ) == "preserve original inode\n"
    assert substitute_artifact.read_text(encoding="utf-8") == "preserve substitute\n"
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert (
        result.worktree
        not in _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
        ).stdout
    )
    proposal = PlannerProposal(
        change="post-crash safe CSI proposal",
        affected_paths=["vault/skills/self-learning/SKILL.md"],
        baseline="one",
        target="two",
        measured_by="test",
        rollback="revert",
        confidence=0.9,
    )
    assert (
        ConflictForecastService(repo_root=repo)
        .forecast(
            [proposal],
            base_sha=_sha(repo),
        )
        .safe_to_implement
    )


def test_cleanup_retains_tombstone_registration_and_branch_for_reconciliation(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    implementation_sha = _sha(Path(result.worktree))

    # A missing clean worktree is recoverable from the retained branch.
    _git(repo, "worktree", "remove", result.worktree)
    recovered = recover_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert recovered.ok
    assert Path(recovered.worktree).exists()
    assert _sha(Path(recovered.worktree)) == implementation_sha

    # Terminal cleanup clears only the bound worktree, retains its exact
    # tombstone, and retires only that worktree's Git administrative record.
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    retained = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert retained.action == ("worktree_quarantined,worktree_registration_retired")
    assert retained.retained_reason == "worktree_quarantine_retained_for_safe_unlink"
    metadata = json.loads(CsiStore(db).get_run(run_id)["implement_json"])
    tombstone = Path(metadata["cleanup_quarantine"]["path"])
    assert tombstone.is_dir()
    assert list(tombstone.iterdir()) == []
    assert metadata["cleanup_quarantine"]["state"] == "bound_retained_at_persist"
    assert metadata["cleanup_quarantine"]["path_verification"] == (
        "matched_bound_identity_after_persist"
    )
    assert metadata["cleanup_quarantine"]["unlink_pending"] is True
    tombstone_identity = os.stat(tombstone, follow_symlinks=False)
    assert (
        metadata["cleanup_quarantine"]["device"],
        metadata["cleanup_quarantine"]["inode"],
    ) == (tombstone_identity.st_dev, tombstone_identity.st_ino)
    assert not Path(result.worktree).exists()
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert (
        result.worktree
        not in _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
        ).stdout
    )
    registration = metadata["cleanup_quarantine"]["registration"]
    assert registration["state"] == "retired_bound"
    retired_admin = Path(registration["retired_path"])
    assert retired_admin.is_dir()
    retired_identity = os.stat(retired_admin, follow_symlinks=False)
    assert (registration["device"], registration["inode"]) == (
        retired_identity.st_dev,
        retired_identity.st_ino,
    )

    # Automated recovery refuses to recreate a terminally cleaned worktree.
    recovered_again = recover_implementation(
        run_id=run_id,
        db_path=db,
        repo_root=repo,
    )
    assert recovered_again.ok is False
    assert recovered_again.error == "cleanup_quarantine_retained"

    # Repeated cleanup remains idempotently retained, even after merge.
    repeated = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert repeated.ok is True
    assert repeated.action == "retained"
    assert repeated.retained_reason == "worktree_quarantine_retained_for_safe_unlink"
    _git(repo, "merge", "--ff-only", result.branch)
    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert cleaned.action == "retained"
    assert cleaned.retained_reason == "worktree_quarantine_retained_for_safe_unlink"
    assert not Path(result.worktree).exists()
    assert tombstone.is_dir()
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_normal_terminal_status_preserves_metadata_for_cleanup(tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _git(repo, "merge", "--ff-only", result.branch)

    store = CsiStore(db)
    ready = store.get_run(run_id)
    assert ready is not None
    assert store.set_status(
        run_id,
        "MERGED",
        implement_json=json.loads(ready["implement_json"]),
        repo_root=repo,
    )
    row = store.get_run(run_id)
    assert row is not None
    metadata = json.loads(row["implement_json"])
    assert metadata["branch"] == result.branch
    assert metadata["worktree"] == result.worktree

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert cleaned.ok
    assert cleaned.action == ("worktree_quarantined,worktree_registration_retired")
    assert cleaned.retained_reason == "worktree_quarantine_retained_for_safe_unlink"
    assert not Path(result.worktree).exists()
    row = store.get_run(run_id)
    assert row is not None
    metadata = json.loads(row["implement_json"])
    tombstone = Path(metadata["cleanup_quarantine"]["path"])
    assert tombstone.is_dir()
    assert list(tombstone.iterdir()) == []
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    proposal = PlannerProposal(
        change="future safe CSI proposal",
        affected_paths=["vault/skills/self-learning/SKILL.md"],
        baseline="one",
        target="two",
        measured_by="test",
        rollback="revert",
        confidence=0.9,
    )
    forecast = ConflictForecastService(repo_root=repo).forecast(
        [proposal],
        base_sha=_sha(repo),
    )
    assert forecast.safe_to_implement is True
    assert result.worktree not in forecast.active_user_worktrees


def test_cleanup_tombstone_record_failure_retains_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    original_git = implement_module._git
    destructive_git_calls: list[tuple[str, ...]] = []

    def _fail_tombstone_record(*args, **kwargs):
        implement_module._directory_entry_identity(args[0], args[1])
        raise RuntimeError("forced_tombstone_record_failure")

    def _record_destructive_git(cwd, *args, check=True):
        if args[:2] in {("worktree", "prune"), ("branch", "-d")}:
            destructive_git_calls.append(args)
        return original_git(cwd, *args, check=check)

    monkeypatch.setattr(
        implement_module,
        "_retain_bound_directory_tombstone",
        _fail_tombstone_record,
    )
    monkeypatch.setattr(implement_module, "_git", _record_destructive_git)

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert cleaned.ok is False
    assert cleaned.action == "retained"
    assert cleaned.retained_reason == "worktree_symlink_or_identity_substitution"
    assert "forced_tombstone_record_failure" in cleaned.error
    row = CsiStore(db).get_run(run_id)
    metadata = json.loads(row["implement_json"])
    journal = metadata["cleanup_quarantine"]
    tombstone = Path(journal["path"])
    assert not Path(result.worktree).exists()
    assert tombstone.is_dir()
    assert list(tombstone.iterdir()) == []
    assert journal["state"] == "quarantined_bound"
    identity = os.stat(tombstone, follow_symlinks=False)
    assert (journal["device"], journal["inode"]) == (
        identity.st_dev,
        identity.st_ino,
    )
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert CsiStore(db).get_run(run_id)["status"] == "REJECTED"
    assert destructive_git_calls == []


def test_cleanup_claim_loss_after_quarantine_keeps_durable_inode_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    original_journal = CsiStore.journal_cleanup_quarantine
    original_git = implement_module._git
    claim_lost = False
    destructive_git_calls: list[tuple[str, ...]] = []

    def _lose_claim_after_durable_journal(self, target_run_id, **kwargs):
        nonlocal claim_lost
        updated = original_journal(self, target_run_id, **kwargs)
        if (
            updated is not None
            and not claim_lost
            and kwargs["cleanup_quarantine"]["state"] == "bound_retained_at_persist"
        ):
            self._conn.execute(  # noqa: SLF001 - deliberate claim-loss simulation
                "UPDATE csi_runs SET status='CANCELLED' WHERE id=?",
                (target_run_id,),
            )
            self._conn.commit()  # noqa: SLF001
            claim_lost = True
        return updated

    def _record_destructive_git(cwd, *args, check=True):
        if args[:2] in {("worktree", "prune"), ("branch", "-d")}:
            destructive_git_calls.append(args)
        return original_git(cwd, *args, check=check)

    monkeypatch.setattr(
        CsiStore,
        "journal_cleanup_quarantine",
        _lose_claim_after_durable_journal,
    )
    monkeypatch.setattr(implement_module, "_git", _record_destructive_git)

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert claim_lost
    assert cleaned.ok is False
    assert cleaned.retained_reason == "cleanup_claim_lost:CANCELLED"
    row = CsiStore(db).get_run(run_id)
    assert row is not None and row["status"] == "CANCELLED"
    metadata = json.loads(row["implement_json"])
    journal = metadata["cleanup_quarantine"]
    tombstone = Path(journal["path"])
    identity = os.stat(tombstone, follow_symlinks=False)
    assert journal["state"] == "bound_retained_at_persist"
    assert journal["path_verification"] == "pending_after_bound_persist"
    assert journal["unlink_pending"] is True
    assert (journal["device"], journal["inode"]) == (
        identity.st_dev,
        identity.st_ino,
    )
    assert tombstone.is_dir()
    assert list(tombstone.iterdir()) == []
    assert not Path(result.worktree).exists()
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert destructive_git_calls == []

    repeated = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    assert repeated.ok is False
    assert repeated.action == "retained"
    assert repeated.retained_reason == "cleanup_claim_lost:CANCELLED"
    repeated_metadata = json.loads(CsiStore(db).get_run(run_id)["implement_json"])
    assert repeated_metadata["cleanup_quarantine"]["path_verification"] == (
        "pending_after_bound_persist"
    )
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    assert destructive_git_calls == []


@pytest.mark.parametrize(
    "crash_phase",
    [
        "before_rename",
        "after_rename",
        "before_clear",
        "after_clear",
        "after_empty_journal",
    ],
)
def test_cleanup_crash_phases_restart_from_durable_bound_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    before_cleanup = CsiStore(db).get_run(run_id)
    assert before_cleanup is not None
    stale_precleanup_metadata = json.loads(before_cleanup["implement_json"])
    worktree = Path(result.worktree)

    if crash_phase == "before_rename":
        original_rename = implement_module._atomic_rename_at

        def _crash_before_cleanup_rename(dir_fd, source, destination, *, exchange):
            if destination.startswith(".csi-cleanup-"):
                raise SystemExit("crash_before_cleanup_rename")
            return original_rename(
                dir_fd,
                source,
                destination,
                exchange=exchange,
            )

        monkeypatch.setattr(
            implement_module,
            "_atomic_rename_at",
            _crash_before_cleanup_rename,
        )
    elif crash_phase in {"after_rename", "after_clear", "after_empty_journal"}:
        original_journal = CsiStore.journal_cleanup_quarantine
        crash_state = (
            "quarantined_bound" if crash_phase == "after_rename" else "bound_retained_at_persist"
        )

        def _crash_at_journal(self, target_run_id, **kwargs):
            state = kwargs["cleanup_quarantine"]["state"]
            if state == crash_state:
                if crash_phase == "after_empty_journal":
                    original_journal(self, target_run_id, **kwargs)
                raise SystemExit(f"crash_at_{crash_phase}")
            return original_journal(self, target_run_id, **kwargs)

        monkeypatch.setattr(
            CsiStore,
            "journal_cleanup_quarantine",
            _crash_at_journal,
        )
    else:

        @contextmanager
        def _crash_before_clear(*args, **kwargs):
            del args, kwargs
            raise SystemExit("crash_before_clear")
            yield  # pragma: no cover - contextmanager shape only

        monkeypatch.setattr(
            implement_module,
            "_clear_bound_directory_tree",
            _crash_before_clear,
        )

    with pytest.raises(SystemExit):
        cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    crashed_row = CsiStore(db).get_run(run_id)
    assert crashed_row is not None and crashed_row["status"] == "CLEANING"
    crashed_meta = json.loads(crashed_row["implement_json"])
    journal = crashed_meta["cleanup_quarantine"]
    assert journal["restore_status"] == "REJECTED"
    assert journal["source_path"] == str(worktree)
    assert journal["state"] in {
        "rename_intent_bound",
        "quarantined_bound",
        "bound_retained_at_persist",
    }

    quarantine = Path(journal["path"])
    if quarantine.exists():
        identity = os.stat(quarantine, follow_symlinks=False)
        assert (journal["device"], journal["inode"]) == (
            identity.st_dev,
            identity.st_ino,
        )
    else:
        identity = os.stat(worktree, follow_symlinks=False)
        assert journal["state"] == "rename_intent_bound"
        assert (journal["device"], journal["inode"]) == (
            identity.st_dev,
            identity.st_ino,
        )

    monkeypatch.undo()
    reviewer_artifact: Path | None = None
    if not worktree.exists():
        worktree.mkdir()
        reviewer_artifact = worktree / "reviewer-artifact.txt"
        reviewer_artifact.write_text("preserve me\n", encoding="utf-8")

    recovered = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert recovered.action == "retained"
    assert recovered.retained_reason.startswith("interrupted_cleanup_reconciled:")
    restarted_store = CsiStore(db)
    recovered_row = restarted_store.get_run(run_id)
    assert recovered_row is not None and recovered_row["status"] == "REJECTED"
    recovered_json = recovered_row["implement_json"]
    recovered_metadata = json.loads(recovered_json)
    assert "cleanup_quarantine" in recovered_metadata
    assert not restarted_store.set_status(
        run_id,
        "CANCELLED",
        implement_json=stale_precleanup_metadata,
    )
    assert restarted_store.set_status(
        run_id,
        "CANCELLED",
        implement_json=recovered_metadata,
    )
    after_transition = restarted_store.get_run(run_id)
    assert after_transition is not None
    assert after_transition["implement_json"] == recovered_json
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()
    if reviewer_artifact is not None:
        assert reviewer_artifact.read_text(encoding="utf-8") == "preserve me\n"


def test_cleanup_hard_exit_after_rename_has_restartable_exact_pointer(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    worktree = Path(result.worktree)
    child = os.fork()
    if child == 0:
        original_journal = CsiStore.journal_cleanup_quarantine

        def _hard_exit_after_rename(self, target_run_id, **kwargs):
            if kwargs["cleanup_quarantine"]["state"] == "quarantined_bound":
                os._exit(88)
            return original_journal(self, target_run_id, **kwargs)

        CsiStore.journal_cleanup_quarantine = _hard_exit_after_rename
        cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
        os._exit(99)

    _, wait_status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(wait_status) == 88
    interrupted = CsiStore(db).get_run(run_id)
    assert interrupted is not None and interrupted["status"] == "CLEANING"
    journal = json.loads(interrupted["implement_json"])["cleanup_quarantine"]
    assert journal["state"] == "rename_intent_bound"
    quarantine = Path(journal["path"])
    identity = os.stat(quarantine, follow_symlinks=False)
    assert (journal["device"], journal["inode"]) == (
        identity.st_dev,
        identity.st_ino,
    )
    assert not worktree.exists()

    worktree.mkdir()
    reviewer_artifact = worktree / "reviewer-artifact.txt"
    reviewer_artifact.write_text("preserve hard-exit replacement\n", encoding="utf-8")
    recovered = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert recovered.ok
    assert recovered.retained_reason.startswith("interrupted_cleanup_reconciled:")
    assert CsiStore(db).get_run(run_id)["status"] == "REJECTED"
    assert reviewer_artifact.read_text(encoding="utf-8") == "preserve hard-exit replacement\n"
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


@pytest.mark.parametrize(
    "failed_state",
    [
        "rename_intent_bound",
        "quarantined_bound",
        "bound_retained_at_persist",
    ],
)
def test_cleanup_journal_failure_never_creates_unaddressable_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_state: str,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _preserve_meta_status(CsiStore(db), run_id, "REJECTED", repo_root=repo)
    worktree = Path(result.worktree)
    original_journal = CsiStore.journal_cleanup_quarantine

    def _fail_selected_journal(self, target_run_id, **kwargs):
        if kwargs["cleanup_quarantine"]["state"] == failed_state:
            raise sqlite3.OperationalError(f"forced_{failed_state}_failure")
        return original_journal(self, target_run_id, **kwargs)

    monkeypatch.setattr(
        CsiStore,
        "journal_cleanup_quarantine",
        _fail_selected_journal,
    )

    failed = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert failed.ok is False
    assert "forced_" in failed.error
    interrupted = CsiStore(db).get_run(run_id)
    assert interrupted is not None and interrupted["status"] == "CLEANING"
    interrupted_meta = json.loads(interrupted["implement_json"])
    journal = interrupted_meta.get("cleanup_quarantine")
    quarantines = list(worktree.parent.glob(".csi-cleanup-*.quarantine"))
    if quarantines:
        assert isinstance(journal, dict)
        assert str(quarantines[0]) == journal["path"]
        identity = os.stat(quarantines[0], follow_symlinks=False)
        assert (journal["device"], journal["inode"]) == (
            identity.st_dev,
            identity.st_ino,
        )
    else:
        assert failed_state == "rename_intent_bound"
        assert journal is None
        assert worktree.is_dir()

    monkeypatch.undo()
    recovered = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)
    recovered_row = CsiStore(db).get_run(run_id)

    assert recovered.action == "retained"
    if failed_state == "rename_intent_bound":
        assert recovered_row["status"] == "INCIDENT"
        assert recovered.retained_reason == "interrupted_cleanup_journal_missing"
    else:
        assert recovered_row["status"] == "REJECTED"
        assert recovered.retained_reason.startswith("interrupted_cleanup_reconciled:")
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_cleanup_claim_stops_if_state_changes_before_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    _git(repo, "merge", "--ff-only", result.branch)
    _preserve_meta_status(CsiStore(db), run_id, "MERGED", repo_root=repo)
    original_git = implement_module._git
    transitioned = False

    def _transition_after_clean_check(cwd, *args, check=True):
        nonlocal transitioned
        result_proc = original_git(cwd, *args, check=check)
        if not transitioned and args[:2] == ("status", "--porcelain=v1"):
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE csi_runs SET status='AWAITING_MERGE' WHERE id=?",
                    (run_id,),
                )
            transitioned = True
        return result_proc

    monkeypatch.setattr(implement_module, "_git", _transition_after_clean_check)

    cleaned = cleanup_implementation(run_id=run_id, db_path=db, repo_root=repo)

    assert transitioned
    assert cleaned.ok is False
    assert cleaned.retained_reason.startswith("cleanup_claim_lost:")
    assert CsiStore(db).get_run(run_id)["status"] == "AWAITING_MERGE"
    assert Path(result.worktree).exists()
    assert _git(repo, "branch", "--list", result.branch).stdout.strip()


def test_recovery_retains_stale_registration_and_moved_reviewer_copy(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    run_id = _seed(db, repo)
    result = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert result.ok
    implementation_sha = _sha(Path(result.worktree))
    moved_copy = tmp_path / "moved-review-copy"
    Path(result.worktree).rename(moved_copy)

    recovered = implement_approved(run_id=run_id, db_path=db, repo_root=repo)

    assert recovered.ok is False
    assert recovered.error == "stale_worktree_registration_retained"
    assert moved_copy.exists()
    assert _sha(moved_copy) == implementation_sha


def test_public_cleanup_journal_cannot_forge_rename_intent_or_retire_live_worktree(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    store = CsiStore(db)

    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    branch1 = "feature/wt1"
    branch2 = "feature/wt2"
    _git(repo, "worktree", "add", "-b", branch1, str(wt1))
    _git(repo, "worktree", "add", "-b", branch2, str(wt2))

    common = repo / ".git"
    wt1_stat = wt1.stat()
    wt2_admin = common / "worktrees" / wt2.name
    wt2_stat = wt2_admin.stat()

    run_id = "run_test_cleanup_forgery"
    base_sha = _sha(repo)
    row_dict = {
        "approval_status": "approved",
        "approved_by": "operator",
        "approved_at": "2026-07-26T00:00:00Z",
        "codebase_sha": base_sha,
        "evidence_json": "{}",
        "synthesis_json": "{}",
        "conflict_json": "{}",
    }
    app_binding = approval_binding(row_dict)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO csi_runs (id, routine_id, status, codebase_sha, approval_status, approved_by, approved_at, "
            "evidence_json, synthesis_json, conflict_json, implement_json, created_at) "
            "VALUES (?, 'routine_test', 'CLEANING', ?, 'approved', 'operator', '2026-07-26T00:00:00Z', "
            "'{}', '{}', '{}', ?, '2026-07-26T00:00:00Z')",
            (
                run_id,
                base_sha,
                json.dumps(
                    {
                        "worktree": str(wt1),
                        "branch": branch1,
                        "approval_binding": app_binding,
                        "cleanup_claim": {
                            "restore_status": "REJECTED",
                            "source_binding": row_dict,
                        },
                    }
                ),
            ),
        )

    run_row = store.get_run(run_id)
    assert run_row is not None
    claimed_json = run_row["implement_json"]

    forged_journal = {
        "path": str(wt1.parent / ".csi-cleanup-forged.quarantine"),
        "source_path": str(wt1),
        "device": wt1_stat.st_dev,
        "inode": wt1_stat.st_ino,
        "state": "rename_intent_bound",
        "path_verification": "source_bound_before_rename",
        "unlink_pending": True,
        "restore_status": "REJECTED",
        "registration": {
            "active_path": str(wt2_admin),
            "retired_path": str(common / "csi-retired-worktrees" / f"{wt2.name}-forged.retired"),
            "device": wt2_stat.st_dev,
            "inode": wt2_stat.st_ino,
            "worktree": str(wt1),
            "branch": branch1,
            "state": "active_bound",
        },
    }

    result = store.journal_cleanup_quarantine(
        run_id,
        observed_implement_json=claimed_json,
        cleanup_quarantine=forged_journal,
    )
    assert result is None, f"Expected None but got: {result}"
    assert wt2_admin.exists()


def test_finalize_implementation_claim_rejects_forged_provenance(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    store = CsiStore(db)

    wt = tmp_path / "wt_impl"
    branch = "feature/impl_forgery"
    _git(repo, "worktree", "add", "-b", branch, str(wt))

    base_sha = _sha(repo)
    run_id = "run_test_impl_forgery"
    claim_meta = {
        "worktree": str(wt),
        "branch": branch,
        "approval_binding": {
            "approved_by": "operator",
            "approved_at": "2026-07-26T00:00:00Z",
            "codebase_sha": base_sha,
        },
    }

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO csi_runs (id, routine_id, status, codebase_sha, approval_status, approved_by, approved_at, "
            "evidence_json, synthesis_json, conflict_json, implement_json, created_at) "
            "VALUES (?, 'routine_test', 'IMPLEMENTING', ?, 'approved', 'operator', '2026-07-26T00:00:00Z', '{}', '{}', '{}', ?, '2026-07-26T00:00:00Z')",
            (run_id, base_sha, json.dumps(claim_meta)),
        )

    row = store.get_run(run_id)
    assert row is not None

    (wt / "file.txt").write_text("hello")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "real commit")
    real_commit = _sha(wt)
    real_tree = _git(wt, "rev-parse", f"{real_commit}^{{tree}}").stdout.strip()

    real_file_hash = hashlib.sha256(b"hello").hexdigest()
    wt_gitfile = wt / ".git"
    gitfile_text = wt_gitfile.read_text("utf-8").strip()
    admin_path = Path(gitfile_text[len("gitdir: ") :].strip()).resolve()
    real_index_hash = hashlib.sha256((admin_path / "index").read_bytes()).hexdigest()

    valid_meta = dict(claim_meta)
    valid_meta.update(
        {
            "approval_binding": {
                "approved_by": "operator",
                "approved_at": "2026-07-26T00:00:00Z",
                "codebase_sha": base_sha,
            },
            "approved_by": "operator",
            "base_sha": base_sha,
            "branch": branch,
            "implementation_commit": real_commit,
            "implementation_parent": base_sha,
            "implementation_tree": real_tree,
            "staged_index_sha256": real_index_hash,
            "committed_payload_sha256": {"file.txt": real_file_hash},
            "worktree": str(wt),
            "written_paths": ["file.txt"],
        }
    )

    def _check_status(expected: str) -> None:
        r = store.get_run(run_id)
        assert r is not None
        assert r["status"] == expected

    # 1. Forged reviewer
    forged_reviewer = dict(valid_meta)
    forged_reviewer["approved_by"] = "attacker"
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_reviewer, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 2. Forged base_sha
    forged_base = dict(valid_meta)
    forged_base["base_sha"] = "0" * 40
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_base, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 3. Forged branch
    forged_branch = dict(valid_meta)
    forged_branch["branch"] = "main"
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_branch, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 4. Forged worktree
    forged_wt = dict(valid_meta)
    forged_wt["worktree"] = "/tmp/fake_wt"
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_wt, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 5. Forged parent
    forged_parent = dict(valid_meta)
    forged_parent["implementation_parent"] = "1" * 40
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_parent, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 6. Forged commit
    forged_commit = dict(valid_meta)
    forged_commit["implementation_commit"] = "2" * 40
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_commit, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 7. Forged tree
    forged_tree = dict(valid_meta)
    forged_tree["implementation_tree"] = "3" * 40
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_tree, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 8. Forged staged_index_sha256
    forged_index = dict(valid_meta)
    forged_index["staged_index_sha256"] = "a" * 64
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_index, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # 9. Forged committed_payload_sha256
    forged_payload = dict(valid_meta)
    forged_payload["committed_payload_sha256"] = {"file.txt": "b" * 64}
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_payload, repo_root=repo
        )
        is False
    )
    _check_status("IMPLEMENTING")

    # Authentic finalization must succeed
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=valid_meta, repo_root=repo
        )
        is True
    )
    _check_status("AWAITING_MERGE")


def test_minimal_claim_real_commit_forged_worktree_and_hashes_remains_implementing(
    tmp_path: Path,
    _repo_factory: Callable[[Path], Path]) -> None:
    repo = _repo_factory(tmp_path)
    db = tmp_path / "csi.db"
    store = CsiStore(db)

    wt = tmp_path / "wt_real"
    branch = "feature/minimal_claim_forgery"
    _git(repo, "worktree", "add", "-b", branch, str(wt))

    base_sha = _sha(repo)
    run_id = "run_minimal_claim_forgery"
    claim_meta = {
        "approval_binding": {
            "approved_by": "operator",
            "approved_at": "2026-07-26T00:00:00Z",
            "codebase_sha": base_sha,
        },
    }

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO csi_runs (id, routine_id, status, codebase_sha, approval_status, approved_by, approved_at, "
            "evidence_json, synthesis_json, conflict_json, implement_json, created_at) "
            "VALUES (?, 'routine_test', 'IMPLEMENTING', ?, 'approved', 'operator', '2026-07-26T00:00:00Z', '{}', '{}', '{}', ?, '2026-07-26T00:00:00Z')",
            (run_id, base_sha, json.dumps(claim_meta)),
        )

    row = store.get_run(run_id)
    assert row is not None

    (wt / "file.txt").write_text("minimal claim content")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "real commit for minimal claim")
    real_commit = _sha(wt)
    real_tree = _git(wt, "rev-parse", f"{real_commit}^{{tree}}").stdout.strip()

    # Minimal claim + real commit + forged worktree
    forged_wt_meta = {
        "approval_binding": {
            "approved_by": "operator",
            "approved_at": "2026-07-26T00:00:00Z",
            "codebase_sha": base_sha,
        },
        "approved_by": "operator",
        "base_sha": base_sha,
        "branch": branch,
        "implementation_commit": real_commit,
        "implementation_parent": base_sha,
        "implementation_tree": real_tree,
        "staged_index_sha256": "f" * 64,
        "committed_payload_sha256": {"file.txt": "f" * 64},
        "worktree": str(tmp_path / "fake_nonexistent_worktree"),
        "written_paths": ["file.txt"],
    }
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_wt_meta, repo_root=repo
        )
        is False
    )
    check_row = store.get_run(run_id)
    assert check_row is not None
    assert check_row["status"] == "IMPLEMENTING"

    # Minimal claim + real commit + real worktree + forged hashes
    forged_hashes_meta = dict(forged_wt_meta)
    forged_hashes_meta["worktree"] = str(wt)
    assert (
        store.finalize_implementation_claim(
            run_id, observed=row, implement_json=forged_hashes_meta, repo_root=repo
        )
        is False
    )
    check_row2 = store.get_run(run_id)
    assert check_row2 is not None
    assert check_row2["status"] == "IMPLEMENTING"
