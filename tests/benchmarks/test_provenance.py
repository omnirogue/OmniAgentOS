"""Tests for benchmark provenance gate and sidecar records.

These tests use real git repositories on purpose: a mocked git status
would prove nothing about dirty-tree detection.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.benchmarks import capture_baseline, provenance
from scripts.benchmarks.provenance import (
    assert_clean_tree,
    verify_unmoved,
)


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(repo),
        }
    )
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """A REAL git repo with one commit: omniagentos/mod.py, scripts/s.py, configs/llm.yaml,

    uv.lock, notes.txt, docs/readme.txt.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    (repo / "omniagentos").mkdir(parents=True, exist_ok=True)
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir(parents=True, exist_ok=True)

    (repo / "omniagentos" / "mod.py").write_text("print('mod')\n", encoding="utf-8")
    (repo / "scripts" / "s.py").write_text("print('s')\n", encoding="utf-8")
    (repo / "configs" / "llm.yaml").write_text("model: mock\n", encoding="utf-8")
    (repo / "uv.lock").write_text("lockfile\n", encoding="utf-8")
    (repo / "notes.txt").write_text("some notes\n", encoding="utf-8")
    (repo / "docs" / "readme.txt").write_text("readme\n", encoding="utf-8")

    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def test_clean_repo_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state = assert_clean_tree(repo)
    expected_sha = _git(repo, "rev-parse", "HEAD")
    assert state.commit_sha == expected_sha
    assert state.clean is True
    assert state.dirty_paths == ()


def test_tracked_modification_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "omniagentos" / "mod.py").write_text("modified\n", encoding="utf-8")
    with pytest.raises(provenance.DirtyTreeError) as exc:
        assert_clean_tree(repo)
    assert "omniagentos/mod.py" in str(exc.value)
    assert "dirty tree" in str(exc.value)


def test_staged_change_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "omniagentos" / "new.py").write_text("new content\n", encoding="utf-8")
    _git(repo, "add", "omniagentos/new.py")
    with pytest.raises(provenance.DirtyTreeError) as exc:
        assert_clean_tree(repo)
    assert "omniagentos/new.py" in str(exc.value)


def test_deleted_file_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    os.remove(repo / "omniagentos" / "mod.py")
    with pytest.raises(provenance.DirtyTreeError) as exc:
        assert_clean_tree(repo)
    assert "omniagentos/mod.py" in str(exc.value)


def test_untracked_scratch_is_recorded_but_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "notes_new.txt").write_text("scratch notes\n", encoding="utf-8")
    state = assert_clean_tree(repo)
    assert "notes_new.txt" in state.untracked_paths
    assert state.blocking_untracked == ()


def test_untracked_python_under_code_root_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "omniagentos" / "sneaky.py").write_text("untracked py\n", encoding="utf-8")
    with pytest.raises(provenance.DirtyTreeError) as exc:
        assert_clean_tree(repo)
    assert "sneaky.py" in str(exc.value)
    assert "untracked code" in str(exc.value)


def test_untracked_python_outside_code_roots_is_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "x.py").write_text("untracked py outside\n", encoding="utf-8")
    state = assert_clean_tree(repo)
    assert "docs/x.py" in state.untracked_paths
    assert state.blocking_untracked == ()


def test_head_move_mid_capture_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = assert_clean_tree(repo)
    (repo / "new_file.txt").write_text("new content\n", encoding="utf-8")
    _git(repo, "add", "new_file.txt")
    _git(repo, "commit", "-m", "new commit")
    new_sha = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(provenance.MovedTreeError) as exc:
        verify_unmoved(before, repo)
    assert before.commit_sha[:12] in str(exc.value)
    assert new_sha[:12] in str(exc.value)


def test_untracked_python_in_new_directory_under_code_root_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    new_dir = repo / "omniagentos" / "newpkg"
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "mod.py").write_text("print(123)\n", encoding="utf-8")
    with pytest.raises(provenance.DirtyTreeError) as exc:
        assert_clean_tree(repo)
    assert "omniagentos/newpkg/mod.py" in str(exc.value)
    assert "untracked code" in str(exc.value)


def test_untracked_non_python_in_new_directory_under_code_root_is_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    new_dir = repo / "scripts" / "scratch"
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "data.json").write_text("{}\n", encoding="utf-8")
    state = assert_clean_tree(repo)
    assert "scripts/scratch/data.json" in state.untracked_paths
    assert state.blocking_untracked == ()


def test_unmoved_head_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = assert_clean_tree(repo)
    verify_unmoved(before, repo)  # must not raise: nothing changed
    assert provenance.git_state(repo).commit_sha == before.commit_sha


def test_tracked_change_mid_capture_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = assert_clean_tree(repo)
    (repo / "omniagentos" / "mod.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(provenance.MovedTreeError) as exc:
        verify_unmoved(before, repo)
    assert "working tree changed" in str(exc.value)
    assert "omniagentos/mod.py" in str(exc.value)


def test_untracked_code_appearing_mid_capture_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = assert_clean_tree(repo)
    (repo / "omniagentos" / "late.py").write_text("print(456)\n", encoding="utf-8")
    with pytest.raises(provenance.MovedTreeError) as exc:
        verify_unmoved(before, repo)
    assert "omniagentos/late.py" in str(exc.value)
    assert "mid-capture" in str(exc.value)


def test_collect_records_full_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    file_bytes = (repo / "configs" / "llm.yaml").read_bytes()
    expected_config_digest = hashlib.sha256(file_bytes).hexdigest()

    lock_bytes = (repo / "uv.lock").read_bytes()
    expected_lock_digest = hashlib.sha256(lock_bytes).hexdigest()

    rec, state = provenance.collect(
        capture_id="cap_x",
        repo_root=repo,
        model_id="grok-4.5",
        config_paths=("configs/llm.yaml",),
        cli_names=(),
        sandbox_mode="seatbelt",
    )

    assert rec.commit_sha == state.commit_sha
    assert rec.dirty is False
    assert rec.config_digests["configs/llm.yaml"] == expected_config_digest
    assert rec.lock_path == "uv.lock"
    assert rec.lock_digest == expected_lock_digest
    assert rec.model_id == "grok-4.5"
    assert rec.sandbox_mode == "seatbelt"


def test_missing_config_and_lock_record_absent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    os.remove(repo / "configs" / "llm.yaml")
    os.remove(repo / "uv.lock")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "removed files")

    rec, state = provenance.collect(
        capture_id="cap_y",
        repo_root=repo,
        model_id="grok-4.5",
        config_paths=("configs/llm.yaml",),
        cli_names=(),
        sandbox_mode="seatbelt",
    )
    assert rec.config_digests["configs/llm.yaml"] == provenance.ABSENT
    assert rec.lock_path == provenance.ABSENT
    assert rec.lock_digest == provenance.ABSENT


def test_to_dict_is_json_serializable_and_stable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rec, _ = provenance.collect(
        capture_id="cap_x",
        repo_root=repo,
        model_id="grok-4.5",
        config_paths=("configs/llm.yaml",),
        cli_names=(),
        sandbox_mode="seatbelt",
    )
    d = rec.to_dict()
    serialized = json.dumps(d, sort_keys=True)
    assert isinstance(serialized, str)

    rebuilt = provenance.CaptureProvenance.from_dict(d)
    assert rebuilt.to_dict() == d


def test_environment_hash_ignores_timing_fields(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rec, _ = provenance.collect(
        capture_id="cap_x",
        repo_root=repo,
        model_id="grok-4.5",
        config_paths=("configs/llm.yaml",),
        cli_names=(),
        sandbox_mode="seatbelt",
    )
    rec2 = replace(rec, capture_id="cap_different", captured_at="2026-12-31T23:59:59SZ")
    assert rec.environment_hash() == rec2.environment_hash()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "different-model"),
        ("commit_sha", "610a734b8fc3b508920669267e4c0de6eb134b64"),
        ("config_digests", {"configs/llm.yaml": "different_digest"}),
        ("sandbox_mode", "unavailable"),
        ("lock_digest", "different_lock_digest"),
        ("cli_versions", {"grok": "9.9.9"}),
        ("python_version", "3.13.0"),
        ("env_hash", "ffffffffffffffff"),
    ],
)
def test_environment_hash_differs_on_every_environment_field(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    repo = _repo(tmp_path)
    rec, _ = provenance.collect(
        capture_id="cap_x",
        repo_root=repo,
        model_id="grok-4.5",
        config_paths=("configs/llm.yaml",),
        cli_names=(),
        sandbox_mode="seatbelt",
    )
    kwargs = {field: value}
    mutated = replace(rec, **kwargs)
    assert rec.environment_hash() != mutated.environment_hash()


def test_write_sidecar_round_trip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rec, _ = provenance.collect(
        capture_id="cap_x",
        repo_root=repo,
        model_id="grok-4.5",
        config_paths=("configs/llm.yaml",),
        cli_names=(),
        sandbox_mode="seatbelt",
    )
    written_path = provenance.write_sidecar(rec, tmp_path)
    expected_path = tmp_path / "provenance" / f"{rec.capture_id}.json"
    assert written_path == expected_path
    assert expected_path.is_file()

    data = json.loads(expected_path.read_text(encoding="utf-8"))
    assert data["environment_hash"] == rec.environment_hash()
    assert data["commit_sha"] == rec.commit_sha


def test_capture_baseline_aborts_on_dirty_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    (repo / "omniagentos" / "mod.py").write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(capture_baseline, "REPO_ROOT", repo)
    out = tmp_path / "out"
    rc = capture_baseline.main(["--arm", "mock", "--out-dir", str(out)])
    # Assert exact exit code of 4 (dirty tree abort) since a bare rc != 0
    # can pass if the run proceeds and fails downstream.
    assert rc == 4, f"dirty tree must abort with exit 4, got {rc}"
    assert not (out / "baseline.db").exists()
    assert not (out / "results.jsonl").exists()
    captured = capsys.readouterr()
    assert "dirty tree" in captured.err


def test_capture_baseline_gate_runs_before_any_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    (repo / "omniagentos" / "mod.py").write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(capture_baseline, "REPO_ROOT", repo)
    out = tmp_path / "out"
    rc = capture_baseline.main(["--arm", "mock", "--out-dir", str(out)])
    # Assert exact exit code of 4 (dirty tree abort) since a bare rc != 0
    # can pass if the run proceeds and fails downstream.
    assert rc == 4, f"dirty tree must abort with exit 4, got {rc}"
    assert not (out / "provenance").exists()
