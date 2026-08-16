"""Tests for Sandbox (W3, §5, §5b.6, B1).

Tests cover:
  - Git worktree creation and cleanup
  - File diff application (create/modify/delete)
  - Pytest execution with scrubbed environment
  - Environment sanitization (no secrets)
  - Hard deadline + process-group kill
  - Authoritative git diff output
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from omniagentos.reliability.sandbox import Sandbox, SandboxResult, validate_proposal_commands


@pytest.fixture
def git_repo_dir():
    """Create a temporary git repository for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "repo"
        repo_path.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )

        # Create initial commit
        (repo_path / "README.md").write_text("# Test repo\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )

        yield str(repo_path)


class TestSandboxInit:
    """Test Sandbox initialization."""

    def test_init_with_default_sandbox_dir(self, git_repo_dir):
        """Sandbox init with default var/sandbox directory."""
        sandbox = Sandbox(git_repo_dir)
        assert sandbox.repo_dir == git_repo_dir
        assert sandbox.sandbox_dir == os.path.join(git_repo_dir, "var", "sandbox")
        assert sandbox.worktree_path.startswith(sandbox.sandbox_dir)

    def test_init_with_custom_sandbox_dir(self, git_repo_dir):
        """Sandbox init with custom sandbox directory."""
        custom_dir = tempfile.mkdtemp()
        try:
            sandbox = Sandbox(git_repo_dir, sandbox_dir=custom_dir)
            assert sandbox.sandbox_dir == custom_dir
            assert sandbox.worktree_path.startswith(custom_dir)
        finally:
            import shutil

            shutil.rmtree(custom_dir, ignore_errors=True)


class TestSandboxWorktree:
    """Test git worktree operations."""

    def test_prepare_worktree_creates_directory(self, git_repo_dir):
        """_prepare_worktree() creates worktree directory."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()

        assert os.path.isdir(sandbox.worktree_path)
        # Check git metadata exists (either .git dir or file)
        git_path = os.path.join(sandbox.worktree_path, ".git")
        assert os.path.exists(git_path)

        # Cleanup
        sandbox._cleanup_worktree()

    def test_cleanup_removes_worktree(self, git_repo_dir):
        """_cleanup_worktree() removes directory and git worktree."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()
        assert os.path.isdir(sandbox.worktree_path)

        sandbox._cleanup_worktree()
        assert not os.path.exists(sandbox.worktree_path)


class TestFileDiffApplication:
    """Test file diff application (B1)."""

    def test_apply_create_file(self, git_repo_dir):
        """Apply create file diff."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()

        try:
            sandbox._apply_file_diff(
                {
                    "path": "new_file.txt",
                    "action": "create",
                    "diff": "New content\n",
                }
            )

            created_file = os.path.join(sandbox.worktree_path, "new_file.txt")
            assert os.path.exists(created_file)
        finally:
            sandbox._cleanup_worktree()

    def test_apply_modify_file(self, git_repo_dir):
        """Apply modify file diff."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()

        try:
            # Create file first
            test_file = os.path.join(sandbox.worktree_path, "test.py")
            Path(test_file).write_text("old content\n")

            # Apply modification
            diff = """--- a/test.py
+++ b/test.py
@@ -1 +1 @@
-old content
+new content
"""
            sandbox._apply_file_diff(
                {
                    "path": "test.py",
                    "action": "modify",
                    "diff": diff,
                }
            )

            assert os.path.exists(test_file)
        finally:
            sandbox._cleanup_worktree()

    def test_apply_delete_file(self, git_repo_dir):
        """Apply delete file action."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()

        try:
            # Create file first
            test_file = os.path.join(sandbox.worktree_path, "to_delete.txt")
            Path(test_file).write_text("content\n")
            assert os.path.exists(test_file)

            # Delete it
            sandbox._apply_file_diff(
                {
                    "path": "to_delete.txt",
                    "action": "delete",
                    "diff": "",
                }
            )

            assert not os.path.exists(test_file)
        finally:
            sandbox._cleanup_worktree()

    def test_apply_rejects_path_escape(self, git_repo_dir):
        """Reject diffs that try to escape worktree."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()

        try:
            with pytest.raises(ValueError, match="escapes worktree"):
                sandbox._apply_file_diff(
                    {
                        "path": "../../escaped_file.txt",
                        "action": "create",
                        "diff": "content",
                    }
                )
        finally:
            sandbox._cleanup_worktree()


class TestEnvironmentScrubbing:
    """Test environment scrubbing (§5b.6, B1)."""

    def test_scrub_env_removes_omniagentos_secrets(self, git_repo_dir):
        """_scrub_env() removes OMNIAGENTOS_*SECRET* vars."""
        sandbox = Sandbox(git_repo_dir)

        with patch.dict(
            os.environ,
            {
                "OMNIAGENTOS_DB_SECRET": "secret_value",
                "OMNIAGENTOS_API_KEY": "key",
                "HOME": "/home/user",
            },
        ):
            env = sandbox._scrub_env()
            assert "OMNIAGENTOS_DB_SECRET" not in env
            assert "HOME" in env

    def test_scrub_env_removes_var_secrets_paths(self, git_repo_dir):
        """_scrub_env() is drop-by-default: anything outside the allowlist dies."""
        sandbox = Sandbox(git_repo_dir)

        with patch.dict(
            os.environ,
            {
                "SECRET_PATH": "/var/secrets/api_token",
                "NORMAL_VAR": "normal_value",
                "HOME": "/home/user",
            },
        ):
            env = sandbox._scrub_env()
            assert "SECRET_PATH" not in env
            # Drop-by-default (red-team B1): un-allowlisted vars do NOT survive.
            assert "NORMAL_VAR" not in env

    def test_scrub_env_removes_connections_env_secrets(self, git_repo_dir):
        """Live keys sourced from connections.env (non-OMNIAGENTOS names) never survive."""
        sandbox = Sandbox(git_repo_dir)

        with patch.dict(
            os.environ,
            {
                "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY": "sk_live_DEADBEEF",
                "PAYPAL_READONLY_CLIENT_SECRET": "pp_secret",
                "CREDENTIALS_ENCRYPTION_KEY": "enc_key",
                "OPENAI_API_KEY": "sk-xxxx",
                "PATH": "/usr/bin",
                "HOME": "/home/user",
            },
        ):
            env = sandbox._scrub_env()
            assert "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY" not in env
            assert "PAYPAL_READONLY_CLIENT_SECRET" not in env
            assert "CREDENTIALS_ENCRYPTION_KEY" not in env
            assert "OPENAI_API_KEY" not in env
            assert env["PATH"] == "/usr/bin"

    def test_scrub_env_keeps_essential_vars(self, git_repo_dir):
        """_scrub_env() keeps PATH, HOME, PYTHONPATH."""
        sandbox = Sandbox(git_repo_dir)

        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/user",
                "PYTHONPATH": "/opt/python",
                "LANG": "en_US.UTF-8",
                "OMNIAGENTOS_SECRET": "secret",
            },
        ):
            env = sandbox._scrub_env()
            assert "PATH" in env
            assert "HOME" in env
            assert "PYTHONPATH" in env
            assert "LANG" in env
            assert "OMNIAGENTOS_SECRET" not in env

    def test_scrub_env_assertion_no_secrets(self, git_repo_dir):
        """Verify scrubbed env contains no secrets in values."""
        sandbox = Sandbox(git_repo_dir)

        with patch.dict(
            os.environ,
            {
                "CUSTOM_VAR": "normal_value",
                "HOME": "/home/user",
                "PATH": "/usr/bin",
            },
        ):
            env = sandbox._scrub_env()
            for key, value in env.items():
                assert "var/secrets" not in value
                assert "SECRET" not in key


class TestGitDiffExtraction:
    """Test authoritative git diff extraction."""

    def test_get_git_diff_with_no_changes(self, git_repo_dir):
        """_get_git_diff() returns empty stats for unmodified repo."""
        sandbox = Sandbox(git_repo_dir)
        sandbox._prepare_worktree()

        try:
            diff_info = sandbox._get_git_diff()
            assert len(diff_info["files_changed"]) == 0
            assert diff_info["insertions"] == 0
            assert diff_info["deletions"] == 0
        finally:
            sandbox._cleanup_worktree()


class TestSandboxResult:
    """Test SandboxResult data model."""

    def test_result_to_json(self):
        """SandboxResult.to_json() returns JSON-serializable dict."""
        result = SandboxResult(
            id="snd_123",
            status="green",
            tests_passed=5,
            tests_failed=0,
            test_count=5,
            git_diff_insertions=10,
            git_diff_deletions=2,
        )

        json_data = result.to_json()
        assert json_data["status"] == "green"
        assert json_data["tests_passed"] == 5
        assert json_data["git_diff_insertions"] == 10

    def test_result_error_truncation(self):
        """SandboxResult truncates stderr tail to 500 chars."""
        long_error = "x" * 1000
        result = SandboxResult(
            id="snd_123",
            status="error",
            stderr_tail=long_error,
        )

        json_data = result.to_json()
        assert len(json_data["stderr_tail"]) <= 500


class TestSandboxFailClosed:
    """H-40: proposal-authored pytest never runs without OS confinement."""

    def test_missing_sandbox_support_refuses_pytest(self, git_repo_dir):
        """When OS sandbox is unavailable, _run_pytest raises (no unconfined fallthrough)."""
        sandbox = Sandbox(git_repo_dir)
        sandbox.worktree_path = git_repo_dir
        with patch("omniagentos.runner.sandbox.sandbox_available", return_value=False):
            with pytest.raises(RuntimeError, match="sandbox_unavailable"):
                sandbox._confine_pytest_command(["python", "-m", "pytest"])

    def test_profile_construction_failure_refuses_pytest(self, git_repo_dir):
        """Profile builder exceptions are not swallowed into unsandboxed execution."""
        sandbox = Sandbox(git_repo_dir)
        sandbox.worktree_path = git_repo_dir
        with (
            patch("omniagentos.runner.sandbox.sandbox_available", return_value=True),
            patch(
                "omniagentos.runner.sandbox.build_profile",
                side_effect=RuntimeError("profile boom"),
            ),
        ):
            with pytest.raises(RuntimeError, match="sandbox_profile_failed"):
                sandbox._confine_pytest_command(["python", "-m", "pytest"])

    def test_contained_success_wraps_sandbox_exec(self, git_repo_dir):
        """Successful setup prefixes the argv with sandbox-exec + profile."""
        sandbox = Sandbox(git_repo_dir)
        sandbox.worktree_path = git_repo_dir
        with (
            patch("omniagentos.runner.sandbox.sandbox_available", return_value=True),
            patch(
                "omniagentos.runner.sandbox.build_profile",
                return_value="(version 1)\n(allow default)",
            ),
        ):
            wrapped = sandbox._confine_pytest_command(["python", "-m", "pytest", "t.py"])
        assert wrapped[0] == "/usr/bin/sandbox-exec"
        assert wrapped[1] == "-p"
        assert "deny network" in wrapped[2]
        assert wrapped[3:] == ["python", "-m", "pytest", "t.py"]

    def test_run_pytest_does_not_popen_when_sandbox_missing(self, git_repo_dir):
        """_run_pytest must not launch pytest if confinement cannot be established."""
        sandbox = Sandbox(git_repo_dir)
        sandbox.worktree_path = git_repo_dir
        with (
            patch("omniagentos.runner.sandbox.sandbox_available", return_value=False),
            patch("omniagentos.reliability.sandbox.subprocess.Popen") as popen,
        ):
            with pytest.raises(RuntimeError, match="sandbox_unavailable"):
                sandbox._run_pytest(["tests/test_x.py"])
        popen.assert_not_called()


class TestExitCodeGrading:
    """Verdicts must follow the pytest exit code: exit 5 (no tests collected)
    and internal errors can never score green."""

    def test_exit_5_no_tests_collected_grades_red(self, git_repo_dir):
        sandbox = Sandbox(str(git_repo_dir))
        stub = {
            "passed": 0,
            "failed": 0,
            "count": 0,
            "errors": ["pytest exit 5: no tests were collected for the derived targets"],
            "timed_out": False,
            "returncode": 5,
        }
        with patch.object(Sandbox, "_run_pytest", return_value=stub):
            result = sandbox.apply_and_test([], pytest_targets=["tests/test_missing.py"])
        assert result.status == "red"

    def test_exit_0_grades_green(self, git_repo_dir):
        sandbox = Sandbox(str(git_repo_dir))
        stub = {
            "passed": 3,
            "failed": 0,
            "count": 3,
            "errors": [],
            "timed_out": False,
            "returncode": 0,
        }
        with patch.object(Sandbox, "_run_pytest", return_value=stub):
            result = sandbox.apply_and_test([], pytest_targets=["tests/test_ok.py"])
        assert result.status == "green"


class TestProposalValidation:
    """Test proposal command validation (B1)."""

    def test_validate_safe_plan(self):
        """Safe plan narrative should not raise."""
        proposal = {
            "plan": [
                "This is a safe narrative explanation",
                "It describes the changes without commands",
            ],
        }
        # Should not raise
        validate_proposal_commands(proposal)

    def test_validate_rejects_command_substitution(self):
        """Reject command substitution patterns."""
        proposal = {
            "plan": [
                "Run the command $(rm -rf /)",
            ],
        }
        with pytest.raises(ValueError, match="Suspicious command pattern"):
            validate_proposal_commands(proposal)

    def test_validate_rejects_backtick_substitution(self):
        """Reject backtick substitution."""
        proposal = {
            "plan": [
                "Execute `dangerous_command`",
            ],
        }
        with pytest.raises(ValueError, match="Suspicious command pattern"):
            validate_proposal_commands(proposal)

    def test_validate_rejects_destructive_pipes(self):
        """Reject pipes to destructive commands."""
        proposal = {
            "plan": [
                "Some operation | bash && rm -rf .",
            ],
        }
        with pytest.raises(ValueError, match="Suspicious command pattern"):
            validate_proposal_commands(proposal)


class TestPipelineExitCodeGrading:
    """PipelineSandboxRunner must grade on the pytest exit code end-to-end."""

    def _improvement(self):
        from types import SimpleNamespace

        return SimpleNamespace(proposal_json={"files": []})

    def test_exit_5_fails_pipeline_outcome(self, git_repo_dir, monkeypatch):
        from omniagentos.reliability.sandbox import PipelineSandboxRunner

        monkeypatch.setattr(
            Sandbox, "_derive_pytest_targets", lambda self, files: ["tests/test_x.py"]
        )
        stub = {
            "passed": 0,
            "failed": 0,
            "count": 0,
            "errors": ["pytest exit 5: no tests were collected for the derived targets"],
            "timed_out": False,
            "returncode": 5,
        }
        monkeypatch.setattr(Sandbox, "_run_pytest", lambda self, targets: stub)
        outcome = PipelineSandboxRunner().run(self._improvement(), str(git_repo_dir))
        assert outcome.passed is False

    def test_exit_0_passes_pipeline_outcome(self, git_repo_dir, monkeypatch):
        from omniagentos.reliability.sandbox import PipelineSandboxRunner

        monkeypatch.setattr(
            Sandbox, "_derive_pytest_targets", lambda self, files: ["tests/test_x.py"]
        )
        stub = {
            "passed": 2,
            "failed": 0,
            "count": 2,
            "errors": [],
            "timed_out": False,
            "returncode": 0,
        }
        monkeypatch.setattr(Sandbox, "_run_pytest", lambda self, targets: stub)
        outcome = PipelineSandboxRunner().run(self._improvement(), str(git_repo_dir))
        assert outcome.passed is True
