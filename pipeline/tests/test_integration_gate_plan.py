"""Focused tests for Integration's remote gate-host plan."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import integration as I  # noqa: E402
from bridge.gate_host import (  # noqa: E402
    GATE_LADDER_WORKERS,
    REMOTE_EVIDENCE_ROOT,
    REMOTE_GATE_WORKSPACE,
    TWIN_HOST,
    HostChoice,
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
        )
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "tester")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _candidate_with_stale_declared_base(repo: Path) -> tuple[I.Candidate, str, str]:
    """Feature merges newer main, making main's new tip the measured base."""
    declared_base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-qb", "feature")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-qm", "feature")

    _git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("new main\n", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-qm", "advance main")
    measured_base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "--no-ff", "-qm", "merge newer main", "main")
    tip_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    assert measured_base != declared_base
    assert _git(repo, "merge-base", "main", tip_sha) == measured_base
    cand = I.Candidate(
        "sha256:" + "a" * 64,
        Path("candidate.json"),
        {},
        paths=["feature.txt"],
        branch="feature",
        base_sha=declared_base,
        tip_sha=tip_sha,
    )
    return cand, declared_base, measured_base


def _remote_choice() -> HostChoice:
    return HostChoice(
        host="test-twin",
        reason="test routes remotely",
        workspace="/remote/gate",
        evidence_root="/remote/evidence",
    )


def _default_twin_choice_no_paths() -> HostChoice:
    """Registry gave no per-box paths — the constructor overrides must win."""
    return HostChoice(host=TWIN_HOST, reason="default twin, no registry paths")


def _pool_choice() -> HostChoice:
    """A pool box that carries its OWN registry paths (not the default twin's)."""
    return HostChoice(
        host="pool-host-2",
        reason="pool router picked a second box",
        workspace="/pool/gate-2",
        evidence_root="/pool/evidence-2",
    )


def _simple_candidate(repo: Path) -> I.Candidate:
    return I.Candidate(
        "sha256:" + "b" * 64,
        Path("candidate.json"),
        {},
        paths=["feature.txt"],
        branch="feature",
        base_sha=_git(repo, "rev-parse", "HEAD"),
        tip_sha=_git(repo, "rev-parse", "HEAD"),
    )


def _integration(tmp_path: Path, repo: Path) -> I.Integration:
    return I.Integration(
        root=tmp_path / "loops",
        repo=repo,
        gate_ws=tmp_path / "gate",
        schema_dir=ROOT / "schema",
        apply=False,
        allow_remote_gate=True,
    )


def test_remote_plan_pins_locally_measured_base_and_records_stale_declaration(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path / "repo")
    cand, declared_base, measured_base = _candidate_with_stale_declared_base(repo)
    integration = _integration(tmp_path, repo)
    monkeypatch.setattr(I, "choose_gate_host", lambda _paths: _remote_choice())

    pins: list[dict] = []

    def _pin(host, workspace, branch, sha, *, local_repo, checkout=False):
        pins.append(
            {
                "host": host,
                "workspace": workspace,
                "branch": branch,
                "sha": sha,
                "local_repo": local_repo,
                "checkout": checkout,
            }
        )
        return {"ok": True, "why": "pinned"}

    preflights: list[dict] = []

    def _preflight(host, **kwargs):
        preflights.append({"host": host, **kwargs})
        return {"ready": True, "failed": []}

    monkeypatch.setattr(I, "pin_remote_candidate", _pin)
    monkeypatch.setattr(I, "preflight_remote", _preflight)

    plan = integration.plan_gate_host(cand)

    assert pins[0]["branch"] == "gate-pinned-main"
    assert pins[0]["sha"] == measured_base
    assert pins[0]["sha"] != declared_base
    assert plan["base_pin"]["measured_base"] == measured_base
    assert plan["base_pin"]["declared_base_mismatch"] == declared_base
    assert preflights == [
        {
            "host": "test-twin",
            "workspace": "/remote/gate",
            "evidence_root": "/remote/evidence",
            "candidate": "feature",
            "expected_base": measured_base,
        }
    ]
    assert plan["dispatched"] == "test-twin"


def test_remote_plan_merge_base_failure_falls_back_without_pin(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    cand, _, _ = _candidate_with_stale_declared_base(repo)
    integration = _integration(tmp_path, repo)
    monkeypatch.setattr(I, "choose_gate_host", lambda _paths: _remote_choice())
    monkeypatch.setattr(I, "git", lambda *_args, **_kwargs: (128, "", "probe failed"))
    monkeypatch.setattr(
        I,
        "pin_remote_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a failed merge-base probe must not attempt a remote pin")
        ),
    )
    monkeypatch.setattr(
        I,
        "preflight_remote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a failed merge-base probe must not attempt preflight")
        ),
    )

    plan = integration.plan_gate_host(cand)

    assert plan["dispatched"] == "local"
    assert "could not measure the merge base locally" in plan["why_not_remote"]
    assert "instrument, not candidate" in plan["why_not_remote"]
    assert "base_pin" not in plan


def test_plan_writes_resolved_workspace_default_twin_constructor_overrides(
    tmp_path, monkeypatch
):
    """AC6b / BLOCKER 4, default-twin case: the router returned no per-box
    paths, so plan_gate_host resolves twin_ws/twin_evidence from the
    CONSTRUCTOR overrides. record["workspace"]/["evidence_root"] must carry
    that resolved value (not the raw, path-less choice.as_dict()), and it must
    be exactly what pin/preflight were called with.
    """
    repo = _init_repo(tmp_path / "repo")
    cand = _simple_candidate(repo)
    override_ws = tmp_path / "override-gate-ws"
    override_evidence = tmp_path / "override-evidence-root"
    integration = I.Integration(
        root=tmp_path / "loops",
        repo=repo,
        gate_ws=tmp_path / "gate",
        schema_dir=ROOT / "schema",
        apply=False,
        allow_remote_gate=True,
        remote_gate_ws=str(override_ws),
        remote_evidence_root=str(override_evidence),
    )
    assert str(override_ws) != REMOTE_GATE_WORKSPACE
    assert str(override_evidence) != REMOTE_EVIDENCE_ROOT
    monkeypatch.setattr(I, "choose_gate_host", lambda _paths: _default_twin_choice_no_paths())
    monkeypatch.setattr(I, "git", lambda *_a, **_k: (0, cand.tip_sha, ""))

    pins: list[dict] = []
    preflights: list[dict] = []
    monkeypatch.setattr(
        I, "pin_remote_candidate",
        lambda host, workspace, branch, sha, **kw: (
            pins.append({"host": host, "workspace": workspace, "branch": branch, "sha": sha})
            or {"ok": True, "why": "pinned"}
        ),
    )
    monkeypatch.setattr(
        I, "preflight_remote",
        lambda host, **kw: (
            preflights.append({"host": host, **kw}) or {"ready": True, "failed": []}
        ),
    )

    plan = integration.plan_gate_host(cand)

    assert plan["workspace"] == str(override_ws)
    assert plan["evidence_root"] == str(override_evidence)
    assert all(p["workspace"] == str(override_ws) for p in pins)
    assert all(p["evidence_root"] == str(override_evidence) for p in preflights)

    captured_cmd: list = []

    def _capture_subprocess_run(cmd, *a, **k):
        captured_cmd.extend(cmd)
        raise OSError("no execution in this test")

    monkeypatch.setattr(
        I.subprocess, "run",
        _capture_subprocess_run,
    )
    integration.run_gate(cand)
    actual_argv = " ".join(map(str, captured_cmd))
    assert str(override_ws) in actual_argv
    assert str(override_evidence) in actual_argv


def test_plan_writes_resolved_workspace_pool_choice(tmp_path, monkeypatch):
    """AC6b / BLOCKER 4, pool case: the router picked a non-default twin that
    carries its OWN registry paths. record["workspace"]/["evidence_root"]
    must carry THAT box's paths, and run_gate's remote_gate_command call must
    read the same workspace off the plan (not the instance defaults, which
    would send this box to the default twin's filesystem).
    """
    repo = _init_repo(tmp_path / "repo")
    cand = _simple_candidate(repo)
    integration = _integration(tmp_path, repo)
    monkeypatch.setattr(I, "choose_gate_host", lambda _paths: _pool_choice())
    monkeypatch.setattr(I, "git", lambda *_a, **_k: (0, cand.tip_sha, ""))
    monkeypatch.setattr(I, "pin_remote_candidate", lambda *a, **kw: {"ok": True, "why": "pinned"})
    monkeypatch.setattr(
        I, "preflight_remote", lambda *a, **kw: {"ready": True, "failed": []}
    )

    plan = integration.plan_gate_host(cand)

    assert plan["workspace"] == "/pool/gate-2"
    assert plan["evidence_root"] == "/pool/evidence-2"

    # run_gate must dispatch its remote_gate_command using the PLAN's
    # resolved workspace, not self.remote_gate_ws (the default twin's path).
    captured: dict = {}
    real_remote_gate_command = I.remote_gate_command

    def _capture_remote_gate_command(host, **kwargs):
        captured["host"] = host
        captured.update(kwargs)
        return real_remote_gate_command(host, **kwargs)

    monkeypatch.setattr(I, "remote_gate_command", _capture_remote_gate_command)
    monkeypatch.setattr(
        I.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no execution in this test"))
    )

    verdict = integration.run_gate(cand)

    assert captured["host"] == "pool-host-2"
    assert captured["workspace"] == "/pool/gate-2"
    assert captured["evidence_root"] == "/pool/evidence-2"
    assert captured["timeout_s"] == I.GATE_TIMEOUT_SECONDS - 300
    assert verdict.result == "instrument-error"


def test_run_gate_extra_env_uses_gate_ladder_workers_single_source_of_truth(
    tmp_path, monkeypatch
):
    """Design M4: no more literal "8" — extra_env falls back to
    str(GATE_LADDER_WORKERS) imported from bridge.gate_host, not a duplicated
    literal, when MERGE_GATE_LADDER_WORKERS is unset in the environment.
    """
    repo = _init_repo(tmp_path / "repo")
    cand = _simple_candidate(repo)
    integration = _integration(tmp_path, repo)
    monkeypatch.setattr(I, "choose_gate_host", lambda _paths: _remote_choice())
    monkeypatch.setattr(I, "git", lambda *_a, **_k: (0, cand.tip_sha, ""))
    monkeypatch.setattr(I, "pin_remote_candidate", lambda *a, **kw: {"ok": True, "why": "pinned"})
    monkeypatch.setattr(
        I, "preflight_remote", lambda *a, **kw: {"ready": True, "failed": []}
    )
    monkeypatch.delenv("MERGE_GATE_LADDER_WORKERS", raising=False)

    captured: dict = {}
    real_remote_gate_command = I.remote_gate_command

    def _capture_remote_gate_command(host, **kwargs):
        captured.update(kwargs)
        return real_remote_gate_command(host, **kwargs)

    monkeypatch.setattr(I, "remote_gate_command", _capture_remote_gate_command)
    monkeypatch.setattr(
        I.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no execution in this test"))
    )

    integration.run_gate(cand)

    assert captured["extra_env"]["MERGE_GATE_LADDER_WORKERS"] == str(GATE_LADDER_WORKERS)


def test_run_gate_i6_timeout_produces_gtimeout_wrapper_in_argv(tmp_path, monkeypatch):
    """R3: integration keeps 300s setup slack inside its local wait timeout.
    to remote_gate_command, so the ACTUAL argv it builds wraps the inner
    command in the gtimeout self-bound (the twin bounds itself, I6).
    """
    repo = _init_repo(tmp_path / "repo")
    cand = _simple_candidate(repo)
    integration = _integration(tmp_path, repo)
    monkeypatch.setattr(I, "choose_gate_host", lambda _paths: _remote_choice())
    monkeypatch.setattr(I, "git", lambda *_a, **_k: (0, cand.tip_sha, ""))
    monkeypatch.setattr(I, "pin_remote_candidate", lambda *a, **kw: {"ok": True, "why": "pinned"})
    monkeypatch.setattr(
        I, "preflight_remote", lambda *a, **kw: {"ready": True, "failed": []}
    )

    captured_cmd: list = []

    def _capture_subprocess_run(cmd, *a, **k):
        captured_cmd.extend(cmd)
        raise OSError("no execution in this test")

    monkeypatch.setattr(I.subprocess, "run", _capture_subprocess_run)

    integration.run_gate(cand)

    inner = captured_cmd[-1]
    assert f"gtimeout -k 30 {I.GATE_TIMEOUT_SECONDS - 300} " in inner
