from __future__ import annotations

import subprocess
from pathlib import Path

from omniagentos.contracts import DeclaredScope
from omniagentos.execution.verify import verify_working_tree


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, check=True
    ).stdout


def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "base.txt").write_text("base\n")
    git(tmp_path, "add", "base.txt")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_declared_coverage_has_full_precision(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "base.txt").write_text("changed\n")
    verdict = verify_working_tree(root, ["base.txt"])
    assert verdict["actual"] == ["base.txt"]
    assert verdict["missing"] == []
    assert verdict["precision"] == 1.0
    assert verdict["ok"] is True


def test_undeclared_path_reduces_precision(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "base.txt").write_text("changed\n")
    (root / "other.txt").write_text("new\n")
    verdict = verify_working_tree(root, ["base.txt"])
    assert "other.txt" in verdict["missing"]
    assert verdict["precision"] < 1
    assert verdict["ok"] is False


def test_keyword_only_risk_does_not_force_execution_level_four(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "base.txt").write_text("delete this ordinary record\n")
    verdict = verify_working_tree(root, ["base.txt"], suggested_level=1)
    assert verdict["keyword_advisory"] is True
    assert verdict["risk_level"] == 4
    assert verdict["execution_level"] == 1


def test_tier_p_forces_execution_level_four(tmp_path: Path) -> None:
    root = repo(tmp_path)
    path = root / "omniagentos" / "contracts.py"
    path.parent.mkdir()
    path.write_text("x = 1\n")
    verdict = verify_working_tree(root, ["omniagentos/contracts.py"])
    assert verdict["tier"] == "P"
    assert verdict["execution_level"] == 4


def test_tier_s_forces_execution_level_at_least_three(tmp_path: Path) -> None:
    root = repo(tmp_path)
    path = root / "omniagentos" / "orgdims" / "company_org.py"
    path.parent.mkdir(parents=True)
    path.write_text("x = 1\n")
    verdict = verify_working_tree(root, ["omniagentos/orgdims/company_org.py"])
    assert verdict["tier"] == "S"
    assert verdict["execution_level"] >= 3


def test_declared_mismatch_forces_execution_level_four(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "base.txt").write_text("changed\n")
    verdict = verify_working_tree(root, ["declared.txt"])
    assert verdict["execution_level"] == 4


def test_create_root_covers_descendants(tmp_path: Path) -> None:
    root = repo(tmp_path)
    path = root / "src" / "new.py"
    path.parent.mkdir()
    path.write_text("x = 1\n")
    verdict = verify_working_tree(root, DeclaredScope(create_roots=["src"]))
    assert verdict["missing"] == []
    assert verdict["precision"] == 1.0


def test_snapshot_does_not_mutate_real_index(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "base.txt").write_text("changed\n")
    (root / "untracked.txt").write_text("new\n")
    before = git(root, "status", "--porcelain")
    verify_working_tree(root, ["base.txt", "untracked.txt"])
    assert git(root, "status", "--porcelain") == before


def test_observation_failure_is_not_clean(tmp_path: Path, monkeypatch) -> None:
    """H-13: a failed disposable-index snapshot must not report ok/clean."""
    import omniagentos.execution.verify as verify_mod

    root = repo(tmp_path)
    (root / "base.txt").write_text("changed\n")

    def _boom(*_a, **_k):
        raise RuntimeError("git index unavailable")

    monkeypatch.setattr(verify_mod, "_entries_from_index", _boom)
    verdict = verify_working_tree(root, ["base.txt"])
    assert verdict["ok"] is False
    assert verdict["degraded"] is True
    assert verdict["observed_source"] == "unobserved"
    assert verdict["undeclared"] == []
    assert verdict["actual"] == []
    assert verdict["execution_level"] == 4
    assert verdict["precision"] == 0.0
    assert any("inconclusive" in r for r in verdict["reasons"])
    assert "observation_error" in verdict


def test_non_git_directory_is_degraded_not_clean(tmp_path: Path) -> None:
    """H-13: verifying a plain directory (no .git) must fail closed."""
    (tmp_path / "base.txt").write_text("x\n")
    verdict = verify_working_tree(tmp_path, ["base.txt"])
    assert verdict["ok"] is False
    assert verdict["degraded"] is True
    assert verdict["observed_source"] == "unobserved"
    assert verdict["execution_level"] == 4
