"""Git worktree sandbox for testing improvements in isolation (§5, §5b.6, B1).

Each improvement gets its own disposable worktree for patch application and
pytest execution. Sandbox process runs with:
  - Drop-by-default env allowlist (PATH/HOME/PYTHONPATH/LANG/TZ/TERM/LC_* only —
    connections.env secrets never survive)
  - Network denied + writes confined to the worktree (sandbox-exec SBPL, when available)
  - Hard deadline + process-group kill

Only declared file diffs are applied; no proposal-supplied commands execute —
the ONLY subprocesses here are hardcoded git/pytest argv lists.
Results include authoritative git diff (name-status + content).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.reliability.governance import DiffEntry, build_diff_entries


@dataclass
class SandboxResult:
    """Sandbox execution result (mapped to improvements.sandbox_json)."""

    id: str
    status: str  # 'green' | 'red' | 'timeout' | 'error'
    tests_passed: int = 0
    tests_failed: int = 0
    test_count: int = 0
    test_errors: list[str] = field(default_factory=list)  # Only first 3 errors
    git_diff_lines: int = 0
    git_diff_files_changed: list[str] = field(default_factory=list)
    git_diff_insertions: int = 0
    git_diff_deletions: int = 0
    stderr_tail: str = ""
    executed_at: str = ""

    def __post_init__(self) -> None:
        if self.test_errors is None:
            self.test_errors = []
        if self.git_diff_files_changed is None:
            self.git_diff_files_changed = []
        if not self.executed_at:
            self.executed_at = utc_now_iso()

    def to_json(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "id": self.id,
            "status": self.status,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "test_count": self.test_count,
            "test_errors": self.test_errors[:3],  # First 3
            "git_diff_lines": self.git_diff_lines,
            "git_diff_files_changed": self.git_diff_files_changed,
            "git_diff_insertions": self.git_diff_insertions,
            "git_diff_deletions": self.git_diff_deletions,
            "stderr_tail": self.stderr_tail[-500:] if self.stderr_tail else "",  # Last 500 chars
            "executed_at": self.executed_at,
        }


class Sandbox:
    """Isolated git worktree for patch application + pytest (§5, B1).

    - Creates worktree under var/sandbox/<id>/
    - Applies only declared file diffs (no commands)
    - Runs pipeline-selected pytest with scrubbed env
    - Hard deadline + process-group kill
    - Authoritative git diff output
    - Always cleans up (worktree removal + rmtree)
    """

    def __init__(
        self,
        repo_dir: str,
        sandbox_dir: str | None = None,
        pytest_timeout_seconds: int = 300,
    ):
        """Initialize sandbox.

        Args:
            repo_dir: Path to the real git repo (source for worktree)
            sandbox_dir: Parent dir for worktrees (default: <repo>/var/sandbox/)
            pytest_timeout_seconds: Hard deadline for pytest execution
        """
        self.repo_dir = repo_dir
        self.sandbox_dir = sandbox_dir or os.path.join(repo_dir, "var", "sandbox")
        self.pytest_timeout_seconds = pytest_timeout_seconds

        self.sandbox_id = new_id("snd")
        self.worktree_path = os.path.join(self.sandbox_dir, self.sandbox_id)

    def apply_and_test(
        self,
        file_diffs: list[dict[str, str]],
        pytest_targets: list[str] | None = None,
    ) -> SandboxResult:
        """Apply declared diffs and run pytest (B1: only declared actions).

        Args:
            file_diffs: List of {"path": str, "action": "create|modify|delete", "diff": str}
            pytest_targets: Pytest file targets (auto-derived from touched paths if omitted)

        Returns:
            SandboxResult with execution summary

        Raises:
            ValueError: If file_diffs contains undeclared commands or suspicious paths
        """
        result = SandboxResult(id=self.sandbox_id, status="error")

        try:
            # Prepare worktree
            self._prepare_worktree()
            result.status = "green"  # Assume green until proven otherwise

            # Apply diffs (B1: no commands, only files)
            for diff_spec in file_diffs:
                self._apply_file_diff(diff_spec)

            # Get authoritative git diff
            git_diff_info = self._get_git_diff()
            result.git_diff_lines = git_diff_info["lines"]
            result.git_diff_files_changed = git_diff_info["files_changed"]
            result.git_diff_insertions = git_diff_info["insertions"]
            result.git_diff_deletions = git_diff_info["deletions"]

            # Run pytest
            if pytest_targets is None:
                # Auto-derive from touched files
                pytest_targets = self._derive_pytest_targets(git_diff_info["files_changed"])

            if pytest_targets:
                test_result = self._run_pytest(pytest_targets)
                result.tests_passed = test_result["passed"]
                result.tests_failed = test_result["failed"]
                result.test_count = test_result["count"]
                result.test_errors = test_result["errors"]

                if test_result.get("timed_out"):
                    result.status = "timeout"
                elif test_result["failed"] > 0 or test_result.get("returncode") != 0:
                    # Verdicts follow the pytest exit code, never stdout parsing:
                    # exit 5 (nothing collected) and internal errors must not
                    # score green.
                    result.status = "red"
            else:
                # No tests to run
                result.test_count = 0
                result.tests_passed = 0

        except subprocess.TimeoutExpired:
            result.status = "timeout"
            result.test_errors = ["Pytest execution exceeded deadline"]
        except Exception as e:
            result.status = "error"
            result.test_errors = [str(e)]
            result.stderr_tail = str(e)
        finally:
            # Always cleanup
            self._cleanup_worktree()

        return result

    def _prepare_worktree(self) -> None:
        """Create git worktree at self.worktree_path (§5, B1)."""
        os.makedirs(self.sandbox_dir, exist_ok=True)

        # Create detached worktree from HEAD
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", self.worktree_path, "HEAD"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")

    def _apply_file_diff(self, diff_spec: dict[str, str]) -> None:
        """Apply a single declared file diff (B1: files only, no commands)."""
        path = diff_spec.get("path", "")
        action = diff_spec.get("action", "modify")
        diff_content = diff_spec.get("diff", "")

        if not path:
            raise ValueError("File diff missing 'path'")

        # Validate path doesn't escape worktree or touch protected dirs
        abs_path = os.path.abspath(os.path.join(self.worktree_path, path))
        if inode_relative_parts_anchored(abs_path, os.path.abspath(self.worktree_path)) is None:
            raise ValueError(f"Path escapes worktree: {path}")

        # A "content" spec writes the whole file verbatim (the §5 file object may
        # carry `content` instead of a unified `diff`); this is the SAME mutation the
        # pipeline's apply performs, so the sandbox diff matches what will be applied.
        if action != "delete" and "content" in diff_spec:
            os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
            with open(abs_path, "w") as fh:
                fh.write(diff_spec["content"])
            return

        # Apply diff via git apply
        if action == "delete":
            # Delete file
            file_abs = os.path.join(self.worktree_path, path)
            if os.path.exists(file_abs):
                os.remove(file_abs)
        elif action == "create":
            # Create new file with content from diff (no unified diff header)
            file_abs = os.path.join(self.worktree_path, path)
            os.makedirs(os.path.dirname(file_abs), exist_ok=True)
            with open(file_abs, "w") as f:
                # Strip unified diff headers if present
                lines = diff_content.split("\n")
                in_header = True
                for line in lines:
                    if in_header and (line.startswith("---") or line.startswith("+++")):
                        continue
                    if in_header and line.startswith("@@"):
                        in_header = False
                        continue
                    if not in_header or (not line.startswith("---") and not line.startswith("+++")):
                        # Remove leading +/- from patch content
                        if line.startswith("+") and not line.startswith("+++"):
                            f.write(line[1:] + "\n")
                        elif line.startswith("-") and not line.startswith("---"):
                            pass  # Skip delete lines
                        elif not line.startswith("+") and not line.startswith("-"):
                            f.write(line + "\n")
        else:  # modify
            # Apply unified diff via git apply
            result = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=self.worktree_path,
                input=diff_content,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git apply failed for {path}: {result.stderr}")

    def _get_git_diff(self) -> dict[str, Any]:
        """Get authoritative git diff (name-status + stats)."""
        # Get name-status
        result = subprocess.run(
            ["git", "diff", "--name-status", "HEAD"],
            cwd=self.worktree_path,
            capture_output=True,
            text=True,
        )
        files_changed = []
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if "\t" in line:
                    parts = line.split("\t", 1)
                    files_changed.append(parts[1])

        # Get diff stats
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=self.worktree_path,
            capture_output=True,
            text=True,
        )
        stats = {"insertions": 0, "deletions": 0, "lines": 0}
        if result.stdout:
            # Parse final line: " 5 files changed, 10 insertions(+), 3 deletions(-)"
            lines = result.stdout.strip().split("\n")
            summary = lines[-1] if lines else ""
            insertions_match = re.search(r"(\d+) insertions?", summary)
            deletions_match = re.search(r"(\d+) deletions?", summary)
            if insertions_match:
                stats["insertions"] = int(insertions_match.group(1))
            if deletions_match:
                stats["deletions"] = int(deletions_match.group(1))
            stats["lines"] = stats["insertions"] + stats["deletions"]

        return {
            "files_changed": files_changed,
            "insertions": stats["insertions"],
            "deletions": stats["deletions"],
            "lines": stats["lines"],
        }

    def _derive_pytest_targets(self, changed_files: list[str]) -> list[str]:
        """Auto-derive pytest targets from changed files (§5).

        For each changed file under omniagentos/, find corresponding test_*.py
        or *_test.py. If file is already a test, include it directly.
        """
        targets = set()

        for changed in changed_files:
            if "/test" in changed or changed.startswith("test"):
                # Already a test file
                targets.add(changed)
            elif changed.endswith(".py"):
                # Guess test file location
                # omniagentos/foo/bar.py -> tests/foo/test_bar.py
                parts = changed.replace("omniagentos/", "").replace(".py", "").split("/")
                if len(parts) > 1:
                    test_path = os.path.join("tests", *parts[:-1], f"test_{parts[-1]}.py")
                    targets.add(test_path)
                else:
                    test_path = os.path.join("tests", f"test_{parts[0]}.py")
                    targets.add(test_path)

        # Filter to files that exist in worktree
        valid_targets = []
        for target in sorted(targets):
            target_path = os.path.join(self.worktree_path, target)
            if os.path.exists(target_path):
                valid_targets.append(target)

        return valid_targets

    def _pytest_executable(self) -> list[str]:
        """Prefer the repo's pinned venv python; fall back to the running one."""
        venv = os.path.join(self.repo_dir, ".venv", "bin", "python")
        if os.path.exists(venv):
            return [venv]
        import sys

        return [sys.executable or "python3"]

    def _run_pytest(self, pytest_targets: list[str]) -> dict[str, Any]:
        """Run pytest with scrubbed env + a HARD deadline enforced by process-group
        kill (§5b.6, B1).

        ``subprocess.run(timeout=…)`` alone leaves orphaned children behind on a
        deadline, so the pytest process is launched in its OWN session/process group
        (``start_new_session=True`` ⇒ ``setsid``) and, on deadline, the WHOLE group
        is signalled (SIGTERM, then SIGKILL). Any grandchild pytest spawned inside
        the sandbox dies with it.

        Returns:
            {"passed": int, "failed": int, "count": int, "errors": list[str], "timed_out": bool}
        """
        env = self._scrub_env()

        pytest_cmd = [
            *self._pytest_executable(),
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *pytest_targets,
        ]

        # Redirect scratch writes into the worktree so the OS sandbox can confine
        # ALL writes to it, then deny network (red-team B1: proposal-authored
        # repro tests run here BEFORE any judge or human sees them). SBPL is
        # last-match-wins, so the appended deny overrides the base profile's
        # network allowance.
        #
        # H-40: fail CLOSED. Setup/profile errors must NOT fall back to running
        # proposal-authored tests unconfined — that path executes untrusted code
        # pre-judge. Missing OS sandbox support and profile construction failure
        # both refuse execution.
        tmp_dir = os.path.join(self.worktree_path, ".sandbox-tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        env["TMPDIR"] = tmp_dir
        pytest_cmd = self._confine_pytest_command(pytest_cmd)

        proc = subprocess.Popen(
            pytest_cmd,
            cwd=self.worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,  # own process group ⇒ killpg reaches every child
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=self.pytest_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", "pytest killed on deadline (process group)"

        output = (stdout or "") + (stderr or "")
        passed = failed = 0
        for line in output.split("\n"):
            if " passed" in line or " failed" in line:
                pm = re.search(r"(\d+) passed", line)
                fm = re.search(r"(\d+) failed", line)
                if pm:
                    passed = int(pm.group(1))
                if fm:
                    failed = int(fm.group(1))

        errors: list[str] = []
        if timed_out:
            errors = ["Pytest execution exceeded deadline"]
        elif proc.returncode == 5:
            errors = ["pytest exit 5: no tests were collected for the derived targets"]
        elif proc.returncode not in (0, None):
            src = stderr if stderr and stderr.strip() else stdout
            errors = [line for line in (src or "").split("\n") if line.strip()][-3:]

        return {
            "passed": passed,
            "failed": failed,
            "count": passed + failed,
            "errors": errors,
            "timed_out": timed_out,
            "returncode": proc.returncode,
        }

    def _confine_pytest_command(self, pytest_cmd: list[str]) -> list[str]:
        """Wrap pytest in the OS sandbox or refuse to run (H-40 fail-closed).

        Raises:
            RuntimeError: if the OS sandbox is unavailable or profile build fails.
        """
        from omniagentos.runner import sandbox as os_sandbox

        if not os_sandbox.sandbox_available():
            raise RuntimeError(
                "sandbox_unavailable: OS sandbox is required for proposal-authored "
                "pytest; refusing unconfined execution"
            )
        try:
            profile = os_sandbox.build_profile(self.worktree_path) + "\n(deny network*)"
        except Exception as exc:  # noqa: BLE001 — surface as sandbox setup failure
            raise RuntimeError(
                f"sandbox_profile_failed: could not construct confinement profile: {exc}"
            ) from exc
        if not str(profile).strip():
            raise RuntimeError("sandbox_profile_failed: empty confinement profile")
        return ["/usr/bin/sandbox-exec", "-p", profile, *pytest_cmd]

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """SIGTERM then SIGKILL the child's whole process group (§5b.6)."""
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                return
            time.sleep(0.2)
            if proc.poll() is not None:
                return

    def _scrub_env(self) -> dict[str, str]:
        """Create scrubbed environment (no secrets, limited PATH).

        Removes:
          - OMNIAGENTOS_*SECRET* vars
          - OMNIAGENTOS_*CONNECTIONS vars
          - Any var with value containing /var/secrets/

        Keeps:
          - PATH (for git, python, etc.)
          - HOME (for ~ expansion)
          - PYTHONPATH (if set)
          - Standard vars (LC_*, LANG, TZ)
        """
        # TRUE allowlist (red-team B1): the launchd jobs source
        # ~/.config/omni/connections.env (live Stripe/PayPal/Meta keys, none
        # OMNIAGENTOS_-prefixed), so anything short of drop-by-default leaks
        # production secrets into proposal-authored pytest. Nothing outside the
        # allowlist survives, regardless of prefix.
        allowlist = {"PATH", "HOME", "PYTHONPATH", "LANG", "TZ", "TERM"}
        env = {
            key: value
            for key, value in os.environ.items()
            if (key in allowlist or key.startswith("LC_")) and "var/secrets" not in (value or "")
        }
        return env

    def _cleanup_worktree(self) -> None:
        """Remove git worktree + directory (always call, even on error)."""
        import shutil

        # Remove worktree via git
        subprocess.run(
            ["git", "worktree", "remove", "--force", self.worktree_path],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )

        # Forcefully remove directory if still exists
        if os.path.exists(self.worktree_path):
            shutil.rmtree(self.worktree_path, ignore_errors=True)


def _declared_paths(proposal: dict[str, Any]) -> list[str]:
    """Repo-relative paths a proposal declares it will touch (files + config_edits)."""
    paths: list[str] = []
    for f in proposal.get("files", []) or []:
        if isinstance(f, dict) and f.get("path"):
            paths.append(str(f["path"]))
        elif isinstance(f, str) and f:
            paths.append(f)
    for edit in proposal.get("config_edits", []) or []:
        if isinstance(edit, dict) and edit.get("path"):
            paths.append(str(edit["path"]))
    return paths


class PipelineSandboxRunner:
    """Adapts :class:`Sandbox` to the pipeline's ``SandboxRunner`` seam (§5).

    ``run(improvement, repo_root)`` returns a ``pipeline.SandboxOutcome`` carrying the
    AUTHORITATIVE diff (``governance.build_diff_entries`` over the worktree, so
    symlinks/renames resolve to their true targets — B2), the declared paths (for the
    pipeline's declared-vs-actual mismatch check), a serialized report, and the
    analyzer's suggested risk (which governance may only ever RAISE).

    Only declared file diffs + schema-validated config edits are applied in the
    worktree; no proposal command is ever executed (B1).
    """

    def __init__(self, sandbox_dir: str | None = None, pytest_timeout_seconds: int = 300):
        self.sandbox_dir = sandbox_dir
        self.pytest_timeout_seconds = pytest_timeout_seconds

    def run(self, improvement: Any, repo_root: str) -> Any:
        # Imported here (not at module top) purely to keep the import graph obviously
        # acyclic; the pipeline does not import the sandbox.
        from omniagentos.reliability.pipeline import SandboxOutcome

        proposal = getattr(improvement, "proposal_json", None) or {}
        declared = _declared_paths(proposal)
        suggested = int(proposal.get("risk_suggestion") or 1 or 1)

        sb = Sandbox(
            repo_root,
            sandbox_dir=self.sandbox_dir,
            pytest_timeout_seconds=self.pytest_timeout_seconds,
        )
        result = SandboxResult(id=sb.sandbox_id, status="error")
        diff_entries: list[DiffEntry] = []
        raw_diff = ""
        passed = False

        try:
            sb._prepare_worktree()
            result.status = "green"

            for f in proposal.get("files", []) or []:
                if isinstance(f, dict) and f.get("path"):
                    sb._apply_file_diff(f)
            for edit in proposal.get("config_edits", []) or []:
                self._apply_config_edit(sb.worktree_path, edit)

            gi = sb._get_git_diff()
            result.git_diff_lines = gi["lines"]
            result.git_diff_files_changed = gi["files_changed"]
            result.git_diff_insertions = gi["insertions"]
            result.git_diff_deletions = gi["deletions"]

            # Build the AUTHORITATIVE diff (commit the declared change) BEFORE running
            # pytest, so test byproducts (__pycache__/*.pyc) never leak into the diff
            # and cannot be mistaken for an undeclared-path change (which would force L4).
            diff_entries, raw_diff = self._authoritative_diff(sb.worktree_path)

            targets = sb._derive_pytest_targets(gi["files_changed"])
            if targets:
                tr = sb._run_pytest(targets)
                result.tests_passed = tr["passed"]
                result.tests_failed = tr["failed"]
                result.test_count = tr["count"]
                result.test_errors = tr["errors"]
                if tr.get("timed_out"):
                    result.status = "timeout"
                elif tr["failed"] > 0 or tr.get("returncode") != 0:
                    result.status = "red"
                # Exit-code-only grading: 0 guarantees at least one test was
                # collected and every test passed (exit 5 = none collected).
                passed = not tr.get("timed_out") and tr.get("returncode") == 0
            else:
                # No test surface touched — green with no assertion of coverage.
                passed = True
        except Exception as exc:  # noqa: BLE001 - a sandbox error is a failed sandbox, never judged
            result.status = "error"
            result.test_errors = [str(exc)]
            result.stderr_tail = str(exc)
            passed = False
        finally:
            sb._cleanup_worktree()

        report = result.to_json()
        report["git_diff"] = raw_diff
        report["declared_paths"] = declared
        return SandboxOutcome(
            passed=passed,
            diff_entries=diff_entries,
            declared_paths=declared,
            report=report,
            suggested_risk=suggested,
        )

    @staticmethod
    def _authoritative_diff(worktree: str) -> tuple[list[DiffEntry], str]:
        """Commit the applied changes and build the realpath-resolved diff range.

        ``build_diff_entries`` needs a commit range; the working-tree changes are
        committed in the throwaway worktree so ``HEAD^..HEAD`` is the exact declared
        change. A clean tree (nothing applied) yields an empty diff.
        """
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
        ).stdout.strip()
        if not status:
            return [], ""
        subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, text=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=sandbox@omni",
                "-c",
                "user.name=sandbox",
                "commit",
                "--no-verify",
                "-m",
                "sandbox: apply declared change",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        entries = build_diff_entries(worktree, "HEAD^", "HEAD")
        raw_diff = subprocess.run(
            ["git", "diff", "HEAD^", "HEAD"], cwd=worktree, capture_output=True, text=True
        ).stdout
        return entries, raw_diff

    @staticmethod
    def _apply_config_edit(worktree: str, edit: dict[str, Any]) -> None:
        """Apply a schema-validated dotted-key config edit inside the worktree.

        Mirrors the pipeline's apply-time config edit so the sandbox diff faithfully
        reflects config changes (a config edit to a Tier-P yaml must show up in the
        authoritative diff so governance classifies it L4). Refuses paths outside
        ``configs/`` and non-scalar values (B1).
        """
        rel = edit.get("path")
        if not rel:
            return
        if not str(rel).replace("\\", "/").startswith("configs/"):
            raise ValueError(f"config_edit path must live under configs/: {rel}")
        sets = edit.get("set")
        if not isinstance(sets, dict):
            raise ValueError("config_edit requires a 'set' mapping")
        abs_path = os.path.abspath(os.path.join(worktree, rel))
        if inode_relative_parts_anchored(abs_path, os.path.abspath(worktree)) is None:
            raise ValueError(f"config_edit escapes worktree: {rel}")

        import yaml

        data: dict[str, Any] = {}
        if os.path.exists(abs_path):
            data = yaml.safe_load(Path(abs_path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"config target is not a mapping: {rel}")
        for dotted, value in sets.items():
            node = data
            keys = str(dotted).split(".")
            for k in keys[:-1]:
                nxt = node.get(k)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[k] = nxt
                node = nxt
            node[keys[-1]] = value
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        Path(abs_path).write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def validate_proposal_commands(proposal_json: dict[str, Any]) -> None:
    """Validate that proposal doesn't embed dangerous commands (B1).

    Proposal.plan[] is narrative only; no commands execute. This function
    scans for suspicious embedded shell patterns and raises if found.

    Args:
        proposal_json: Proposal dict (from analyzer)

    Raises:
        ValueError: If suspicious command patterns detected
    """
    plan = proposal_json.get("plan", [])

    # Look for shell-like patterns in plan narrative
    suspicious_patterns = [
        r"\$\(",  # Command substitution
        r"`.*`",  # Backtick substitution
        r"&&\s*(rm|dd|mkfs|truncate|shred)",  # Destructive commands
        r"\|\s*bash",  # Pipe to bash
    ]

    for item in plan:
        if isinstance(item, str):
            for pattern in suspicious_patterns:
                if re.search(pattern, item):
                    raise ValueError(f"Suspicious command pattern in plan: {item}")


__all__ = [
    "Sandbox",
    "SandboxResult",
    "PipelineSandboxRunner",
    "validate_proposal_commands",
]
