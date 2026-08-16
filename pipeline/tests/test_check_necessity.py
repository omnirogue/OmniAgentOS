"""Regression tests for the pre-file necessity gate (check_necessity).

The gate under test is loaded by APPLYING ``file_proposal.patch`` to the real
``pipeline/bridge/file_proposal.py`` and importing the result -- so these tests
exercise the exact bytes that will land, and ``check_necessity`` comes FROM the
patched ``file_proposal`` namespace (there is no separately-exec'd standalone).
The real bridge dir is on ``sys.path`` so the patched module's sibling imports
(``ledger_read``/``ledger_write``/``canonical``/``land_detect``) resolve.

Two families of tests:

  * SECURITY -- one per cross-lineage (grok) finding, asserting the STRUCTURED
    gate makes the exploit impossible by construction: a probe cannot supply a
    binary, a flag, ``--output``/``--pre``/``-O``/``--ext-diff``, a path-qualified
    ``argv[0]``, or an out-of-repo read. A repo-escape path is refused
    ``necessity.probe_unsafe``; an injection-shaped ``pattern`` is a harmless
    literal search string.
  * BEHAVIOUR -- the reject-code table (reproduce / landed / in_flight /
    dependency / unproven / probe_unsafe) plus the polarity guards (a retired
    twin is not in_flight; a declared feature with no probe is ADMITTED) and the
    three probe types.

Run: ``python -m pytest tests_check_necessity.py -q`` under an interpreter that
can import the bridge package (the repo's ``.venv`` interpreter).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _find_file_proposal() -> Path:
    import os
    env = os.environ.get("FILE_PROPOSAL")
    if env and Path(env).is_file():
        return Path(env)
    for parent in HERE.parents:
        cand = parent / "pipeline" / "bridge" / "file_proposal.py"
        if cand.is_file():
            return cand
    fallback = Path.home() / "OmniAgentOS" / "pipeline" / "bridge" / "file_proposal.py"
    if fallback.is_file():
        return fallback
    raise RuntimeError(
        "cannot locate pipeline/bridge/file_proposal.py; set $FILE_PROPOSAL")


def _load_module():
    """Import the REAL, patch-applied ``file_proposal`` and use ``check_necessity``
    from its namespace -- so these tests exercise the exact landed code (the moved
    test imports check_necessity FROM pipeline.bridge.file_proposal, NOT a
    standalone copy or an in-memory re-application of the patch)."""
    fp_path = _find_file_proposal()
    bridge = fp_path.parent
    if str(bridge) not in sys.path:
        sys.path.insert(0, str(bridge))
    import file_proposal as fp  # noqa: E402 -- bridge dir just added to sys.path
    assert hasattr(fp, "check_necessity"), (
        f"{fp_path} does not define check_necessity -- apply file_proposal.patch "
        "to the worktree's pipeline/bridge/file_proposal.py before running this suite")
    return fp


FP = _load_module()


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(path, "config", "user.email", "t@example.test")
    _git(path, "config", "user.name", "Necessity Test")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(repo: Path, relpath: str, content: str, msg: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _empty_queue(tmp_path: Path) -> Path:
    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True, exist_ok=True)
    (queue / "ledger.jsonl").write_text("")
    return queue


def _art(problem: str, paths: list[str], probe: dict | None,
         *, ident: str = "sha256:" + "a" * 64, risk_class: str | None = None,
         **extra) -> dict:
    art: dict = {
        "kind": "proposal",
        "id": ident,
        "paths": list(paths),
        "payload": {"problem": problem, "implementation_plan": "do the thing"},
    }
    if probe is not None:
        art["necessity_probe"] = probe
    if risk_class is not None:
        art["payload"]["risk_class"] = risk_class
    for key, value in extra.items():
        if key in ("branch", "base_sha", "supersedes", "necessity_probe"):
            art[key] = value
        else:
            art["payload"][key] = value
    return art


def _grep_probe(pattern: str, path: str) -> dict:
    return {"type": "grep_present", "pattern": pattern, "path": path}


def _checks(rep) -> dict[str, int]:
    return {r.check: r.code for r in rep.refusals}


# ==========================================================================
# SECURITY -- every grok finding, made impossible by construction
# ==========================================================================

# BLOCKER-1 (arbitrary write via --output) + MAJOR-3 (side effect then WARN):
# an injection-shaped pattern is a HARMLESS literal search string; no write.
def test_blocker1_output_flag_is_harmless_literal(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "f.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    sentinel = tmp_path / "prefile_gate_pwned"

    for pattern in (
        f"git diff --output={sentinel} HEAD",
        f"git show --output={sentinel} HEAD",
        "--output=/tmp/x",
    ):
        argv, why, _interp = FP._build_probe(_grep_probe(pattern, "f.py"))
        # the gate builds a fixed read-only grep argv; the pattern is the operand
        # of -e, never a flag, and there is no --output anywhere in the argv.
        assert argv is not None and why == ""
        assert argv[:6] == ["git", "--no-pager", "grep", "-q", "-F", "-e"]
        assert "--output" not in " ".join(argv[:6])
        art = _art("something", ["f.py"], _grep_probe(pattern, "f.py"),
                   risk_class="bug")
        rep = FP.Report()
        FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert not sentinel.exists(), "a probe must never write a file"


# BLOCKER-2 (rg --pre exec) + BLOCKER-3 (git grep -O exec) + MAJOR-2 (ext-diff/
# textconv) + MAJOR-1 (cat/git -C exfil): none of these tool names or flags is a
# probe TYPE, so they cannot be requested at all.
@pytest.mark.parametrize("bad_type", [
    "rg", "rg_pre", "grep_pager", "ext_diff", "textconv", "cat", "shell", "exec",
    "command", None, 123,
])
def test_unknown_probe_types_refused(bad_type):
    spec = {"type": bad_type, "pattern": "x", "path": "f.py"}
    argv, why, interp = FP._build_probe(spec)
    assert argv is None and interp is None
    assert "unknown probe type" in why or "typed object" in why


# BLOCKER-4 + MINOR-1 (path-qualified / PATH-shadowed binary): the proposal has
# no field that becomes argv[0]; the binary is always the literal "git", even if
# the spec tries to smuggle one.
def test_blocker4_binary_is_always_literal_git():
    for spec in (
        {"type": "grep_present", "pattern": "x", "path": "f.py",
         "binary": "/tmp/evil/git"},
        {"type": "grep_present", "pattern": "x", "path": "f.py", "command": "./git"},
    ):
        argv, why, _interp = FP._build_probe(spec)
        assert argv is not None and argv[0] == "git", (argv, why)


# MAJOR-1 (out-of-repo read/exfil): a repo-escaping path is refused, whatever the
# type. Absolute, parent-escape, and home-rooted paths cannot reach outside.
@pytest.mark.parametrize("bad_path", [
    "../../etc/passwd", "../secret", "/etc/passwd", "/Users/victim/.ssh/id_rsa",
    "~/secrets", "a/../../b", "-rf", "foo;rm -rf /", "a\nb",
])
def test_major1_repo_escape_paths_refused(bad_path):
    for spec in (
        {"type": "grep_present", "pattern": "x", "path": bad_path},
        {"type": "path_absent", "path": bad_path},
        {"type": "diff_from_ref", "ref": "HEAD", "path": bad_path},
    ):
        argv, why, interp = FP._build_probe(spec)
        assert argv is None and interp is None, (spec, argv)
        assert why


# MAJOR-2 (ext-diff/textconv via a crafted ref) + option injection: a ref that is
# a flag, has a path separator, or a `..` range is refused; the diff argv the gate
# builds always carries --no-ext-diff.
@pytest.mark.parametrize("bad_ref", [
    "--ext-diff", "--textconv", "--output=/tmp/x", "-O/bin/sh", "origin/main",
    "a..b", "HEAD; rm -rf /", "$(whoami)", "refs/heads/x",
])
def test_major2_unsafe_refs_refused(bad_ref):
    argv, why, interp = FP._build_probe(
        {"type": "diff_from_ref", "ref": bad_ref, "path": "f.py"})
    assert argv is None and interp is None, (bad_ref, argv)
    assert why


def test_diff_argv_is_read_only_and_no_ext_diff():
    argv, why, _interp = FP._build_probe(
        {"type": "diff_from_ref", "ref": "HEAD", "path": "f.py"})
    assert argv is not None and why == ""
    assert argv == ["git", "--no-pager", "diff", "--quiet", "--no-ext-diff",
                    "HEAD", "--", "f.py"]


# MINOR-2 asked for an extended unsafe-probe table -- here it is, structured.
@pytest.mark.parametrize("spec", [
    {"type": "grep_present", "pattern": "", "path": "f.py"},          # empty pattern
    {"type": "grep_present", "pattern": "x" * 9999, "path": "f.py"},  # over-long
    {"type": "grep_present", "pattern": "a\nb", "path": "f.py"},      # newline
    {"type": "grep_present", "pattern": "x"},                         # no path
    {"type": "path_absent"},                                         # no path
    {"type": "diff_from_ref", "path": "f.py"},                       # no ref
    "git grep -O/bin/sh LEAK",                                       # a bare string
    ["git", "diff"],                                                 # a list
])
def test_minor2_extended_unsafe_specs_refused(spec):
    argv, why, interp = FP._build_probe(spec)
    assert argv is None and interp is None, (spec, argv)
    assert why


# ==========================================================================
# BEHAVIOUR -- the reject-code table + polarity guards + the three probe types
# ==========================================================================

# 1. a grep_present probe that STILL reproduces on HEAD -> PASSES
def test_grep_present_reproduces_passes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "target.py", "def f():\n    return True  # TODO_FIX marker\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    art = _art("the TODO_FIX branch still exists on HEAD", ["target.py"],
               _grep_probe("TODO_FIX", "target.py"), risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)

    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert rep.exit_code == FP.EXIT_WRITTEN


# 2. a grep_present probe that NO LONGER reproduces -> necessity.reproduce (1)
def test_grep_present_no_longer_reproduces_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "target.py", "def f():\n    return False  # fixed, no marker\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    art = _art("the TODO_FIX branch still exists on HEAD", ["target.py"],
               _grep_probe("TODO_FIX", "target.py"), risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)

    checks = _checks(rep)
    assert checks.get("necessity.reproduce") == FP.EXIT_REFUSED_FIXABLE, checks


# 2b. path_absent -- the named path is missing (reproduces) -> PASSES;
#     present -> necessity.reproduce.
def test_path_absent_type(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "present.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    # the problem IS a missing path: renamed_away.py is absent -> reproduces
    art_missing = _art("renamed_away.py was deleted and must be restored",
                       ["renamed_away.py"],
                       {"type": "path_absent", "path": "renamed_away.py"},
                       risk_class="bug", new_paths=["renamed_away.py"])
    rep = FP.Report()
    FP.check_necessity(art_missing, queue, {"repo": repo}, rep)
    assert "necessity.reproduce" not in _checks(rep), [r.render() for r in rep.refusals]

    # the path is present -> the "missing path" problem is already solved
    art_present = _art("present.py is missing", ["present.py"],
                       {"type": "path_absent", "path": "present.py"},
                       risk_class="bug")
    rep2 = FP.Report()
    FP.check_necessity(art_present, queue, {"repo": repo}, rep2)
    assert _checks(rep2).get("necessity.reproduce") == FP.EXIT_REFUSED_FIXABLE


# 2c. diff_from_ref -- path differs from ref (reproduces) -> PASSES;
#     identical -> necessity.reproduce.
def test_diff_from_ref_type(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, "conf.ini", "value = old\n", "c0")
    _commit(repo, "conf.ini", "value = new\n", "c1")  # HEAD differs from base
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    # HEAD's conf.ini differs from the base commit -> reproduces
    art_diff = _art("conf.ini drifted from the base", ["conf.ini"],
                    {"type": "diff_from_ref", "ref": base, "path": "conf.ini"},
                    risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art_diff, queue, {"repo": repo}, rep)
    assert "necessity.reproduce" not in _checks(rep), [r.render() for r in rep.refusals]

    # identical to HEAD -> no divergence -> already reconciled
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    art_same = _art("conf.ini differs from HEAD", ["conf.ini"],
                    {"type": "diff_from_ref", "ref": head, "path": "conf.ini"},
                    risk_class="bug")
    rep2 = FP.Report()
    FP.check_necessity(art_same, queue, {"repo": repo}, rep2)
    assert _checks(rep2).get("necessity.reproduce") == FP.EXIT_REFUSED_FIXABLE


# 3. an already-landed branch proposal -> necessity.landed (exit 3)
def test_already_landed_branch_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, "keep.txt", "keep this file\n", "c0 base")
    _commit(repo, "feat.txt", "the landed line X\n", "c1 main adds feat")
    _git(repo, "checkout", "-q", "-b", "feature", base)
    _commit(repo, "feat.txt", "the landed line X\n", "feature adds feat")
    _git(repo, "checkout", "-q", "main")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    art = _art("feat.txt should carry line X", ["feat.txt"],
               _grep_probe("keep", "keep.txt"),  # a reproducing probe so 1-3 pass
               risk_class="bug", branch="feature", base_sha=base)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)

    checks = _checks(rep)
    assert checks.get("necessity.landed") == FP.EXIT_REFUSED_DO_NOT_RETRY, (
        checks, list(rep.warnings))


# 4. a duplicate of a LIVE non-terminal proposal -> necessity.in_flight (3)
def test_duplicate_of_live_proposal_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1  # LEAK marker\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))

    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    live_id = "sha256:" + "b" * 64
    live = {"kind": "proposal", "id": live_id, "paths": ["mod.py"],
            "payload": {"problem": "the widget leaks memory", "implementation_plan": "x"}}
    (queue / "proposals" / (live_id.replace(":", "_") + ".json")).write_text(json.dumps(live))
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": live_id, "event": "proposed", "ts": "2026-08-11T00:00:00Z"}) + "\n")

    art = _art("The widget leaks memory", ["mod.py"],
               _grep_probe("LEAK", "mod.py"), risk_class="bug",
               ident="sha256:" + "c" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)

    assert _checks(rep).get("necessity.in_flight") == FP.EXIT_REFUSED_DO_NOT_RETRY, _checks(rep)


def test_terminal_proposal_is_not_in_flight(tmp_path, monkeypatch):
    """Polarity guard: a RETIRED twin must NOT trip in_flight (no false drop)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1  # LEAK marker\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))

    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    dead_id = "sha256:" + "d" * 64
    dead = {"kind": "proposal", "id": dead_id, "paths": ["mod.py"],
            "payload": {"problem": "the widget leaks memory", "implementation_plan": "x"}}
    (queue / "proposals" / (dead_id.replace(":", "_") + ".json")).write_text(json.dumps(dead))
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": dead_id, "event": "proposed", "ts": "2026-08-11T00:00:00Z"}) + "\n"
        + json.dumps({"id": dead_id, "event": "rejected", "ts": "2026-08-11T01:00:00Z"}) + "\n")

    art = _art("The widget leaks memory", ["mod.py"],
               _grep_probe("LEAK", "mod.py"), risk_class="bug",
               ident="sha256:" + "e" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.in_flight" not in _checks(rep), [r.render() for r in rep.refusals]


# 5. an unsafe/malformed probe -> necessity.probe_unsafe (exit 1), and the
#    reproduce check must NOT run on it.
def test_unsafe_probe_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("something is wrong", ["mod.py"],
               {"type": "grep_present", "pattern": "x", "path": "../../etc/passwd"},
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)

    checks = _checks(rep)
    assert checks.get("necessity.probe_unsafe") == FP.EXIT_REFUSED_FIXABLE, checks
    assert "necessity.reproduce" not in checks


# 6. a bug-class proposal with NO probe -> necessity.unproven
def test_missing_probe_on_bug_is_unproven(tmp_path):
    queue = _empty_queue(tmp_path)
    art = _art("a real reproducible bug", ["mod.py"], None, risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {}, rep)
    assert _checks(rep).get("necessity.unproven") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)


def test_multiple_probes_is_unproven(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("bug", ["mod.py"],
               [_grep_probe("x", "mod.py"), _grep_probe("y", "mod.py")],
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.unproven") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)


# 7. POLARITY / over-refusal fix: a declared feature with no probe is ADMITTED.
def test_feature_without_probe_admitted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "keep.txt", "x\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # a net-new feature touching a NON-self-governing path, no reproducible problem
    art = _art("add a brand-new capability", ["src/newthing.py"], None,
               risk_class="feature", new_paths=["src/newthing.py"])
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert rep.exit_code == FP.EXIT_WRITTEN


def test_docs_proposal_admitted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "hi\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("expand the README", ["README.md"], None, risk_class="docs")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]


# 8. dependency-satisfied: a phantom base_sha dependency -> necessity.dependency
def test_dependency_phantom_sha_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("feature built on a base", ["mod.py"], None, risk_class="feature",
               dependencies=["needs base_sha deadbeefdeadbeefdeadbeef to exist"])
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.dependency") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)


# 9. undecided: a self-governing surface with no reproducible defect / decision
def test_self_governing_undecided(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "pipeline/schema/envelope.schema.json", "{}\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("change how the schema governs", ["pipeline/schema/envelope.schema.json"],
               None, risk_class="feature")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.undecided") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)


# 10. instrument-error polarity: a probe that cannot run WARNS, never refuses.
def test_probe_error_warns_not_refuses(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # diff against a ref that does not exist -> git errors (>=2) -> WARN skip
    art = _art("bug vs a missing ref", ["mod.py"],
               {"type": "diff_from_ref", "ref": "nonexistentref", "path": "mod.py"},
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.reproduce" not in _checks(rep), [r.render() for r in rep.refusals]
    assert any("necessity.reproduce" in w for w in rep.warnings), rep.warnings


# ==========================================================================
# RESIDUAL-HARDENING regressions (cross-lineage round-2: Gemini + opus-xhigh)
# ==========================================================================

# FIX 1 (Gemini MAJOR / NEW-1-NUL-BYTE-CRASH): a NUL byte in `branch` must NOT
# crash the filer. _run_probe catches ValueError AND _check_landed validates the
# branch, so an embedded NUL WARNS-and-skips instead of raising.
def test_fix1_nul_byte_in_branch_warns_not_crashes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "f.py", "x = 1  # NUL_MARKER\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # a reproducing probe so checks 1-3 pass, plus a branch carrying an embedded NUL
    art = _art("bug", ["f.py"], _grep_probe("NUL_MARKER", "f.py"),
               risk_class="bug", branch="feature\x00bug", base_sha="deadbeef")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)  # must NOT raise ValueError
    assert any("necessity.landed" in w and "usable ref name" in w
               for w in rep.warnings), rep.warnings
    assert rep.refusals == [], [r.render() for r in rep.refusals]


# FIX 1 defence-in-depth: the _valid_branch validator itself rejects control
# chars / leading dash / over-long, but ALLOWS a '/' (git branch names carry it).
def test_fix1_valid_branch_validator():
    assert FP._valid_branch("feature\x00bug")[0] is None
    assert FP._valid_branch("a\nb")[0] is None
    assert FP._valid_branch("-rf")[0] is None
    assert FP._valid_branch("")[0] is None
    assert FP._valid_branch(123)[0] is None
    assert FP._valid_branch("x" * 201)[0] is None
    # a '/' is legitimate in a git branch name and must be accepted
    assert FP._valid_branch("lane/graphrt-correctness-0810") == (
        "lane/graphrt-correctness-0810", "")


# FIX 2 (Gemini MINOR / MINOR-1-NORMALISED-LEADING-DASH): normpath('./-foo') ==
# '-foo'; the post-normpath belt-and-braces check must reject a normalised leading
# dash, not just a raw one.
def test_fix2_normpath_leading_dash_refused():
    relpath, why = FP._confine_path("./-foo")
    assert relpath is None, (relpath, why)
    assert why
    # a benign normalising path is still accepted
    ok, why2 = FP._confine_path("./pkg/./mod.py")
    assert ok == "pkg/mod.py" and why2 == ""


# FIX 3 (opus C1): a probe that REPRODUCED on a self-governing path proves a real
# defect -> ADMITTED, never routed to necessity.undecided.
def test_fix3_reproducing_probe_on_self_governing_admitted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "keep.txt", "x\n", "c0")  # pipeline/prompts/foo.md is absent
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("the prompt file foo.md was deleted and must be restored",
               ["pipeline/prompts/foo.md"],
               {"type": "path_absent", "path": "pipeline/prompts/foo.md"},
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert "necessity.undecided" not in _checks(rep)


# FIX 3: check-7 STILL reachable -- an undeclared self-governing proposal with no
# probe and no decision signal still fires necessity.undecided.
def test_fix3_check7_still_reachable(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "pipeline/bridge/gate_host.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # no risk_class, no probe, no decision -> _needs_probe False, probe_reproduced False
    art = _art("adjust how the gate governs itself",
               ["pipeline/bridge/gate_host.py"], None)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.undecided") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)


# FIX 4a (opus C2): identical problem on DISJOINT files -> admitted (warn), never
# frozen do-not-retry.
def test_fix4_identical_problem_disjoint_paths_admitted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "pkg/a.py", "x = 1\n", "c0")
    _commit(repo, "pkg/b.py", "y = 2\n", "c1")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    live_id = "sha256:" + "b" * 64
    live = {"kind": "proposal", "id": live_id, "paths": ["pkg/a.py"],
            "payload": {"problem": "fix the flaky test", "implementation_plan": "x"}}
    (queue / "proposals" / (live_id.replace(":", "_") + ".json")).write_text(json.dumps(live))
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": live_id, "event": "proposed"}) + "\n")
    # same terse problem, DIFFERENT file
    art = _art("fix the flaky test", ["pkg/b.py"], None, risk_class="feature",
               ident="sha256:" + "c" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.in_flight" not in _checks(rep), [r.render() for r in rep.refusals]
    assert any("disjoint files" in w for w in rep.warnings), rep.warnings


# FIX 4b: identical problem on OVERLAPPING files -> still refused do-not-retry.
def test_fix4_identical_problem_overlapping_paths_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "shared.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    live_id = "sha256:" + "b" * 64
    live = {"kind": "proposal", "id": live_id, "paths": ["shared.py"],
            "payload": {"problem": "fix the flaky test", "implementation_plan": "x"}}
    (queue / "proposals" / (live_id.replace(":", "_") + ".json")).write_text(json.dumps(live))
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": live_id, "event": "proposed"}) + "\n")
    art = _art("fix the flaky test", ["shared.py"], None, risk_class="feature",
               ident="sha256:" + "c" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.in_flight") == FP.EXIT_REFUSED_DO_NOT_RETRY, _checks(rep)


# FIX 4c: a terse (2-word) problem that is a raw substring of a longer live problem
# on a path-superset must NOT spuriously trip the contained/superset refusal.
def test_fix4_terse_substring_not_spuriously_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "a.py", "x = 1\n", "c0")
    _commit(repo, "b.py", "y = 2\n", "c1")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    live_id = "sha256:" + "b" * 64
    live = {"kind": "proposal", "id": live_id, "paths": ["a.py"],
            "payload": {"problem": "the authentication token cache leaks on retry",
                        "implementation_plan": "x"}}
    (queue / "proposals" / (live_id.replace(":", "_") + ".json")).write_text(json.dumps(live))
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": live_id, "event": "proposed"}) + "\n")
    # draft: 2-word problem that is a substring of the live problem, path superset
    art = _art("token cache", ["a.py", "b.py"], None, risk_class="feature",
               ident="sha256:" + "c" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.in_flight" not in _checks(rep), [r.render() for r in rep.refusals]


# FIX 5a (Gemini C / opus): a dependency whose cat-file COULD NOT RUN (instrument
# error) must WARN, never hard-refuse necessity.dependency as a phantom.
def test_fix5_dependency_instrument_error_warns(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    real = FP._run_probe

    def fake(argv, cwd, timeout=120):
        if "cat-file" in argv:
            return None, "git could not run (simulated instrument error)"
        return real(argv, cwd, timeout)

    monkeypatch.setattr(FP, "_run_probe", fake)
    art = _art("feature built on a base", ["mod.py"], None, risk_class="feature",
               dependencies=["needs base_sha deadbeefdeadbeefdeadbeef to exist"])
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.dependency" not in _checks(rep), _checks(rep)
    assert any("cat-file could not run" in w for w in rep.warnings), rep.warnings


# FIX 5b: a genuine phantom (every root ran, none resolved) STILL refuses.
def test_fix5_genuine_phantom_still_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("feature built on a base", ["mod.py"], None, risk_class="feature",
               dependencies=["needs base_sha deadbeefdeadbeefdeadbeef to exist"])
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.dependency") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)


# FIX 6 (opus MINOR): a grep_present probe whose path is NOT present in the probed
# repo (wrong root) exits 1 like a genuine "gone" -- confirm the path is tracked
# before refusing; if not, WARN rather than false-refuse necessity.reproduce.
def test_fix6_grep_present_wrong_root_warns_not_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "present.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # the probe path 'absent.py' is not tracked in this repo -> grep exits 1
    art = _art("a marker is missing from a file not in this root", ["absent.py"],
               _grep_probe("SOME_MARKER", "absent.py"), risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.reproduce" not in _checks(rep), [r.render() for r in rep.refusals]
    assert any("wrong root" in w for w in rep.warnings), rep.warnings


# ==========================================================================
# ROUND-3 hardening regressions (cross-lineage round-2 Gemini + Grok leads +
# systematic Sweep A/B). Polarity throughout: certainty refuses; every instrument
# error / malformed field / corrupt on-disk datum WARNS-and-skips, never refuses,
# never crashes check_necessity.
# ==========================================================================

# G1 (Gemini R2): a proposals/*.json with invalid UTF-8 bytes must NOT crash the
# filer. read_text(encoding="utf-8") raises UnicodeDecodeError (a ValueError, not
# an OSError/JSONDecodeError); the fix reads errors="replace" AND catches
# ValueError, so the corrupt neighbour is warn-skipped and dedup proceeds.
def test_g1_corrupt_utf8_neighbour_does_not_crash(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1  # LEAK marker\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    # a neighbour proposal file whose bytes are not valid UTF-8
    (queue / "proposals" / "corrupt.json").write_bytes(b"\xff\xfe\x00 not json \xc3\x28")
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": "sha256:" + "b" * 64, "event": "proposed"}) + "\n")

    art = _art("a fresh distinct problem", ["mod.py"],
               _grep_probe("LEAK", "mod.py"), risk_class="bug",
               ident="sha256:" + "c" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)  # must NOT raise
    assert "necessity.in_flight" not in _checks(rep), [r.render() for r in rep.refusals]


# G2 (Gemini R2): a defect-ASSERTING (bug-class) proposal on a self-governing path
# whose probe INSTRUMENT-ERRORS (code=None -> warn-skip) must be ADMITTED, never
# routed to necessity.undecided. check-7's guard is `not _needs_probe(art)`.
def test_g2_bug_probe_instrument_error_on_self_governing_admitted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "pipeline/prompts/PROMPT-x.md", "hi\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    def all_instrument_error(argv, cwd, timeout=120):
        return None, "simulated instrument error"

    monkeypatch.setattr(FP, "_run_probe", all_instrument_error)
    art = _art("the prompt has a reproducible defect",
               ["pipeline/prompts/PROMPT-x.md"],
               _grep_probe("MARKER", "pipeline/prompts/PROMPT-x.md"),
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.undecided" not in _checks(rep), [r.render() for r in rep.refusals]
    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert any("could not run" in w for w in rep.warnings), rep.warnings


# G3 (Gemini R2): '..foo' / '..env' are legit filenames (normpath leaves them
# unchanged), NOT traversals -- they must be ACCEPTED. A real '../x' escape is
# still refused.
def test_g3_dotdot_prefixed_filename_accepted():
    for name in ("..foo", "..env", "..gitignore-ish"):
        rel, why = FP._confine_path(name)
        assert rel == name, (name, rel, why)
    # genuine traversal still refused
    for bad in ("../x", "..", "../../etc/passwd"):
        rel, why = FP._confine_path(bad)
        assert rel is None and why, (bad, rel)


# Ga (Grok): _valid_branch must reject git rev-syntax metacharacters that a legal
# branch name never carries but git would interpret on `<branch>^{commit}`.
def test_ga_valid_branch_rejects_git_rev_metacharacters():
    for bad in (":foo", "a~1", "a^2", "a b", "a\tb", "foo?", "foo*", "foo[x]",
                "back\\slash", "reflog@{0}"):
        rel, why = FP._valid_branch(bad)
        assert rel is None and why, (bad, rel)
    # a legit branch name (slash, dot, dash, underscore, digits) is accepted
    assert FP._valid_branch("feat/ok-branch_1.2") == ("feat/ok-branch_1.2", "")


def test_ga_colon_branch_warns_not_refused(tmp_path, monkeypatch):
    """End-to-end: a ':foo' branch is an instrument-error ref -> warn-skip in
    _check_landed, never a crash and never a finding."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "f.py", "x = 1  # M\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("bug", ["f.py"], _grep_probe("M", "f.py"), risk_class="bug",
               branch=":foo", base_sha="deadbeef")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)  # must NOT raise
    assert any("necessity.landed" in w and "usable ref name" in w
               for w in rep.warnings), rep.warnings
    assert "necessity.landed" not in _checks(rep)


# Gb (Grok): a non-list `paths` (int/str/dict) must not raise TypeError anywhere
# it is iterated. `_art_paths` coerces it to [] defensively.
def test_gb_non_list_paths_does_not_crash(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    for bad_paths in (5, "mod.py", {"a": 1}, None):
        art = {"kind": "proposal", "id": "sha256:" + "c" * 64,
               "paths": bad_paths,
               "payload": {"problem": "p", "implementation_plan": "x",
                           "risk_class": "feature"}}
        rep = FP.Report()
        FP.check_necessity(art, queue, {"repo": repo}, rep)  # must NOT raise
    assert FP._art_paths({"paths": 5}) == []
    assert FP._art_paths({"paths": ["a.py", 3, "b.py", None]}) == ["a.py", "b.py"]
    assert FP._art_paths({}) == []


# Gc (Grok): _SELF_GOVERNING must match WHOLE path TOKENS, not raw substrings --
# 'aggregate_stats.py', 'propagate.py', 'gateway_client.py' are NOT self-governing.
def test_gc_self_governing_whole_token_match():
    not_self_gov = [
        ["omniagentos/aggregate_stats.py"],
        ["x/propagate.py"],
        ["x/gateway_client.py"],
        ["src/schematics_helper.py"],   # 'schema' is a substring of 'schematics'
        ["src/prompting_notes.py"],     # 'prompt' substring of 'prompting'
    ]
    for paths in not_self_gov:
        assert not FP._is_self_governing({"paths": paths}), paths
    is_self_gov = [
        ["configs/gates.d/x.yaml"],
        ["pipeline/prompts/PROMPT-x.md"],
        ["x/schema.json"],
        ["x/approvals.py"],
        ["a/gate.py"],
        ["b/approval_flow.py"],
    ]
    for paths in is_self_gov:
        assert FP._is_self_governing({"paths": paths}), paths


def test_gc_aggregate_feature_admitted_not_undecided(tmp_path, monkeypatch):
    """End-to-end: a feature touching 'aggregate_stats.py' must NOT be false-routed
    to necessity.undecided by a substring 'gate' match."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "omniagentos/aggregate_stats.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("improve aggregate stats", ["omniagentos/aggregate_stats.py"],
               None, risk_class="feature")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]


# Sweep A: a top-level backstop guard turns ANY unexpected raise inside the check
# body into a WARNING (never a crash, never a refusal). Simulate a helper raising.
def test_sweepA_internal_error_warns_not_crashes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("simulated internal fault")

    monkeypatch.setattr(FP, "_check_dependency", boom)
    art = _art("a plain feature", ["mod.py"], None, risk_class="feature")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)  # must NOT raise
    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert any("unexpected internal error" in w for w in rep.warnings), rep.warnings


# Sweep B: the bug/exempt class regexes match at a word START (\b), so a stem like
# 'fix' inside 'prefix' no longer forces an unrelated class into the bug lane,
# while genuine stems (bugfix, hotfix, regression, enhancement) still match.
def test_sweepB_class_regex_word_boundary():
    assert FP._BUG_CLASS_RE.search("bug")
    assert FP._BUG_CLASS_RE.search("bugfix")
    assert FP._BUG_CLASS_RE.search("hotfix")
    assert FP._BUG_CLASS_RE.search("regression")
    assert FP._BUG_CLASS_RE.search("reproduce")
    # substrings that are NOT a bug class
    assert not FP._BUG_CLASS_RE.search("prefix")
    assert not FP._BUG_CLASS_RE.search("suffix-work")
    assert not FP._BUG_CLASS_RE.search("humbug")   # 'bug' mid-word

    assert FP._EXEMPT_CLASS_RE.search("feature")
    assert FP._EXEMPT_CLASS_RE.search("enhancement")
    assert FP._EXEMPT_CLASS_RE.search("docs")
    assert FP._EXEMPT_CLASS_RE.search("ci")
    assert not FP._EXEMPT_CLASS_RE.search("citation")   # 'ci' mid-word (\bci\b)


def test_sweepB_prefix_class_not_forced_to_probe(tmp_path, monkeypatch):
    """A proposal declaring risk_class='prefix-cleanup' (contains 'fix' as a
    substring) is NOT a bug class, so it is not forced to carry a probe."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "src/newthing.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("rename a prefix helper", ["src/newthing.py"], None,
               risk_class="prefix-cleanup")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.unproven" not in _checks(rep), [r.render() for r in rep.refusals]


# ==========================================================================
# ROUND-4 hardening regressions (cross-lineage round-3 Gemini). Polarity
# preserved: certainty refuses; every instrument error / malformed input /
# ambiguity WARNS-and-skips, never refuses, never crashes check_necessity.
# ==========================================================================

# F1 (Gemini R3): a proposals/*.json that is PATHOLOGICALLY deep-nested makes
# json.loads raise RecursionError -- a subclass of RuntimeError, NOT of
# ValueError -- so it escaped the neighbour-read except, aborted _check_in_flight,
# and the top-level backstop then silently skipped checks 4-7 for the artifact.
# The fix broadens the neighbour-read except to catch RecursionError, so the bad
# neighbour is skipped and the loop CONTINUES -- a genuine duplicate behind it
# still trips in_flight, and nothing propagates.
#
# NOTE the RecursionError is injected by monkeypatching json.loads for the ONE
# pathological neighbour: CPython's C json accelerator does not reliably raise
# RecursionError at any practical nesting depth on every build (the pure-Python
# scanner and other runtimes do), so a literal deep-nested string would be a
# flaky, interpreter-dependent trigger. The class the except must catch is the
# invariant under test, and it is asserted directly below.
def test_f1_recursionerror_neighbour_does_not_abort_in_flight(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "mod.py", "x = 1  # LEAK marker\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))

    queue = tmp_path / "queue"
    (queue / "proposals").mkdir(parents=True)
    # a pathological neighbour: reading its bytes makes json.loads raise
    # RecursionError (deep nesting). It sorts FIRST so it is read before the live
    # twin -- if the except did not catch RecursionError the loop would abort here
    # and the in_flight refusal below would never be reached.
    sentinel = "DEEP_NESTED_NEIGHBOUR_SENTINEL"
    (queue / "proposals" / "aaa_pathological.json").write_text(sentinel)
    live_id = "sha256:" + "b" * 64
    live = {"kind": "proposal", "id": live_id, "paths": ["mod.py"],
            "payload": {"problem": "the widget leaks memory", "implementation_plan": "x"}}
    (queue / "proposals" / ("zzz_" + live_id.replace(":", "_") + ".json")).write_text(
        json.dumps(live))
    (queue / "ledger.jsonl").write_text(
        json.dumps({"id": live_id, "event": "proposed", "ts": "2026-08-11T00:00:00Z"}) + "\n")

    real_loads = FP.json.loads

    def loads_with_deep_nest(s, *a, **k):
        if isinstance(s, str) and sentinel in s:
            raise RecursionError("simulated deep-nested json")
        return real_loads(s, *a, **k)

    monkeypatch.setattr(FP.json, "loads", loads_with_deep_nest)

    art = _art("The widget leaks memory", ["mod.py"],
               _grep_probe("LEAK", "mod.py"), risk_class="bug",
               ident="sha256:" + "c" * 64)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)  # must NOT raise
    # the refusal STILL fires -- the RecursionError neighbour did not abort the loop
    assert _checks(rep).get("necessity.in_flight") == FP.EXIT_REFUSED_DO_NOT_RETRY, (
        _checks(rep), list(rep.warnings))


# F1 unit: RecursionError is a RuntimeError subclass and NOT a ValueError, so the
# pre-round-4 `except (OSError, ValueError)` could not have caught it -- which is
# exactly why the handler had to be broadened to `(OSError, ValueError,
# RecursionError)`. Guards against a future narrowing.
def test_f1_recursionerror_class_relationship():
    assert issubclass(RecursionError, RuntimeError)
    assert not issubclass(RecursionError, ValueError)
    assert not issubclass(RecursionError, OSError)


# F2a (Gemini R3): a diff_from_ref probe whose path exists in NEITHER <ref> NOR
# the worktree makes `git diff --quiet <ref> -- <path>` exit 0 (nothing vs
# nothing) -> _GONE -- an AMBIGUOUS wrong-root/nonexistent-path case, not a proven
# "already reconciled". It must WARN-and-skip, never refuse necessity.reproduce.
def test_f2_diff_from_ref_absent_both_sides_warns_not_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, "real.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # ghost.py is tracked at NEITHER `base` NOR the worktree -> diff exits 0
    art = _art("a file drifted from base", ["ghost.py"],
               {"type": "diff_from_ref", "ref": base, "path": "ghost.py"},
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.reproduce" not in _checks(rep), [r.render() for r in rep.refusals]
    assert any("absent from both" in w for w in rep.warnings), rep.warnings


# F2b: a diff_from_ref probe whose path DOES exist and is genuinely identical to
# <ref> is a REAL _GONE -> still refuses necessity.reproduce (the guard must not
# over-admit a truly-reconciled path).
def test_f2_diff_from_ref_identical_still_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "conf.ini", "value = same\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # conf.ini exists and is identical to HEAD -> genuine "already reconciled"
    art = _art("conf.ini differs from HEAD", ["conf.ini"],
               {"type": "diff_from_ref", "ref": head, "path": "conf.ini"},
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert _checks(rep).get("necessity.reproduce") == FP.EXIT_REFUSED_FIXABLE, (
        _checks(rep), list(rep.warnings))


# F2c: a diff_from_ref probe whose path exists ONLY at <ref> (deleted in the
# worktree) is still a decidable _GONE -> refuses (present on one side, so the
# exit 0 is not an instrument ambiguity). Guards the "at least one side" logic.
def test_f2_diff_from_ref_present_at_ref_only_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, "gone_now.py", "x = 1\n", "c0")
    _git(repo, "rm", "-q", "gone_now.py")
    _git(repo, "commit", "-q", "-m", "delete gone_now.py")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    # diff base..worktree for gone_now.py: exists at base, absent from worktree.
    # git diff --quiet reports a difference (deletion) -> exit 1 -> _REPRO -> admit.
    # (This asserts the guard does NOT spuriously WARN on a one-sided-present path
    # that legitimately reproduces.)
    art = _art("gone_now.py drifted", ["gone_now.py"],
               {"type": "diff_from_ref", "ref": base, "path": "gone_now.py"},
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert "necessity.reproduce" not in _checks(rep), [r.render() for r in rep.refusals]
    assert not any("absent from both" in w for w in rep.warnings), rep.warnings


# ==========================================================================
# ROUND-5 C-ii regression (confirming review): the anti-gaming ruling is a
# MISLABEL-SMELL WARN, not a refuse. An honestly-undeclared / mislabeled bug that
# omits both a bug class and a probe is ADMITTED (forcing a probe on everything is
# the starvation blocker), but flagged with a nudge-warning so the omission is
# observable. `_needs_probe` is UNCHANGED -- there is no defect-language refuse
# path anywhere. Polarity preserved: certainty refuses; a smell only WARNS.
# ==========================================================================

_MISLABEL_MARKER = "reads like a bug"


# C-ii unit: _needs_probe is UNCHANGED by the C-ii work -- defect-language in the
# problem text does NOT flip it. Declared bug -> True; declared feature -> False;
# a present probe/falsifier (undeclared) -> True; undeclared + defect text but no
# probe/falsifier -> still False (exempt; the smell is only a WARN, not a probe
# requirement).
def test_cii_needs_probe_unchanged():
    assert FP._needs_probe({"payload": {"risk_class": "bug", "problem": "x"}}) is True
    assert FP._needs_probe({"payload": {"risk_class": "feature", "problem": "x"}}) is False
    # undeclared class + a defect-claiming problem, NO probe/falsifier -> exempt
    assert FP._needs_probe({"payload": {"problem": "login crashes with a 500"}}) is False
    assert FP._needs_probe(
        {"payload": {"problem": "the parser returns the wrong value"}}) is False
    # a present probe (undeclared class) still forces the probe path
    assert FP._needs_probe(
        {"necessity_probe": {"type": "grep_present", "pattern": "x", "path": "f.py"},
         "payload": {"problem": "net-new"}}) is True
    # a declared falsifier (undeclared class) still forces the probe path
    assert FP._needs_probe(
        {"payload": {"problem": "net-new", "falsifier": "run the repro"}}) is True


# C-ii (a): an UNDECLARED plan whose problem talks like a defect but carries no
# class and no probe is ADMITTED (no refusal) AND the mislabel-smell WARN fires.
def test_cii_undeclared_defect_language_admitted_with_warn(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "src/login.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    for problem in (
        "login crashes with a 500 on submit",
        "the parser returns the wrong value for nested keys and is broken",
    ):
        art = _art(problem, ["src/login.py"], None)  # no risk_class, no probe
        rep = FP.Report()
        FP.check_necessity(art, queue, {"repo": repo}, rep)
        assert rep.refusals == [], [r.render() for r in rep.refusals]
        assert any(_MISLABEL_MARKER in w for w in rep.warnings), (problem, rep.warnings)


# C-ii (b): a genuine net-new plan with NO defect language is a CLEAN admit -- no
# refusal and NO mislabel-smell warning (no over-flagging of honest net-new work).
def test_cii_netnew_no_defect_language_clean_admit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "src/ui.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("users want a dark-mode toggle", ["src/ui.py"], None)  # undeclared
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert not any(_MISLABEL_MARKER in w for w in rep.warnings), rep.warnings


# C-ii (c): a net-new plan whose text merely MENTIONS a defect-ish word ('error')
# still ADMITS -- the WARN is cheap and acceptable, the polarity is never a refuse.
def test_cii_netnew_incidental_defect_word_still_admits(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "src/upload.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("add error handling to the upload flow", ["src/upload.py"], None)
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]  # never a refuse


# C-ii (d): a DECLARED bug is unchanged -- it needs a probe, so a missing probe
# fires necessity.unproven and the mislabel-smell WARN does NOT fire (the smell is
# only for the exempt/undeclared branch).
def test_cii_declared_bug_unchanged_no_smell_warn(tmp_path):
    queue = _empty_queue(tmp_path)
    art = _art("a real reproducible bug crashes on startup", ["mod.py"], None,
               risk_class="bug")
    rep = FP.Report()
    FP.check_necessity(art, queue, {}, rep)
    assert _checks(rep).get("necessity.unproven") == FP.EXIT_REFUSED_FIXABLE, _checks(rep)
    assert not any(_MISLABEL_MARKER in w for w in rep.warnings), rep.warnings


# C-ii residual: a DECLARED exempt class (feature) whose problem text carries
# defect language is left QUIET -- admitted with NO mislabel-smell warning. This
# is the accepted residual (flagging declared net-new is noise); a deliberate
# mislabel of a defect as a feature is NOT flagged here.
def test_cii_declared_exempt_with_defect_text_stays_quiet(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "src/login.py", "x = 1\n", "c0")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    queue = _empty_queue(tmp_path)
    art = _art("the login flow crashes with a bug", ["src/login.py"], None,
               risk_class="feature")
    rep = FP.Report()
    FP.check_necessity(art, queue, {"repo": repo}, rep)
    assert rep.refusals == [], [r.render() for r in rep.refusals]
    assert not any(_MISLABEL_MARKER in w for w in rep.warnings), rep.warnings


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
