"""H-30 — end-to-end behaviour of the release gate that mocks cannot prove.

Everything here exercises the *real* code path:

- ``main()`` is actually invoked, and its returned process exit code is
  asserted (0 / 1 / 3). The previous tests re-implemented the status→rc
  mapping inside the test body and asserted ``3 == 3``, which would have
  stayed green through any regression in ``main()`` itself.
- The interpreter chosen by :func:`resolve_python` is *executed* and asked
  whether it still has its virtualenv and can import the gate's own tooling.
- Evidence filenames are minted under a frozen clock to prove they cannot
  collide for two runs of the same pinned SHA.
- A real phase subprocess is spawned to prove ``OMNIAGENTOS_REQUIRE_PG=1``
  actually reaches it.

Isolation: every git operation runs in a throwaway ``tmp_path`` repository
created by :func:`temp_git_repo`. No live services, no database connections,
no writes to the real worktree.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.harnesses import release_gate as rg
from omniagentos.harnesses.no_silent_skip import NO_RESULT_EXIT_CODE
from omniagentos.knowledge.contracts import ENV_REQUIRE_PG
from tests.harnesses.test_release_gate import FakeGitWorld

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* with a hermetic identity and no global config bleed."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=release-gate-test",
            "-c",
            "user.email=release-gate-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def temp_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create an isolated one-commit git repo. Returns ``(repo_path, head_sha)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "README.md").write_text("release gate exit-code fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point evidence outside the repo and pin a known-good interpreter.

    Evidence must not land inside the fixture repo: writing there would dirty
    the very tree whose cleanliness the gate is asserting.
    """
    var = tmp_path / "var"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var))
    monkeypatch.setenv("OMNIAGENTOS_PYTHON", sys.executable)
    monkeypatch.delenv("RELEASE_GATE_ALLOW_DIRTY", raising=False)
    return var


def _evidence_files(var: Path) -> list[Path]:
    return sorted((var / "release-gate").glob("*.json"))


# ---------------------------------------------------------------------------
# Real main() exit codes
# ---------------------------------------------------------------------------


def test_main_returns_0_for_planned_dry_run(
    tmp_path: Path, gate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, head = temp_git_repo(tmp_path)

    rc = rg.main(["--repo-root", str(repo), "--dry-run", "--phases", "ruff"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "status:     planned" in out
    written = json.loads(_evidence_files(gate_env)[0].read_text(encoding="utf-8"))
    assert written["status"] == "planned"
    assert written["pinned_sha"] == head


def test_main_returns_0_after_a_phase_actually_executes(
    tmp_path: Path, gate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 on the *certification* path, not merely on the dry-run path.

    The dry-run test above proves only the ``planned`` mapping: it returns 0
    without ever starting a phase, so it would keep passing if executing a
    genuinely successful phase produced any exit code at all. This is the
    mapping that actually certifies, and it was the one not covered.

    The sentinel is what makes this non-vacuous. Asserting ``rc == 0`` and
    ``status == "passed"`` would also be satisfied by a gate that skipped the
    phase and called the empty result a success — the precise false green this
    engagement exists to eliminate. The phase writes the sentinel *outside* the
    repository (writing inside would dirty the tree the gate just certified),
    so the assertion fails unless the payload really ran.
    """
    repo, head = temp_git_repo(tmp_path)
    sentinel = tmp_path / "phase-ran.txt"
    script = repo / "scripts" / "smoke" / "e2e.sh"
    script.parent.mkdir(parents=True)
    script.write_text(f"#!/usr/bin/env bash\nprintf ok > {sentinel}\n", encoding="utf-8")
    # Committed, not just written: the gate refuses a dirty tree, so an
    # uncommitted payload would be refused (3) before any phase started.
    _git(repo, "add", "scripts/smoke/e2e.sh")
    _git(repo, "commit", "--quiet", "-m", "add passing e2e payload")
    head = _git(repo, "rev-parse", "HEAD").strip()

    rc = rg.main(["--repo-root", str(repo), "--phases", "e2e"])
    out = capsys.readouterr().out

    assert rc == 0, f"a gate whose every phase passed did not exit 0\n{out}"
    assert sentinel.read_text(encoding="utf-8") == "ok", (
        "the gate reported success without the phase payload ever running"
    )
    assert "status:     passed" in out
    written = json.loads(_evidence_files(gate_env)[0].read_text(encoding="utf-8"))
    assert written["status"] == "passed"
    assert written["pinned_sha"] == head
    assert written["phases"][0]["name"] == "e2e"
    assert written["phases"][0]["status"] == "passed"
    assert written["phases"][0]["exit_code"] == 0
    # The success path is also the only one that can certify, so the flag that
    # an auditor reads must be true here — and be true as a *fact*, recorded
    # after verification ran, not as the intent recorded before it.
    assert written["interpreter_verified"] is True


def test_main_returns_3_for_dirty_tree(
    tmp_path: Path, gate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = temp_git_repo(tmp_path)
    (repo / "untracked.txt").write_text("makes the tree dirty\n", encoding="utf-8")

    rc = rg.main(["--repo-root", str(repo), "--dry-run", "--phases", "ruff"])

    assert rc == 3, "dirty tree must be refused (3), never a plain failure (1)"
    assert "dirty" in capsys.readouterr().out.lower()


def test_main_returns_3_for_unresolvable_interpreter(
    tmp_path: Path, gate_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = temp_git_repo(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_PYTHON", str(tmp_path / "no" / "such" / "python"))

    rc = rg.main(["--repo-root", str(repo), "--dry-run", "--phases", "ruff"])

    assert rc == 3
    written = json.loads(_evidence_files(gate_env)[0].read_text(encoding="utf-8"))
    assert written["status"] == "refused"
    assert "OMNIAGENTOS_PYTHON" in written["refuse_reason"]


def test_main_returns_3_for_unknown_phase(tmp_path: Path, gate_env: Path) -> None:
    repo, _ = temp_git_repo(tmp_path)

    rc = rg.main(["--repo-root", str(repo), "--phases", "not_a_real_phase"])

    assert rc == 3


def test_main_returns_1_when_a_phase_payload_is_missing(
    tmp_path: Path, gate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing payload fails the gate (1) — it must never pass by absence."""
    repo, _ = temp_git_repo(tmp_path)
    # scripts/smoke/e2e.sh does not exist in the fixture repo, so bash exits 127.
    rc = rg.main(["--repo-root", str(repo), "--phases", "e2e"])

    assert rc == 1, "a failing phase is exit 1, distinct from a refusal (3)"
    assert "status:     failed" in capsys.readouterr().out
    written = json.loads(_evidence_files(gate_env)[0].read_text(encoding="utf-8"))
    assert written["phases"][0]["name"] == "e2e"
    assert written["phases"][0]["status"] == "failed"
    assert written["phases"][0]["exit_code"] != 0


def test_main_list_returns_0_without_touching_git(gate_env: Path) -> None:
    assert rg.main(["--list"]) == 0
    assert _evidence_files(gate_env) == []


# ---------------------------------------------------------------------------
# Interpreter selection is behavioural, not just textual
# ---------------------------------------------------------------------------


def _project_venv_python() -> Path:
    return REPO_ROOT / ".venv" / "bin" / "python"


requires_project_venv = pytest.mark.skipif(
    not _project_venv_python().is_file(),
    reason="project .venv is not present in this checkout",
)


@requires_project_venv
def test_resolve_python_returns_the_venv_path_verbatim() -> None:
    """The venv executable path must be returned, not its symlink target."""
    selected = Path(rg.resolve_python({}, repo_root=REPO_ROOT))
    assert selected == _project_venv_python()


@requires_project_venv
def test_selected_interpreter_retains_venv_and_imports_gate_tooling() -> None:
    """Execute the selected interpreter and prove it can run the gate's phases.

    This is the behavioural assertion the textual path check cannot make: an
    interpreter that lost its virtualenv still *looks* fine as a string but
    cannot import pytest, ruff or mypy, so every phase would fail (or worse,
    a differently-provisioned base interpreter would silently certify).
    """
    selected = rg.resolve_python({}, repo_root=REPO_ROOT)
    probe = rg.probe_interpreter(selected)

    assert probe.in_virtualenv, (
        f"selected interpreter is not in a virtualenv: prefix={probe.prefix} "
        f"base_prefix={probe.base_prefix}"
    )
    assert Path(probe.prefix) == REPO_ROOT / ".venv"
    missing = sorted(name for name, ok in probe.importable.items() if not ok)
    assert missing == [], f"selected interpreter cannot import: {missing}"


@requires_project_venv
def test_resolving_the_symlink_would_have_lost_the_venv() -> None:
    """Anti-regression: prove why resolve() is forbidden on interpreter paths.

    ``.venv/bin/python`` is a symlink to the base interpreter. If it is ever
    canonicalised again, the gate certifies under a Python whose ``sys.prefix``
    is the standalone install — a different environment entirely.
    """
    venv_python = _project_venv_python()
    if not venv_python.is_symlink():
        pytest.skip("this venv uses a copied interpreter, not a symlink")

    kept = rg.probe_interpreter(str(venv_python))
    canonicalised = rg.probe_interpreter(str(venv_python.resolve()))

    assert kept.prefix != canonicalised.prefix
    assert kept.in_virtualenv
    assert not canonicalised.in_virtualenv


def test_gate_refuses_a_forged_repo_local_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo-local ``.venv/bin/python`` pointing at a foreign interpreter is refused.

    This is the attack the path-shape check cannot see. ``resolve_python`` asks
    only "does ``<repo>/.venv/bin/python`` exist and is it executable"; a symlink
    to any system interpreter satisfies that. Before the gate executed the
    interpreter, such a tree certified against an environment nobody audited —
    and if that interpreter happened to have pytest installed, the phases would
    even go green.

    Asserting exit 3 through real ``main()`` is what makes this non-vacuous:
    defining ``verify_certification_interpreter`` without calling it from the
    gate (the prior state of this module) would leave this test failing.
    """
    repo, _ = temp_git_repo(tmp_path)

    # A genuine, working interpreter — just not this project's venv.
    foreign = Path(sys.base_prefix) / "bin" / "python3"
    if not foreign.exists():
        pytest.skip(f"no base interpreter at {foreign}")

    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(foreign)

    # No OMNIAGENTOS_PYTHON: force the auto-selection branch, where the forgery lives.
    monkeypatch.delenv("OMNIAGENTOS_PYTHON", raising=False)

    rc = rg.main(["--repo-root", str(repo), "--dry-run"])

    assert rc == 3, "gate certified under a forged repo-local interpreter"


@requires_project_venv
def test_probe_refuses_a_module_that_is_found_but_cannot_be_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-installed wheel is discoverable and unusable; the probe must say so.

    ``importlib.util.find_spec`` answers "is there something here", not "does it
    work". A wheel unpacked without its native library leaves a ``psycopg``
    package whose ``__init__`` raises on import — the stale/half-built
    environment interpreter verification exists to reject — and a spec-based
    probe reports it as present.

    The shadowing package below is a real one on ``PYTHONPATH``, and the test
    asserts the two answers *disagree*, so it cannot pass by accident: were the
    probe still spec-based, ``importable`` would be true and the assertion fails.
    """
    broken = tmp_path / "shadow"
    (broken / "psycopg").mkdir(parents=True)
    (broken / "psycopg" / "__init__.py").write_text(
        'raise ImportError("libpq not found; wheel is half-installed")\n', encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(broken))

    selected = rg.resolve_python({}, repo_root=REPO_ROOT)

    found = subprocess.run(
        [selected, "-c", "import importlib.util as u; print(u.find_spec('psycopg') is not None)"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    assert found == "True", "premise changed: the broken package is no longer discoverable"

    probe = rg.probe_interpreter(selected)
    assert probe.importable["psycopg"] is False, (
        "probe certified a package that is discoverable but raises on import"
    )

    with pytest.raises(rg.InterpreterError, match="cannot import psycopg"):
        rg.verify_certification_interpreter(selected, repo_root=REPO_ROOT)


def test_probe_refuses_a_verdict_that_omits_a_required_module(tmp_path: Path) -> None:
    """An incomplete probe payload must not read as "nothing missing".

    Callers decide by scanning ``importable`` for false values, so a mapping
    that simply *omits* a module reports it as fine. An empty mapping would
    certify every requirement at once — a success resting on no evidence, which
    is the whole family of defect this module guards against.
    """
    stub = tmp_path / "python-stub"
    stub.write_text(
        "#!/bin/sh\n"
        'echo \'{"executable":"/x","prefix":"/p","base_prefix":"/b",'
        '"importable":{"pytest":true}}\'\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    with pytest.raises(rg.InterpreterError, match="no verdict for"):
        rg.probe_interpreter(str(stub))


@pytest.mark.parametrize(
    ("script", "match"),
    [
        pytest.param("#!/bin/sh\nsleep 30\n", "did not answer within", id="hangs"),
        pytest.param("#!/bin/sh\necho not json\n", "did not return JSON", id="not-json"),
        pytest.param("#!/bin/sh\necho '[1, 2]'\n", "not an object", id="json-but-not-an-object"),
        pytest.param(
            '#!/bin/sh\necho \'{"executable":"/x","prefix":"/p","base_prefix":"/b"}\'\n',
            "no usable 'importable' mapping",
            id="importable-missing",
        ),
        pytest.param(
            "#!/bin/sh\necho '{\"importable\":{}}'\n",
            "omitted base_prefix, executable, prefix",
            id="fields-missing",
        ),
        pytest.param(
            "#!/bin/sh\nprintf '\\377\\376 not utf8'\n",
            "undecodable output",
            id="undecodable-bytes",
        ),
        pytest.param(
            "#!/bin/sh\n"
            'echo \'{"executable":"/x","prefix":"/p","base_prefix":"/b",'
            '"importable":{"pytest":"false","ruff":"false","mypy":"false",'
            '"psycopg":"false"}}\'\n',
            "non-boolean import verdicts",
            id="string-verdicts",
        ),
        pytest.param(
            "#!/bin/sh\n"
            'echo \'{"executable":null,"prefix":"/p","base_prefix":"/b",'
            '"importable":{"pytest":true,"ruff":true,"mypy":true,'
            '"psycopg":true}}\'\n',
            "non-string interpreter identity",
            id="null-identity",
        ),
    ],
)
def test_every_probe_failure_is_an_interpreter_error(
    tmp_path: Path, script: str, match: str
) -> None:
    """The probe's failures must all arrive in the form the gate can record.

    ``run_release_gate`` promises durable evidence "for every outcome including
    refusals", and it delivers that by catching ``InterpreterError`` alone. A
    hang, an unreadable payload, or a truncated one previously escaped as
    ``TimeoutExpired``/``ValueError``/``KeyError``, aborting the gate with a
    traceback and **no evidence file at all** — the outcome an audit cannot
    distinguish from a run that was never started.

    Each stub is a real executable exercising the production parser, so these
    fail if the conversions are removed rather than merely changing the message.
    """
    stub = tmp_path / "python-stub"
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)

    with pytest.raises(rg.InterpreterError, match=match):
        rg.probe_interpreter(str(stub), timeout=2.0)


def test_an_unrunnable_interpreter_is_refused_evidence_not_a_traceback(tmp_path: Path) -> None:
    """A gate run whose interpreter cannot even start must still leave a record.

    This is the end-to-end half of the check above: it goes through
    ``run_release_gate`` rather than the probe, so it fails if ``OSError`` is
    converted somewhere that the gate does not catch.
    """
    missing = tmp_path / "no-such-interpreter"
    assert not missing.exists(), "premise changed: the stub path exists"

    ev = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        runner=None,
        verify_interpreter=True,
        evidence_path=tmp_path / "ev.json",
        python=str(missing),
        env={},
    )

    assert ev.status == "refused"
    assert "could not be executed" in ev.refuse_reason
    assert ev.interpreter_verified is False
    assert (tmp_path / "ev.json").exists(), "a refused run left no durable evidence"


def test_an_interpreter_reporting_nothing_imports_is_refused_not_certified(
    tmp_path: Path,
) -> None:
    """The fail-open direction, proven through the gate rather than the parser.

    This stub is a real virtualenv by every structural check — ``sys.prefix``
    differs from ``sys.base_prefix`` — and it answers the import question
    truthfully in content: *none* of the required modules are available. It just
    says so with JSON strings. Coercing those values with ``bool`` turns every
    ``"false"`` into ``True``, so the interpreter least able to run the gate's
    phases passes verification with ``missing == []``.

    Asserting on ``refuse_reason`` rather than merely on non-certification is
    what makes this non-vacuous: a stub can fail for many reasons, and only this
    one is about the verdicts being unreadable.
    """
    stub = tmp_path / "python-liar"
    payload = json.dumps(
        {
            "executable": str(stub),
            "prefix": str(tmp_path / "venv"),
            "base_prefix": "/usr",
            "importable": dict.fromkeys(rg.REQUIRED_INTERPRETER_MODULES, "false"),
        }
    )
    stub.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n", encoding="utf-8")
    stub.chmod(0o755)

    ev = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        runner=None,
        verify_interpreter=True,
        evidence_path=tmp_path / "ev.json",
        python=str(stub),
        env={},
    )

    assert ev.status == "refused", (
        f"an interpreter that reported no module as importable was not refused "
        f"(status={ev.status!r})"
    )
    assert "non-boolean import verdicts" in ev.refuse_reason, (
        f"the run was refused, but not because the verdicts were unreadable: {ev.refuse_reason!r}"
    )
    assert ev.interpreter_verified is False
    written = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert written["status"] == "refused", "the refusal was not recorded durably"


def test_normalize_executable_preserves_the_final_symlink(tmp_path: Path) -> None:
    """Only the parent is resolved; the executable component stays verbatim."""
    real = tmp_path / "real" / "python3"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    link_dir = tmp_path / "venv" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "python"
    link.symlink_to(real)

    normalized = rg.normalize_executable(link)

    assert normalized.name == "python"
    assert normalized == link
    assert normalized != real


def test_resolve_python_refuses_a_foreign_interpreter(tmp_path: Path) -> None:
    """No .venv and a sys.executable outside the root must refuse, not fall back."""
    empty_root = tmp_path / "no_venv_here"
    empty_root.mkdir()
    with pytest.raises(rg.InterpreterError, match="project-local interpreter"):
        rg.resolve_python({}, repo_root=empty_root, require_project_local=True)


# ---------------------------------------------------------------------------
# Evidence paths are collision-proof
# ---------------------------------------------------------------------------


def test_evidence_paths_are_unique_within_one_clock_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same pinned SHA, same frozen instant — names must still all differ."""
    monkeypatch.setattr(rg, "_evidence_stamp", lambda: "20260725T120000000000Z")
    env = {"OMNIAGENTOS_VAR_DIR": str(tmp_path / "var")}

    paths = [rg.evidence_path_for(tmp_path, "a" * 40, env) for _ in range(64)]

    assert len(set(paths)) == 64


def test_two_runs_at_the_same_sha_do_not_overwrite_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two full runs of the same commit leave two audit records, not one."""
    monkeypatch.setattr(rg, "_evidence_stamp", lambda: "20260725T120000000000Z")
    var = tmp_path / "var"
    env = {"OMNIAGENTOS_VAR_DIR": str(var)}
    repo = tmp_path / "repo"
    repo.mkdir()

    for _ in range(2):
        rg.run_release_gate(
            repo_root=repo,
            phases=["ruff"],
            runner=FakeGitWorld(dirty=False).runner,
            python="python",
            env=env,
        )

    files = _evidence_files(var)
    assert len(files) == 2, "same-SHA same-instant runs overwrote each other"
    assert all(json.loads(f.read_text(encoding="utf-8"))["pinned_sha"] == "a" * 40 for f in files)


def test_evidence_write_refuses_to_overwrite_an_existing_record(tmp_path: Path) -> None:
    """The write is exclusive-create, so a colliding name loses instead of clobbering."""
    evidence = rg.GateEvidence(
        pinned_sha="a" * 40,
        status="passed",
        started_at="s",
        ended_at="e",
        repo_root=str(tmp_path),
        allow_dirty=False,
    )
    target = tmp_path / "release-gate" / "record.json"

    rg.write_evidence(evidence, target)
    first = target.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError):
        rg.write_evidence(evidence, target)
    assert target.read_text(encoding="utf-8") == first, "existing evidence was rewritten"


def test_operator_named_evidence_path_is_never_silently_relocated(tmp_path: Path) -> None:
    """``--evidence`` is honoured exactly; a collision surfaces, not a new filename."""
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "chosen.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError):
        rg.run_release_gate(
            repo_root=repo,
            phases=["ruff"],
            runner=FakeGitWorld(dirty=False).runner,
            python="python",
            env={"OMNIAGENTOS_VAR_DIR": str(tmp_path / "var")},
            evidence_path=target,
        )
    assert target.read_text(encoding="utf-8") == "{}"
    assert not (tmp_path / "var").exists(), "gate fell back to an auto-named file"


def test_main_refuses_with_exit_3_when_evidence_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecordable verdict is a refusal, not a traceback.

    A gate that dies with ``PermissionError`` after computing its verdict is
    indistinguishable from a crashed gate, and the verdict is lost either way.
    """
    repo, _ = temp_git_repo(tmp_path)
    unwritable = tmp_path / "nowhere"
    unwritable.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(unwritable))

    rc = rg.main(["--repo-root", str(repo), "--dry-run"])

    assert rc == 3


def test_main_refuses_with_exit_3_when_evidence_path_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An occupied ``--evidence`` path refuses; it neither clobbers nor crashes."""
    repo, _ = temp_git_repo(tmp_path)
    target = tmp_path / "chosen.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))

    rc = rg.main(["--repo-root", str(repo), "--dry-run", "--evidence", str(target)])

    assert rc == 3
    assert target.read_text(encoding="utf-8") == "{}", "operator evidence was overwritten"


@pytest.mark.parametrize(
    ("dirty", "dry_run", "expected_status"),
    # "simulated" rather than "passed": these runs inject a runner, so nothing
    # was certified. The parametrization still reaches the same three write sites.
    [(False, False, "simulated"), (False, True, "planned"), (True, False, "refused")],
)
def test_every_terminal_write_regenerates_a_colliding_auto_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty: bool,
    dry_run: bool,
    expected_status: str,
) -> None:
    """A collided auto-generated name is retried on *every* exit path.

    Exclusive creation only preserves evidence if each terminal write also
    knows how to pick a fresh name. A site that calls ``write_evidence``
    directly would raise ``FileExistsError`` on a legitimate collision and lose
    the very verdict it was recording — and the loss is invisible in normal
    runs, because auto-generated names rarely collide. Forcing the collision is
    the only way to prove the retry is wired at each site rather than one.
    """
    var = tmp_path / "var"
    env = {"OMNIAGENTOS_VAR_DIR": str(var)}
    repo = tmp_path / "repo"
    repo.mkdir()

    calls = itertools.count()

    def colliding_path(*_args: object, **_kwargs: object) -> Path:
        n = next(calls)
        name = "collide.json" if n < 2 else f"fresh-{n}.json"
        return var / "release-gate" / name

    monkeypatch.setattr(rg, "evidence_path_for", colliding_path)

    for _ in range(2):
        evidence = rg.run_release_gate(
            repo_root=repo,
            phases=["ruff"],
            runner=FakeGitWorld(dirty=dirty).runner,
            python="python",
            env=env,
            dry_run=dry_run,
        )
        assert evidence.status == expected_status

    files = _evidence_files(var)
    assert len(files) == 2, f"{expected_status} path lost a record to a name collision"
    assert {f.name for f in files} == {"collide.json", "fresh-2.json"}


def test_the_interpreter_refusal_write_site_also_regenerates_a_colliding_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth terminal write, which an injected runner cannot reach.

    The parametrized test above covers three write sites, but every one of its
    cases passes ``runner=...`` — and ``will_verify`` is ``runner is None and
    verify_interpreter``, so injecting a runner switches verification off and
    the interpreter-refusal site is never entered. That site is reached only by
    a real, failing interpreter.

    It is also the one site that cannot reuse the others' naming: it refuses
    before git resolves a SHA, so it writes ``pinned_sha=""`` and ``_maybe_write``
    falls back to the literal ``"refused"`` stem. A retry defect confined to that
    fallback would be invisible everywhere else, and would silently discard the
    refusal evidence that rounds 3 to 5 exist to guarantee.
    """
    var = tmp_path / "var"
    env = {"OMNIAGENTOS_VAR_DIR": str(var)}
    calls = itertools.count()

    def colliding_path(*_args: object, **_kwargs: object) -> Path:
        n = next(calls)
        name = "collide.json" if n < 2 else f"fresh-{n}.json"
        return var / "release-gate" / name

    monkeypatch.setattr(rg, "evidence_path_for", colliding_path)

    for _ in range(2):
        evidence = rg.run_release_gate(
            repo_root=tmp_path,
            phases=["ruff"],
            runner=None,
            python=str(tmp_path / "nonexistent-python"),
            env=env,
        )
        assert evidence.status == "refused"
        assert evidence.interpreter_verified is False
        assert evidence.pinned_sha == "", (
            "premise changed: this site now has a SHA, so it no longer exercises "
            "the 'refused' name fallback"
        )

    files = _evidence_files(var)
    assert len(files) == 2, "the interpreter-refusal path lost a record to a name collision"
    assert {f.name for f in files} == {"collide.json", "fresh-2.json"}


def test_evidence_path_honours_var_dir_and_short_sha(tmp_path: Path) -> None:
    path = rg.evidence_path_for(
        tmp_path, "abcdef1234567890", {"OMNIAGENTOS_VAR_DIR": str(tmp_path)}
    )
    assert path.parent == tmp_path / "release-gate"
    assert path.name.endswith(".json")
    assert "abcdef123456" in path.name


# ---------------------------------------------------------------------------
# OMNIAGENTOS_REQUIRE_PG=1 certification parity
# ---------------------------------------------------------------------------


def test_certification_env_forces_require_pg_on() -> None:
    assert rg.certification_env({})[ENV_REQUIRE_PG] == "1"
    assert rg.certification_env({"PATH": "/usr/bin"})[ENV_REQUIRE_PG] == "1"
    # An operator cannot weaken certification by exporting 0.
    assert rg.certification_env({ENV_REQUIRE_PG: "0"})[ENV_REQUIRE_PG] == "1"


def test_certification_env_preserves_the_rest_of_the_environment() -> None:
    env = rg.certification_env({"PATH": "/usr/bin", "HOME": "/tmp/home"})
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/home"


def test_phase_subprocess_actually_receives_require_pg(tmp_path: Path) -> None:
    """Spawn a real phase and let the child assert the flag reached it.

    ``make validate`` used to run the suite with ``OMNIAGENTOS_REQUIRE_PG=1``;
    routing validate through this gate must not quietly drop that, or every
    PostgreSQL-backed test can skip itself and the gate still reports green.
    """
    repo, head = temp_git_repo(tmp_path)
    spec = rg.PhaseSpec(
        name="backend",
        description="require_pg probe",
        argv=(
            sys.executable,
            "-c",
            f"import os,sys; sys.exit(0 if os.environ.get({ENV_REQUIRE_PG!r}) == '1' else 9)",
        ),
    )

    ev = rg.run_phase(
        spec,
        repo_root=repo,
        pinned_sha=head,
        allow_dirty=False,
        env={ENV_REQUIRE_PG: "0"},  # even an explicit 0 must be overridden
        runner=None,  # real subprocess — this is the point of the test
    )

    assert ev.exit_code == 0, f"{ENV_REQUIRE_PG} did not reach the phase subprocess"
    assert ev.status == "passed"


@pytest.mark.parametrize(
    ("use_runner", "verify"),
    [
        pytest.param(True, True, id="injected-runner"),
        pytest.param(False, False, id="verification-disabled"),
    ],
)
def test_an_unverified_run_cannot_claim_a_certification(
    tmp_path: Path, use_runner: bool, verify: bool
) -> None:
    """Both carve-outs around interpreter verification must self-identify.

    ``verify_certification_interpreter`` is skipped when a runner is injected
    and when verification is switched off. Left alone, either path emits
    evidence reading ``status="passed"`` that is byte-for-byte the shape of a
    real certification — so the guard would be present but optional, and a
    record produced by a test harness could be mistaken for a released build's.

    The phase results are still recorded truthfully; it is only the overall
    claim that is withheld, and the reason is written into the evidence.
    """
    repo, _ = temp_git_repo(tmp_path)
    ev = rg.run_release_gate(
        repo_root=repo,
        phases=["ruff"],
        runner=FakeGitWorld(dirty=False).runner if use_runner else None,
        verify_interpreter=verify,
        evidence_path=tmp_path / "ev.json",
        python=sys.executable,
        env={},
    )

    assert ev.status == "simulated", f"unverified run claimed {ev.status!r}"
    assert ev.interpreter_verified is False
    assert "not a certification" in ev.refuse_reason

    written = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert written["status"] == "simulated"
    assert written["interpreter_verified"] is False

    # And the CLI exit mapping must not reward it: only "passed"/"planned" are 0.
    assert ev.status not in {"passed", "planned"}


def test_a_run_refused_by_verification_does_not_record_itself_as_verified(
    tmp_path: Path,
) -> None:
    """``interpreter_verified`` must state what happened, not what was intended.

    A run that switches verification *on* and is then refused by it did not
    verify anything — verification is precisely what stopped it. Deriving the
    flag from the caller's intent (``runner is None and verify_interpreter``)
    makes the refusal evidence claim a verified interpreter while
    ``refuse_reason`` says the interpreter was rejected: a self-contradicting
    audit record, and the one that reads "verified" is the one an auditor
    trusts. The flag must therefore be set only after the check returns.

    The stub is a real executable answering the probe honestly for a non-venv
    interpreter, so the refusal comes from the production check rather than
    from a patched-out one.
    """
    stub = tmp_path / "python-not-a-venv"
    payload = json.dumps(
        {
            "executable": "/usr/bin/python3",
            "prefix": "/usr",
            "base_prefix": "/usr",
            "importable": dict.fromkeys(rg.REQUIRED_INTERPRETER_MODULES, True),
        }
    )
    stub.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n", encoding="utf-8")
    stub.chmod(0o755)

    ev = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        runner=None,
        verify_interpreter=True,
        evidence_path=tmp_path / "ev.json",
        python=str(stub),
        env={},
    )

    assert ev.status == "refused", f"the stub interpreter was not refused: {ev.status!r}"
    assert "not a virtualenv" in ev.refuse_reason, (
        f"refused for an unrelated reason, so this test would not exercise a "
        f"failed verification: {ev.refuse_reason!r}"
    )
    assert ev.interpreter_verified is False, (
        "evidence for a run that verification *rejected* claims the interpreter was verified"
    )
    written = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert written["interpreter_verified"] is False


def test_gate_evidence_records_require_pg(tmp_path: Path) -> None:
    ev = rg.run_release_gate(
        repo_root=tmp_path,
        phases=["ruff"],
        runner=FakeGitWorld(dirty=False).runner,
        evidence_path=tmp_path / "ev.json",
        python="python",
        env={},
    )
    assert ev.require_pg is True
    assert ev.python == "python"
    written = json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))
    assert written["require_pg"] is True
    assert written["python"] == "python"
    assert "require_pg: 1" in rg.format_summary(ev)


# ---------------------------------------------------------------------------
# Satellite payload handoff: S19A owns api_contracts, S19B owns the scale phases
# ---------------------------------------------------------------------------


def test_api_contracts_phase_targets_s19a_canonical_paths() -> None:
    """The gate must name the artifact S19A actually produces."""
    assert rg.API_CONTRACT_ARTIFACT == "contracts/openapi.json"
    assert rg.API_CONTRACT_GENERATOR == "scripts/generate_openapi.py"
    assert rg.API_CONTRACT_TEST == "tests/api/test_openapi_contract.py"

    spec = _spec("api_contracts")
    assert spec.argv == (
        "python",
        "-m",
        "pytest",
        "-q",
        "-p",
        rg.NO_SILENT_SKIP_PLUGIN,
        "--no-silent-skip-mode",
        "required",
        rg.API_CONTRACT_TEST,
    )
    assert rg.API_CONTRACT_ARTIFACT in spec.description
    assert rg.API_CONTRACT_GENERATOR in spec.description


def test_api_contracts_phase_fails_closed_when_payload_is_absent(
    tmp_path: Path, gate_env: Path
) -> None:
    """Until S19A lands, the phase must fail — never pass by absence."""
    repo, _ = temp_git_repo(tmp_path)
    assert not (repo / rg.API_CONTRACT_TEST).exists()

    rc = rg.main(["--repo-root", str(repo), "--phases", "api_contracts"])

    assert rc == 1
    written = json.loads(_evidence_files(gate_env)[0].read_text(encoding="utf-8"))
    assert written["phases"][0]["name"] == "api_contracts"
    assert written["phases"][0]["status"] == "failed"


def _spec(name: str) -> rg.PhaseSpec:
    specs = rg.default_phase_specs(python="python", repo_root=Path("/tmp/repo"))
    return next(s for s in specs if s.name == name)


def test_s19b_phases_couple_only_through_pytest_markers() -> None:
    """The gate pins S19B's marker contract and nothing deeper."""
    live = _spec("live_restart")
    load = _spec("load_contention")

    strict = (
        "python",
        "-m",
        "pytest",
        "-q",
        "-p",
        rg.NO_SILENT_SKIP_PLUGIN,
        "--no-silent-skip-mode",
        "required",
        "-m",
    )
    assert live.argv == (*strict, rg.LIVE_RESTART_SELECTION, "tests")
    assert load.argv == (*strict, rg.LOAD_CONTENTION_SELECTION, "tests")
    assert live.conditional and live.enable_env == "RELEASE_GATE_LIVE"
    assert load.conditional and load.enable_env == "RELEASE_GATE_LOAD"


@pytest.mark.parametrize(
    ("phase", "infra", "payload"),
    [
        pytest.param(
            "live_restart",
            rg.LIVE_RESTART_MARKER,
            rg.LIVE_RESTART_PAYLOAD_MARKER,
            id="live_restart",
        ),
        pytest.param(
            "load_contention",
            rg.LOAD_CONTENTION_MARKER,
            rg.LOAD_CONTENTION_PAYLOAD_MARKER,
            id="load_contention",
        ),
    ],
)
def test_an_s19b_phase_selects_its_own_payload_not_any_marked_test(
    phase: str, infra: str, payload: str
) -> None:
    """Selecting on the infrastructure marker alone certifies the wrong thing.

    ``smoke`` and ``perf`` are repository-wide markers that predate these phases
    and are carried by tests belonging to nobody in particular. A phase selecting
    ``-m smoke`` passes as long as *any* smoke test exists — so S19B's restart
    payload could be deleted outright and ``live_restart`` would still record
    ``passed``, which is the "certification resting on the wrong evidence"
    failure this gate exists to prevent.

    The selection must therefore require the payload marker too. Asserted as a
    conjunction over the parsed expression rather than as a string, so it cannot
    be satisfied by a phase that merely mentions the payload marker in a
    disjunction or a description.
    """
    argv = _spec(phase).argv
    # The value after the *last* ``-m``: the first one is ``python -m pytest``.
    last_m = len(argv) - 1 - argv[::-1].index("-m")
    selection = argv[last_m + 1]

    conjuncts = {part.strip() for part in selection.split(" and ")}
    assert " or " not in selection, (
        f"phase {phase!r} accepts an alternative to its own payload: {selection!r}"
    )
    assert payload in conjuncts, (
        f"phase {phase!r} selects {selection!r}, which any test marked {infra!r} "
        f"satisfies; deleting S19B's payload would leave the phase green"
    )
    assert infra in conjuncts, (
        f"phase {phase!r} dropped the {infra!r} marker, changing which execution "
        f"policy the payload runs under: {selection!r}"
    )


def test_an_s19b_phase_with_no_payload_fails_instead_of_certifying(tmp_path: Path) -> None:
    """The end of the fail-closed argument, run as a real pytest session.

    A marker expression that matches nothing deselects everything, and pytest
    exits 5 for that — not a failure the gate would notice by exit code alone.
    Here the phase's own argv is run against a tree holding a test with only the
    *infrastructure* marker, which is exactly the state of this repository until
    S19B applies the payload marker. The run must be refused.
    """
    payload_dir = tmp_path / "tests"
    payload_dir.mkdir()
    (payload_dir / "test_unrelated_smoke.py").write_text(
        "import pytest\n\n"
        f"@pytest.mark.{rg.LIVE_RESTART_MARKER}\n"
        "def test_someone_elses_smoke_test():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    argv = _spec("live_restart").argv
    result = subprocess.run(
        [sys.executable, *argv[1:-1], "-p", "no:cacheprovider", str(payload_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"the live_restart phase certified a tree containing no S19B payload "
        f"(exit {result.returncode})\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "executed no tests" in combined or "deselected" in combined


def test_gate_does_not_reach_into_satellite_internals() -> None:
    """Payload ownership stays with the satellites: no imports of their modules."""
    source = Path(rg.__file__).read_text(encoding="utf-8")
    for forbidden in ("testpolicy", "coverage_policy", "generate_openapi"):
        assert f"import {forbidden}" not in source
        assert f"from omniagentos.{forbidden}" not in source


def test_pytest_addopts_in_env_refuses_certification(tmp_path: Path) -> None:
    """PYTEST_ADDOPTS in env must cause the release gate to refuse certification."""
    env = {"PYTEST_ADDOPTS": "-k non_existent_test", "OMNIAGENTOS_VAR_DIR": str(tmp_path / "var")}
    evidence = rg.run_release_gate(
        repo_root=REPO_ROOT,
        phases=["ruff"],
        env=env,
        runner=FakeGitWorld(dirty=False).runner,
    )
    assert evidence.status == "refused"
    assert "PYTEST_ADDOPTS" in evidence.refuse_reason


def test_foreign_interpreter_override_is_rejected(tmp_path: Path) -> None:
    """OMNIAGENTOS_PYTHON pointing outside repo must be rejected."""
    env = {"OMNIAGENTOS_PYTHON": "/usr/bin/python3", "OMNIAGENTOS_VAR_DIR": str(tmp_path / "var")}
    evidence = rg.run_release_gate(
        repo_root=REPO_ROOT,
        phases=["ruff"],
        env=env,
        verify_interpreter=True,
    )
    assert evidence.status == "refused"
    assert (
        "outside the repository" in evidence.refuse_reason
        or "not a virtualenv" in evidence.refuse_reason
    )


def test_dirty_mode_never_returns_passed_or_exit_0(tmp_path: Path, gate_env: Path) -> None:
    """allow_dirty=True must never return status 'passed' or exit 0, even on verified interpreter."""
    repo, head = temp_git_repo(tmp_path)
    (repo / "dirty.txt").write_text("makes tree dirty\n", encoding="utf-8")

    # 1. Test dry-run path with allow_dirty=True using main()
    rc_dry = rg.main(["--repo-root", str(repo), "--allow-dirty", "--dry-run", "--phases", "ruff"])
    assert rc_dry != 0, "main() must not return exit 0 on a dirty tree even with --allow-dirty"
    assert rc_dry == 1
    files_dry = _evidence_files(gate_env)
    written_dry = json.loads(files_dry[-1].read_text(encoding="utf-8"))
    assert written_dry["status"] == "simulated"
    assert "RELEASE_GATE_ALLOW_DIRTY=1 was used on a dirty tree" in written_dry["refuse_reason"]

    # 2. Test actual phase execution path with allow_dirty=True on a verified interpreter
    script = repo / "scripts" / "smoke" / "e2e.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _git(repo, "add", "scripts/smoke/e2e.sh")
    _git(repo, "commit", "--quiet", "-m", "add passing e2e payload")
    (repo / "dirty.txt").write_text("dirty again\n", encoding="utf-8")

    rc_exec = rg.main(["--repo-root", str(repo), "--allow-dirty", "--phases", "e2e"])
    assert rc_exec != 0, "main() must not return exit 0 when executing a phase on a dirty tree"
    assert rc_exec == 1
    files_exec = _evidence_files(gate_env)
    written_exec = json.loads(files_exec[-1].read_text(encoding="utf-8"))
    assert written_exec["status"] == "simulated"
    assert written_exec["interpreter_verified"] is True
    assert "RELEASE_GATE_ALLOW_DIRTY=1 was used on a dirty tree" in written_exec["refuse_reason"]
