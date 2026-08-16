"""Post-attempt mechanical assess + violation plan."""

from __future__ import annotations

from pathlib import Path

from omniagentos.execution.post_attempt import evaluate_post_attempt


def test_pass_when_expected_file_present(tmp_path: Path) -> None:
    (tmp_path / "out.md").write_text("hello world\n", encoding="utf-8")
    # Pure mechanical presence: skip live verify (non-git tmp dirs are
    # unobserved and H-13 fail-closes that path).
    r = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["out.md"],
        lane="swarm",
        holder_id="t1",
        use_verify=False,
    )
    assert r.mechanical_pass is True
    assert r.assess_verdict == "pass"


def test_default_verify_on_non_git_cannot_mechanical_pass(tmp_path: Path) -> None:
    """H-13: default use_verify=True must not clean-pass when observe fails."""
    (tmp_path / "out.md").write_text("hello world\n", encoding="utf-8")
    r = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["out.md"],
        lane="swarm",
        holder_id="t_default_verify",
        # use_verify defaults to True — non-git tmp is unobserved.
    )
    assert r.mechanical_pass is False
    assert r.assess_verdict == "fail"
    assert any("verify_error" in f or "observation" in f for f in r.flags)
    assert r.violation_actions


def test_fail_missing_file(tmp_path: Path) -> None:
    # Legacy missing-expected-file path without live verify (assess only).
    r = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["missing.md"],
        lane="runner",
        holder_id="run_1",
        use_verify=False,
    )
    assert r.mechanical_pass is False
    assert r.assess_verdict == "fail"


def test_undeclared_triggers_swarm_plan(tmp_path: Path) -> None:
    r = evaluate_post_attempt(
        working_dir=tmp_path,
        undeclared_paths=["secret.py"],
        lane="swarm",
        holder_id="swt_1",
    )
    assert "revert_worktree" in r.violation_actions or r.violation_actions


def test_verify_exception_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """use_verify=True must not soft-swallow verify crashes (Opus caveat)."""

    def _boom(*_a, **_k):
        raise RuntimeError("git index unavailable")

    # Patch at import site used inside evaluate_post_attempt
    import omniagentos.execution.verify as ver

    monkeypatch.setattr(ver, "verify_working_tree", _boom)

    r = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["out.md"],
        declared_scope=["out.md"],
        lane="swarm",
        holder_id="t_err",
        use_verify=True,
    )
    assert r.mechanical_pass is False
    assert r.assess_verdict == "fail"
    assert any("verify_error" in f for f in r.flags)
    assert r.violation_actions


def test_unobserved_verify_result_fails_mechanical_pass(tmp_path: Path, monkeypatch) -> None:
    """H-13: unobserved/degraded verify must not become a mechanical pass."""
    (tmp_path / "out.md").write_text("hello world\n", encoding="utf-8")
    import omniagentos.execution.verify as ver

    def _unobserved(*_a, **_k):
        return {
            "ok": False,
            "degraded": True,
            "observed_source": "unobserved",
            "undeclared": [],
            "missing": [],
            "observation_error": "RuntimeError:git index unavailable",
            "reasons": ["working-tree observation was inconclusive"],
            "execution_level": 4,
        }

    monkeypatch.setattr(ver, "verify_working_tree", _unobserved)
    r = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["out.md"],
        declared_scope=["out.md"],
        lane="swarm",
        holder_id="t_unobs",
        use_verify=True,
    )
    assert r.mechanical_pass is False
    assert r.assess_verdict == "fail"
    assert any("observation" in f for f in r.flags)
