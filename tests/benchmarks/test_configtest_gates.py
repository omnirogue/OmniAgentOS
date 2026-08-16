from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.benchmarks import configtest_gates
from scripts.benchmarks.configtest_gates import (
    CONFIG_PATH,
    REQUIRED_TEST_ROOTS,
    SELF_PROTECTED_FILES,
    AntiSabotageGateError,
    ConfigTestConfigError,
    Status,
    bracket_for,
    deterministic_env,
    gate_build,
    gate_diff_scope,
    gate_existing_suite_unmodified,
    load_config,
    normalize_trace,
    settle_gate,
    trace_diff,
)


class GitRepo:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self._path = path
        self._env = env

    def __truediv__(self, other: str) -> Path:
        return self._path / other

    def __fspath__(self) -> str:
        return os.fspath(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def add(self, path: str) -> None:
        subprocess.run(["git", "add", str(path)], cwd=self._path, check=True, env=self._env)

    def commit(self, msg: str = "commit") -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                msg,
                "--allow-empty",
            ],
            cwd=self._path,
            check=True,
            env=self._env,
        )


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    subprocess.run(["git", "init", str(tmp_path)], check=True, env=env)
    return GitRepo(tmp_path, env)


def test_gate_build_passes_on_clean_files(repo: GitRepo) -> None:
    p = repo / "clean.py"
    p.write_text("print('hello')\n")
    repo.add("clean.py")
    passed, ev = gate_build(repo._path, ["clean.py"])
    assert passed is True
    assert ev["failures"] == []
    assert "clean.py" in ev["checked"]


def test_gate_build_catches_syntax_error(repo: GitRepo) -> None:
    p = repo / "bad.py"
    p.write_text("print('hello'\n")
    repo.add("bad.py")
    passed, ev = gate_build(repo._path, ["bad.py"])
    assert passed is False
    assert len(ev["failures"]) == 1
    assert ev["failures"][0]["path"] == "bad.py"
    assert ev["failures"][0]["lineno"] == 1
    assert "bad.py" in ev["failures"][0]["error"]
    assert "SyntaxError" in ev["failures"][0]["error"]


def test_gate_build_skips_deleted_and_non_py_files(repo: GitRepo) -> None:
    (repo / "deleted.py").write_text("a")
    (repo / "not_py.txt").write_text("a")
    repo.add(".")
    repo.commit("initial")
    (repo / "deleted.py").unlink()
    passed, ev = gate_build(repo._path, ["deleted.py", "not_py.txt"])
    assert passed is True
    assert "deleted.py" in ev["skipped"]
    assert "not_py.txt" in ev["skipped"]


def test_gate_build_leaves_no_pycache(repo: GitRepo) -> None:
    d = repo / "some_pkg"
    d.mkdir()
    p = d / "mod.py"
    p.write_text("print('hello')\n")
    repo.add("some_pkg/mod.py")
    passed, ev = gate_build(repo._path, ["some_pkg/mod.py"])
    assert passed is True

    # Walk the tree and assert that no __pycache__ directory exists anywhere under repo
    for root, dirs, _files in os.walk(str(repo)):
        assert "__pycache__" not in dirs
        assert "__pycache__" not in root


def test_gate_existing_suite_catches_modified_file(repo: GitRepo) -> None:
    tests = repo / "tests"
    tests.mkdir()
    f = tests / "test_a.py"
    f.write_text("A")
    repo.add(".")
    repo.commit("base")
    f.write_text("B")
    repo.add(".")
    repo.commit("head")

    passed, ev = gate_existing_suite_unmodified(repo._path, "HEAD~1")
    assert passed is False
    assert len(ev["violations"]) == 1
    assert ev["violations"][0]["path"] == "tests/test_a.py"


def test_gate_existing_suite_allows_new_test_file(repo: GitRepo) -> None:
    tests = repo / "tests"
    tests.mkdir()
    repo.commit("base")
    f = tests / "test_new.py"
    f.write_text("A")
    repo.add(".")

    passed, ev = gate_existing_suite_unmodified(repo._path, "HEAD")
    assert passed is True
    assert "tests/test_new.py" in ev["added"]


def test_gate_existing_suite_catches_deleted_file(repo: GitRepo) -> None:
    tests = repo / "tests"
    tests.mkdir()
    f = tests / "test_a.py"
    f.write_text("A")
    repo.add(".")
    repo.commit("base")
    f.unlink()
    repo.add(".")

    passed, ev = gate_existing_suite_unmodified(repo._path, "HEAD")
    assert passed is False
    assert len(ev["violations"]) == 1
    assert ev["violations"][0]["status"] == "D"


def test_gate_existing_suite_catches_renamed_file(repo: GitRepo) -> None:
    tests = repo / "tests"
    tests.mkdir()
    f = tests / "test_a.py"
    f.write_text("A")
    repo.add(".")
    repo.commit("base")
    f.rename(tests / "test_b.py")
    repo.add(".")

    passed, ev = gate_existing_suite_unmodified(repo._path, "HEAD")
    assert passed is False
    assert len(ev["violations"]) == 1
    assert ev["violations"][0]["status"] in ("R", "C")
    assert ev["violations"][0]["old_path"] == "tests/test_a.py"


def test_gate_diff_scope_catches_out_of_allowlist_path(repo: GitRepo) -> None:
    (repo / "out.py").write_text("A")
    repo.add(".")
    repo.commit("base")
    (repo / "out.py").write_text("B")
    repo.add(".")

    passed, ev = gate_diff_scope(repo._path, "HEAD", ["in.py"])
    assert passed is False
    assert "out.py" in ev["violations"]


def test_gate_diff_scope_passes_in_allowlist(repo: GitRepo) -> None:
    (repo / "a").mkdir()
    (repo / "a/b").mkdir()
    (repo / "a/b/c.py").write_text("C")
    (repo / "d.py").write_text("D")
    (repo / "exact.py").write_text("E")
    repo.add(".")

    repo.commit("base")

    (repo / "a/b/c.py").write_text("C2")
    (repo / "d.py").write_text("D2")
    (repo / "exact.py").write_text("E2")
    repo.add(".")

    passed, ev = gate_diff_scope(repo._path, "HEAD", ["a/b/", "*.py", "exact.py"])
    assert passed is True
    assert len(ev["violations"]) == 0


def test_gate_diff_scope_fails_closed_on_empty(repo: GitRepo) -> None:
    (repo / "out.py").write_text("A")
    repo.add(".")
    repo.commit("base")
    (repo / "out.py").write_text("B")
    repo.add(".")

    passed, ev = gate_diff_scope(repo._path, "HEAD", [])
    assert passed is False
    assert "out.py" in ev["violations"]


def test_gate_diff_scope_escape_paths(repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.benchmarks.configtest_gates import Change

    monkeypatch.setattr(
        "scripts.benchmarks.configtest_gates.collect_changes",
        lambda *args, **kwargs: [
            Change(path="/etc/passwd", status="M"),
            Change(path="a/../b.py", status="M"),
        ],
    )
    passed, ev = gate_diff_scope(repo._path, "HEAD", ["*"])
    assert passed is False
    assert "/etc/passwd" in ev["violations"]
    assert "a/../b.py" in ev["violations"]


def test_normalize_trace_and_trace_diff_equal() -> None:
    left = (
        "Error at 2026-07-27T18:21:03Z in /private/tmp/pytest-of-x/test_a0/mod.py:123\n"
        "UUID: 12345678-1234-1234-1234-1234567890ab\n"
        "Addr: 0xdeadBEEF"
    )
    right = (
        "Error at 2026-07-27 18:21:04 in /other/dir/test_a0/mod.py:456\n"
        "UUID: aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb\n"
        "Addr: 0x1234"
    )
    equal, ev = trace_diff(left, right)
    assert equal is True
    assert len(ev["diff"]) == 0


def test_normalize_trace_and_trace_diff_not_masked() -> None:
    left = "Exception: foo\nline 1"
    right = "Exception: bar\nline 1"
    equal, ev = trace_diff(left, right)
    assert equal is False
    assert any("foo" in line for line in ev["diff"])
    assert any("bar" in line for line in ev["diff"])


def test_normalize_trace_preserves_basenames() -> None:
    left = "raised in /a/mod.py:3"
    right = "raised in /b/other.py:9"
    norm_left = normalize_trace(left)
    norm_right = normalize_trace(right)
    assert norm_left != norm_right
    equal, ev = trace_diff(left, right)
    assert equal is False


def test_trace_diff_max_lines_zero_differing() -> None:
    left = "some trace content"
    right = "different trace content"
    equal, ev = trace_diff(left, right, max_lines=0)
    assert equal is False
    assert ev["truncated"] is True


def test_deterministic_env() -> None:
    from scripts.benchmarks.configtest_gates import DETERMINISM_ENV

    assert DETERMINISM_ENV.get("PYTHONHASHSEED") == "0"
    base = {"USER": "test", "OTHER": "val"}
    env = deterministic_env(base)
    assert env["PYTHONHASHSEED"] == "0"
    assert env["USER"] == "test"
    assert env["OTHER"] == "val"

    # Check default base (when None is passed)
    default_env = deterministic_env()
    assert default_env["PYTHONHASHSEED"] == "0"
    assert "PATH" in default_env


def test_load_config_validates_and_monotonic() -> None:
    conf = load_config(CONFIG_PATH)
    for cat in ["docs", "config", "tests", "code", "infra"]:
        b0 = bracket_for(cat, "T0", config=conf)
        b1 = bracket_for(cat, "T1", config=conf)
        b2 = bracket_for(cat, "T2", config=conf)
        assert b0.max_tokens <= b1.max_tokens <= b2.max_tokens
        assert b0.max_cost_usd <= b1.max_cost_usd <= b2.max_cost_usd
        assert b0.wall_clock_ceiling_s <= b1.wall_clock_ceiling_s <= b2.wall_clock_ceiling_s


def test_bracket_for_raises_unknown() -> None:
    with pytest.raises(ConfigTestConfigError):
        bracket_for("docs", "T99")
    with pytest.raises(ConfigTestConfigError):
        bracket_for("unknown", "T0")


def test_load_config_drifts(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("statuses: [pass, fail]\nbrackets: {}")
    with pytest.raises(ConfigTestConfigError, match="statuses drift"):
        load_config(bad)


def test_load_config_validation(tmp_path: Path) -> None:
    # 1. Config cell missing a budget key with no defaults
    f1 = tmp_path / "missing_defaults.yaml"
    f1.write_text("""
version: 1
statuses: [pass, fail, timeout, crash, invalid]
defaults:
  max_tokens: 1000
brackets:
  docs:
    T0: {max_tokens: 500, wall_clock_ceiling_s: 10}
""")
    with pytest.raises(ConfigTestConfigError, match="Missing budget in docs x T0"):
        load_config(f1)

    # 2. Non-positive budget raises ConfigTestConfigError
    f2 = tmp_path / "non_positive.yaml"
    f2.write_text("""
version: 1
statuses: [pass, fail, timeout, crash, invalid]
defaults:
  max_tokens: 1000
  max_cost_usd: 0.10
  wall_clock_ceiling_s: 100
brackets:
  docs:
    T0: {max_tokens: -5, max_cost_usd: 0.10, wall_clock_ceiling_s: 100}
""")
    with pytest.raises(ConfigTestConfigError, match="Non-positive budget in docs x T0"):
        load_config(f2)

    f3 = tmp_path / "non_positive_cost.yaml"
    f3.write_text("""
version: 1
statuses: [pass, fail, timeout, crash, invalid]
defaults:
  max_tokens: 1000
  max_cost_usd: -0.10
  wall_clock_ceiling_s: 100
brackets:
  docs:
    T0: {max_tokens: 100, max_cost_usd: -0.10, wall_clock_ceiling_s: 100}
""")
    with pytest.raises(ConfigTestConfigError, match="Non-positive budget in docs x T0"):
        load_config(f3)


def test_status_enum() -> None:
    # Assert values are exactly 'pass', 'fail', 'timeout', 'crash', 'invalid'
    assert list(Status) == [
        Status.PASS,
        Status.FAIL,
        Status.TIMEOUT,
        Status.CRASH,
        Status.INVALID,
    ]
    assert Status.PASS == "pass"
    assert Status.FAIL == "fail"
    assert Status.TIMEOUT == "timeout"
    assert Status.CRASH == "crash"
    assert Status.INVALID == "invalid"

    # Assert runtime type is str
    assert isinstance(Status.PASS, str)
    # Check that there are exactly 5 values
    assert len(Status) == 5


# ---------------------------------------------------------------------------
# settle_gate — the anti-sabotage gate wired into a (simulated) live settle
# attempt. sha256:309db4693b3a16f645d90d87904c1d0d73bd414a08aed68710cdd45e4a985a6d
# ---------------------------------------------------------------------------


class _StubGateDecision:
    def __init__(self, decision: str, evidence: dict | None = None) -> None:
        self.decision = decision
        self.evidence = evidence or {}


class _StubGateService:
    """Minimal GateService double for tests that need control over g5's verdict."""

    def __init__(self, decision: str = "allow", *, raises: Exception | None = None) -> None:
        self._decision = decision
        self._raises = raises
        self.calls: list[dict] = []

    def g5_local_verify(self, evidence: dict) -> _StubGateDecision:
        self.calls.append(dict(evidence))
        if self._raises is not None:
            raise self._raises
        return _StubGateDecision(self._decision, {"reason": "stub_denied"})


def _write_registry(
    path: Path,
    *,
    files: list[str] | None = None,
    directories: list[str] | None = None,
) -> None:
    """Write a minimal protected-paths-shaped registry for settle_gate fixtures.

    settle_gate reads only ``tier_p.files`` / ``tier_p.directories`` via its own
    strict loader (independent of ``omniagentos.policy.protected_paths``), so
    the fixture only needs to be shaped enough for that loader.
    """
    payload = {
        "version": 1,
        "tier_p": {
            "files": list(SELF_PROTECTED_FILES) if files is None else files,
            "directories": list(REQUIRED_TEST_ROOTS) if directories is None else directories,
        },
        "tier_s": {"files": [], "directories": []},
    }
    path.write_text(yaml.safe_dump(payload))


def test_settle_gate_allows_clean_run(repo: GitRepo, tmp_path: Path) -> None:
    """Red-first: a clean attempt (no test tampering) settles."""
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("A")
    repo.add(".")
    repo.commit("base")
    (repo / "src.py").write_text("print('feature')\n")
    repo.add(".")

    registry = tmp_path / "protected-paths.yaml"
    _write_registry(registry)

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry)
    assert ok is True
    assert evidence["decision"] == "allow"
    assert evidence["tamper_check"]["violations"] == []


def test_settle_gate_refuses_when_existing_test_tampered(repo: GitRepo, tmp_path: Path) -> None:
    """Red-first: an existing test file modified since base is refused, not settled."""
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    f = tests_dir / "test_a.py"
    f.write_text("A")
    repo.add(".")
    repo.commit("base")
    f.write_text("TAMPERED-TO-GO-GREEN")
    repo.add(".")

    registry = tmp_path / "protected-paths.yaml"
    _write_registry(registry)

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry)
    assert ok is False
    assert evidence["reason"] == "test_tampering_detected"
    assert evidence["decision"] == "deny"
    assert evidence["tamper_check"]["violations"][0]["path"] == "tests/test_a.py"


def test_settle_gate_refuses_when_registry_missing(repo: GitRepo, tmp_path: Path) -> None:
    """Red-first: the gate cannot run without its config, so settle refuses."""
    repo.commit("base")
    missing = tmp_path / "does-not-exist.yaml"

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=missing)
    assert ok is False
    assert evidence["reason"].startswith("gate_unrunnable:")
    assert evidence["decision"] == "deny"


def test_settle_gate_refuses_when_registry_malformed(repo: GitRepo, tmp_path: Path) -> None:
    """Red-first: unreadable/malformed config is a refusal, not a crash or a pass."""
    repo.commit("base")
    bad = tmp_path / "protected-paths.yaml"
    bad.write_text("tier_p: [this, is, not, a, mapping]\n")

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=bad)
    assert ok is False
    assert evidence["reason"].startswith("gate_unrunnable:")


def test_settle_gate_refuses_when_test_root_uncovered(repo: GitRepo, tmp_path: Path) -> None:
    """A registry that protects itself but drops a required test root is unrunnable."""
    repo.commit("base")
    registry = tmp_path / "protected-paths.yaml"
    _write_registry(registry, directories=["dashboard/e2e"])  # drops "tests"

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry)
    assert ok is False
    assert "test roots not protected" in evidence["reason"]
    assert "tests" in evidence["reason"]


def test_settle_gate_refuses_when_gate_service_raises(repo: GitRepo, tmp_path: Path) -> None:
    """Acceptance: a raise from GateService is a refusal, never a silent open."""
    repo.commit("base")
    registry = tmp_path / "protected-paths.yaml"
    _write_registry(registry)
    stub = _StubGateService(raises=RuntimeError("boom"))

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry, gate_service=stub)
    assert ok is False
    assert evidence["reason"].startswith("gate_service_unavailable:")
    assert evidence["decision"] == "deny"
    assert len(stub.calls) == 1


def test_settle_gate_propagates_gate_service_deny(repo: GitRepo, tmp_path: Path) -> None:
    """A clean tamper check does not override an independent GateService deny."""
    repo.commit("base")
    registry = tmp_path / "protected-paths.yaml"
    _write_registry(registry)
    stub = _StubGateService(decision="deny")

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry, gate_service=stub)
    assert ok is False
    assert evidence["decision"] == "deny"
    assert evidence["reason"] == "stub_denied"


# --- Mutation-check tests: each names the mutation it fails under -----------


def test_mutation_check_gate_disable_is_caught(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a future edit disables/bypasses the tamper check inside settle_gate,
    this test fails: it asserts the check both RUNS exactly once and that its
    verdict is what actually decides the settle outcome, on a repo where an
    existing test really was tampered with."""
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    f = tests_dir / "test_a.py"
    f.write_text("A")
    repo.add(".")
    repo.commit("base")
    f.write_text("TAMPERED")
    repo.add(".")

    registry = tmp_path / "protected-paths.yaml"
    _write_registry(registry)

    real_gate = configtest_gates.gate_existing_suite_unmodified
    calls: list[tuple] = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(configtest_gates, "gate_existing_suite_unmodified", _spy)

    ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry)

    assert len(calls) == 1, "gate-disable mutation: tamper check was not invoked"
    assert calls[0][1].get("test_roots") == REQUIRED_TEST_ROOTS, (
        "gate-disable mutation: tamper check was not asked to defend the protected roots"
    )
    assert ok is False, "gate-disable mutation: a tampered attempt was allowed to settle"
    assert evidence["reason"] == "test_tampering_detected"


def test_mutation_check_config_self_protection_removal_is_caught(
    repo: GitRepo, tmp_path: Path
) -> None:
    """If configs/protected-paths.yaml ever loses either self-protection entry
    (itself or scripts/benchmarks/configtest_gates.py), this test fails: it
    asserts settle_gate refuses to run unprotected rather than falling back to
    "no self-protection configured means nothing to check"."""
    repo.commit("base")

    for missing in SELF_PROTECTED_FILES:
        registry = tmp_path / f"protected-paths-{missing.replace('/', '_')}.yaml"
        remaining = [f for f in SELF_PROTECTED_FILES if f != missing]
        _write_registry(registry, files=remaining)

        ok, evidence = settle_gate(repo._path, "HEAD", protected_paths_path=registry)
        assert ok is False, f"self-protection-removal mutation ({missing}) was not caught"
        assert evidence["reason"].startswith("gate_unrunnable:")
        assert "self-protection removed" in evidence["reason"]
        assert missing in evidence["reason"]


# --- CLI entry point (the wireable hook a future gate-registry entry uses) --


def test_cli_settle_gate_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        configtest_gates,
        "settle_gate",
        lambda *a, **k: (True, {"decision": "allow"}),
    )
    assert configtest_gates._cli_settle_gate(["HEAD", "main"]) == 0


def test_cli_settle_gate_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        configtest_gates,
        "settle_gate",
        lambda *a, **k: (False, {"decision": "deny", "reason": "test_tampering_detected"}),
    )
    assert configtest_gates._cli_settle_gate(["HEAD", "main"]) == 1


def test_cli_settle_gate_cannot_run_is_never_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        configtest_gates,
        "settle_gate",
        lambda *a, **k: (False, {"decision": "deny", "reason": "gate_unrunnable:Boom:x"}),
    )
    assert configtest_gates._cli_settle_gate(["HEAD", "main"]) == 2


def test_cli_settle_gate_crash_is_never_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(configtest_gates, "settle_gate", _boom)
    assert configtest_gates._cli_settle_gate(["HEAD", "main"]) == 2


def test_cli_settle_gate_missing_args_refuses() -> None:
    assert configtest_gates._cli_settle_gate([]) == 2


def test_settle_gate_uses_real_protected_paths_path_by_default() -> None:
    assert configtest_gates.PROTECTED_PATHS_PATH.name == "protected-paths.yaml"
    assert configtest_gates.PROTECTED_PATHS_PATH.parent.name == "configs"


def test_load_tier_p_rejects_non_mapping_registry(tmp_path: Path) -> None:
    bad = tmp_path / "protected-paths.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(AntiSabotageGateError):
        configtest_gates._load_tier_p(bad)
