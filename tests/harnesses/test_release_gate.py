"""H-30 — focused tests for the immutable pinned-SHA release gate harness.

These tests never run the real full gate. They inject a command runner that
simulates git and phase commands so we can prove:

- dirty HEAD is refused;
- moving HEAD is refused;
- named phases are stable and ordered;
- phase evidence is recorded;
- dry-run plans without executing phase payloads.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from omniagentos.harnesses import release_gate as rg


class FakeGitWorld:
    """Minimal fake git + command world for the release gate."""

    def __init__(self, *, head: str = "a" * 40, dirty: bool = False) -> None:
        self.head = head
        self.dirty = dirty
        self.commands: list[tuple[str, ...]] = []
        self.phase_exit: dict[str, int] = {}
        self.move_head_after: str | None = None  # phase name after which HEAD moves
        self.dirty_after: str | None = None  # phase name after which tree becomes dirty
        self._phase_hits: set[str] = set()

    def runner(self, argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        args = tuple(argv)
        self.commands.append(args)

        # git -C <root> ...
        if len(args) >= 4 and args[0] == "git" and args[1] == "-C":
            git_args = args[3:]
            if git_args == ("rev-parse", "HEAD"):
                return 0, self.head + "\n"
            if git_args == ("status", "--porcelain"):
                if self.dirty:
                    return 0, " M omniagentos/harnesses/release_gate.py\n"
                return 0, ""
            return 1, f"unexpected git args: {git_args}"

        # Phase payloads — identify by distinctive argv fragments.
        name = self._phase_name_from_argv(args)
        if name and self.move_head_after == name and name in self._phase_hits:
            # Already completed once; subsequent git checks should see movement.
            pass
        if name:
            self._phase_hits.add(name)
            code = self.phase_exit.get(name, 0)
            # Mutate world *after* the phase command returns, before next pin check.
            if self.move_head_after == name:
                self.head = "b" * 40
            if self.dirty_after == name:
                self.dirty = True
            return code, f"ok:{name}\n" if code == 0 else f"fail:{name}\n"

        return 0, "ok\n"

    @staticmethod
    def _phase_name_from_argv(args: tuple[str, ...]) -> str | None:
        joined = " ".join(args)
        # Order matters: more specific first.
        if "ruff format" in joined or (len(args) >= 3 and args[-2:] == ("--check", ".")):
            if "ruff" in joined and "format" in joined:
                return "format"
        if "ruff" in joined and "check" in joined:
            return "ruff"
        if "mypy" in joined:
            return "mypy"
        if "omniagentos.db.migrate" in joined:
            return "migrations"
        if rg.API_CONTRACT_TEST in joined:
            return "api_contracts"
        if "npm" in joined and "audit" in joined:
            return "dependency_scan"
        if "vitest" in joined or ("npm" in joined and args[-1:] == ("test",)):
            return "dashboard_unit"
        if "npm" in joined and "lint" in joined:
            return "dashboard_lint"
        if "npm" in joined and "build" in joined:
            return "dashboard_build"
        if "e2e.sh" in joined:
            return "e2e"
        if "-m" in args and "smoke" in joined:
            return "live_restart"
        if "-m" in args and "perf" in joined:
            return "load_contention"
        if "pytest" in joined:
            return "backend"
        return None


def test_named_phases_cover_required_release_surface() -> None:
    """Full gate phase names are a stable contract (H-30)."""
    specs = rg.default_phase_specs(python="python", repo_root=Path("/tmp/repo"))
    names = [s.name for s in specs]
    assert names == list(rg.ALL_PHASE_NAMES)
    for required in (
        "backend",
        "dashboard_unit",
        "dashboard_lint",
        "dashboard_build",
        "e2e",
        "ruff",
        "format",
        "mypy",
        "migrations",
        "api_contracts",
        "dependency_scan",
        "live_restart",
        "load_contention",
    ):
        assert required in names


def test_refuses_dirty_tree(tmp_path: Path) -> None:
    world = FakeGitWorld(dirty=True)
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        dry_run=True,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "refused"
    assert "dirty" in evidence.refuse_reason.lower()
    assert evidence.phases == []
    written = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert written["status"] == "refused"


def test_refuses_moving_head_between_phases(tmp_path: Path) -> None:
    world = FakeGitWorld(dirty=False)
    world.move_head_after = "ruff"
    world.phase_exit["ruff"] = 0
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff", "format"],
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
        # Force conditional flags irrelevant — we selected explicit phases.
    )
    assert evidence.status == "refused"
    assert evidence.pinned_sha == "a" * 40
    assert any(p.status == "refused" for p in evidence.phases)
    assert "moved" in evidence.refuse_reason.lower() or any(
        "moved" in p.detail.lower() for p in evidence.phases
    )


def test_dry_run_plans_without_executing_phase_payloads(tmp_path: Path) -> None:
    world = FakeGitWorld(dirty=False)
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff", "format", "mypy"],
        dry_run=True,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "planned"
    assert evidence.pinned_sha == "a" * 40
    assert [p.name for p in evidence.phases] == ["ruff", "format", "mypy"]
    assert all(p.status == "planned" for p in evidence.phases)
    # Only git commands — no pytest/npm/ruff payload execution.
    non_git = [c for c in world.commands if not (c and c[0] == "git")]
    assert non_git == []


def test_phase_failure_records_evidence_and_fails_gate(tmp_path: Path) -> None:
    world = FakeGitWorld(dirty=False)
    world.phase_exit["ruff"] = 1
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff", "format"],
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
        stop_on_failure=True,
    )
    assert evidence.status == "failed"
    assert evidence.phases[0].name == "ruff"
    assert evidence.phases[0].status == "failed"
    assert evidence.phases[0].exit_code == 1
    # Stopped before format.
    assert [p.name for p in evidence.phases] == ["ruff"]


def test_successful_subset_records_phase_evidence_but_does_not_certify(tmp_path: Path) -> None:
    """Every phase green under an injected runner is ``simulated``, not ``passed``.

    The runner replaced the subprocess world, so no real command ran and no
    interpreter was verified. Recording that as ``passed`` would make this
    file's evidence indistinguishable from a genuine certification sitting in
    the same directory — so the phase results are kept in full, and only the
    overall claim is withheld.
    """
    world = FakeGitWorld(dirty=False)
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff", "format"],
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "simulated"
    assert evidence.interpreter_verified is False
    assert evidence.pinned_sha == "a" * 40
    assert [p.status for p in evidence.phases] == ["passed", "passed"]
    assert all(p.head_sha_at_start == evidence.pinned_sha for p in evidence.phases)
    assert all(p.head_sha_at_end == evidence.pinned_sha for p in evidence.phases)
    data = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert data["pinned_sha"] == "a" * 40
    assert data["status"] == "simulated"
    assert data["interpreter_verified"] is False
    assert len(data["phases"]) == 2


def test_full_gate_with_skipped_conditionals_is_not_certified(tmp_path: Path) -> None:
    """Silent omission is forbidden: skips fail the full gate."""
    world = FakeGitWorld(dirty=False)
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=None,  # full plan
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
        env={
            "RELEASE_GATE_LIVE": "0",
            "RELEASE_GATE_LOAD": "0",
        },
    )
    assert evidence.status == "failed"
    skipped = [p.name for p in evidence.phases if p.status == "skipped"]
    assert "live_restart" in skipped
    assert "load_contention" in skipped


def test_cli_list_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = rg.main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    for name in rg.ALL_PHASE_NAMES:
        assert name in out


def test_unknown_phase_refuses(tmp_path: Path) -> None:
    world = FakeGitWorld()
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["not_a_real_phase"],
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "refused"
    assert "unknown phase" in evidence.refuse_reason.lower()


def test_refuses_mid_run_dirty_after_phase(tmp_path: Path) -> None:
    """Mid-run dirtiness (tree becomes dirty after a phase) must refuse."""
    world = FakeGitWorld(dirty=False)
    world.dirty_after = "ruff"  # Tree becomes dirty after ruff completes
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff", "format"],
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "refused"
    # The ruff phase should have recorded the dirty-tree refusal in its evidence.
    ruff_ev = next((p for p in evidence.phases if p.name == "ruff"), None)
    assert ruff_ev is not None
    assert ruff_ev.status == "refused"
    assert "dirty" in ruff_ev.detail.lower() or "dirty" in evidence.refuse_reason.lower()


def test_refuses_final_head_movement(tmp_path: Path) -> None:
    """HEAD movement after all phases but before final check must refuse."""
    world = FakeGitWorld(dirty=False)
    # HEAD moves after the last phase (format), caught by final provenance check
    world.move_head_after = "format"
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff", "format"],
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    # Could be refused at phase end or at final check — either way, refused.
    assert evidence.status == "refused"
    assert "moved" in evidence.refuse_reason.lower() or any(
        "moved" in p.detail.lower() for p in evidence.phases if p.detail
    )


def test_allow_dirty_evidence_is_recorded(tmp_path: Path) -> None:
    """allow_dirty=True must be recorded in evidence for audit trail."""
    world = FakeGitWorld(dirty=True)
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        dry_run=False,
        allow_dirty=True,  # Debug override
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    # Gate proceeds (dirty is allowed); evidence records the override.
    assert evidence.allow_dirty is True
    data = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert data["allow_dirty"] is True


def test_phase_exit_code_is_preserved_in_evidence(tmp_path: Path) -> None:
    """A phase's raw exit code survives into evidence (main()'s rc is covered
    end-to-end in tests/harnesses/test_release_gate_cli.py)."""
    world = FakeGitWorld(dirty=False)
    world.phase_exit["ruff"] = 42
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "failed"
    assert evidence.phases[0].exit_code == 42


def test_payload_existence_e2e_script(tmp_path: Path) -> None:
    """e2e phase must fail if the script does not exist (payload existence)."""
    world = FakeGitWorld(dirty=False)

    # The e2e phase references scripts/smoke/e2e.sh which won't exist in tmp_path.
    # A default runner hitting a missing script should fail with FileNotFoundError
    # or similar. We simulate by having the runner return 127 (command not found).
    def runner_missing_script(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        args = tuple(argv)
        # Git commands work normally
        if len(args) >= 4 and args[0] == "git" and args[1] == "-C":
            git_args = args[3:]
            if git_args == ("rev-parse", "HEAD"):
                return 0, world.head + "\n"
            if git_args == ("status", "--porcelain"):
                return 0, ""
            return 1, f"unexpected git args: {git_args}"
        # e2e.sh doesn't exist
        if "e2e.sh" in " ".join(args):
            return 127, "bash: scripts/smoke/e2e.sh: No such file or directory\n"
        return 0, "ok\n"

    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["e2e"],
        dry_run=False,
        allow_dirty=False,
        runner=runner_missing_script,
        evidence_path=tmp_path / "ev.json",
        python="python",
    )
    assert evidence.status == "failed"
    assert evidence.phases[0].name == "e2e"
    assert evidence.phases[0].status == "failed"
    assert evidence.phases[0].exit_code == 127


def test_interpreter_error_refuses_gate(tmp_path: Path) -> None:
    """Gate must refuse with clear error when interpreter cannot be verified."""
    world = FakeGitWorld(dirty=False)
    # Pass an invalid interpreter path
    evidence = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        dry_run=False,
        allow_dirty=False,
        runner=world.runner,
        evidence_path=tmp_path / "ev.json",
        python=None,  # Will try to resolve
        env={"OMNIAGENTOS_PYTHON": "/nonexistent/python"},
    )
    assert evidence.status == "refused"
    assert (
        "OMNIAGENTOS_PYTHON" in evidence.refuse_reason or "does not exist" in evidence.refuse_reason
    )
