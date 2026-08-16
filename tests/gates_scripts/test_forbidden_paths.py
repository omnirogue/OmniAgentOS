from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/gates/forbidden-paths.sh"

# Git subcommands that can render a PATH into their output. The invariant is a
# property of the SUBCOMMAND, not of any one flag: keying the guard on the
# literal `--name-only` let `git log --name-status | cut -f2-` feed $SWEPT
# unpinned while the guard reported clean.
_PATH_PRODUCING_GIT = frozenset(
    {"log", "diff", "show", "status", "ls-tree", "ls-files", "diff-tree", "diff-index"}
)

# `git`, then any GLOBAL options, then the subcommand. The globals have to be
# consumed explicitly, because the pinned form is `git -c core.quotePath=false
# log ...` -- the substring "git log" never appears in a correctly pinned walk,
# so a naive search for it matches only the BROKEN ones. Note `-c` AFTER the
# subcommand (`git log --merges -c`) is the combined-diff flag, not a config,
# and is correctly not treated as a pin.
_GIT_INVOCATION = re.compile(
    r"\bgit((?:\s+-[cC]\s+\S+|\s+--no-pager|\s+--git-dir=\S+|\s+--work-tree=\S+)*)"
    r"\s+([a-z][a-z-]*)"
)


def _unpinned_git_path_walks(source: str) -> list[str]:
    """Statements invoking a path-producing git subcommand without the quotePath pin.

    Backslash-continued lines are joined FIRST, so the unit scanned is the
    logical statement rather than the physical line: wrapping a long `git log`
    across two lines is a legitimate reformat that leaves the walk correctly
    pinned, and scanning raw lines would refuse a correct change.
    """
    joined = source.replace("\\\n", " ")
    offenders = []
    for statement in joined.splitlines():
        if statement.lstrip().startswith("#"):
            continue
        for globals_, subcommand in _GIT_INVOCATION.findall(statement):
            if subcommand not in _PATH_PRODUCING_GIT:
                continue
            if "core.quotePath=false" not in globals_:
                offenders.append(statement.strip())
                break
    return offenders


# Every git invocation carries the identity, not just the ones that obviously
# commit. `git merge` resolves a committer up front and dies with exit 128 --
# "Committer identity unknown" -- even under `--no-commit`, so pinning the
# identity at the `commit` call site alone made the merge fixtures pass on a
# developer Mac (git auto-detects from gecos) and fail on a CI runner, which has
# no ~/.gitconfig to auto-detect from. Injecting it here means the next helper
# that creates a commit cannot reintroduce that split.
_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _candidate_repo(tmp_path: Path) -> Path:
    """A checkout holding the real gate, on branch ``candidate`` off ``main``."""
    repo = tmp_path / "checkout"
    gate = repo / "scripts/gates/forbidden-paths.sh"
    gate.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, gate)

    _git(repo, "init", "-q", "-b", "main")
    # Force the ADVERSARIAL default explicitly. core.quotePath is client config,
    # so on a machine where the operator has set it to false globally, a mutant
    # gate with its `-c core.quotePath=false` pins removed would still see raw
    # non-ASCII paths and the quoting tests would pass against it. Pinning it
    # here makes their kill-power a property of the fixture, not of the host.
    _git(repo, "config", "core.quotePath", "true")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "candidate")
    return repo


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("REPO", None)
    return subprocess.run(
        [str(repo / "scripts/gates/forbidden-paths.sh"), "candidate", "main"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_repo_is_derived_from_the_gate_script_checkout(tmp_path: Path) -> None:
    """The standalone gate must inspect its own checkout off the operator's Mac."""
    repo = tmp_path / "checkout"
    gate = repo / "scripts/gates/forbidden-paths.sh"
    gate.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, gate)

    _git(repo, "init", "-q", "-b", "main")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "ARCHI.md").write_text("generated\n", encoding="utf-8")
    _git(repo, "add", "ARCHI.md")
    _git(repo, "commit", "-qm", "candidate")

    env = os.environ.copy()
    env.pop("REPO", None)
    result = subprocess.run(
        [str(gate), "candidate", "main"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED generated" in result.stdout


def test_secret_added_then_deleted_before_the_tip_is_refused(tmp_path: Path) -> None:
    """A `git merge --no-ff` lands the blob even when the tip no longer shows it.

    The net three-dot diff is empty here, which used to take the "no files
    changed" early-out and exit 0 — the same hole scripts/merge-gate.sh:866-875
    closed on its own secret check.
    """
    repo = _candidate_repo(tmp_path)
    (repo / "configs").mkdir()
    (repo / "configs/accounts.yaml").write_text("token: sk-live-x\n", encoding="utf-8")
    _commit(repo, "add accounts.yaml")
    _git(repo, "rm", "-q", "configs/accounts.yaml")
    _commit(repo, "remove it again")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED secret-bearing path" in result.stdout
    assert "configs/accounts.yaml" in result.stdout


def test_dotenv_below_the_repo_root_is_refused(tmp_path: Path) -> None:
    """`^\\.env.*$` only ever matched the repo root; dashboard/.env.local is the
    standard Next.js secret file and nothing in .gitignore stops it."""
    repo = _candidate_repo(tmp_path)
    (repo / "dashboard").mkdir()
    (repo / "dashboard/.env.local").write_text(
        "DATABASE_URL=postgres://u:p@h/d\n", encoding="utf-8"
    )
    _commit(repo, "add dashboard env")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED secret-bearing path" in result.stdout
    assert "dashboard/.env.local" in result.stdout


def test_root_dotenvrc_is_still_refused(tmp_path: Path) -> None:
    """Widening must be a strict superset: `.envrc` matched `^\\.env.*$` before."""
    repo = _candidate_repo(tmp_path)
    (repo / ".envrc").write_text("export TOKEN=x\n", encoding="utf-8")
    _commit(repo, "add .envrc")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert ".envrc" in result.stdout


def test_secret_introduced_by_a_merge_commit_is_refused(tmp_path: Path) -> None:
    """`--no-merges` skipped the one commit that carried the secret.

    The blob enters and leaves the candidate exclusively through merge commits:
    the first merge resolves by writing `dashboard/.env.local` (a path on
    NEITHER parent), the second removes it. No ordinary commit ever names it and
    the net diff is clean, so the pre-fix sweep saw only the three .txt files
    and printed "no forbidden paths in 3 changed file(s)", exit 0 -- while the
    key stayed permanently reachable from the first merge's tree.
    """
    repo = _candidate_repo(tmp_path)
    (repo / "candidate.txt").write_text("c\n", encoding="utf-8")
    _commit(repo, "candidate")

    _git(repo, "checkout", "-q", "-b", "topic-add", "main")
    (repo / "topic-add.txt").write_text("x\n", encoding="utf-8")
    _commit(repo, "topic add")

    # An evil merge: the merge commit itself introduces the secret.
    _git(repo, "checkout", "-q", "candidate")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "topic-add")
    (repo / "dashboard").mkdir()
    (repo / "dashboard/.env.local").write_text("OPENAI_API_KEY=sk-live-x\n", encoding="utf-8")
    _commit(repo, "merge topic-add")

    _git(repo, "checkout", "-q", "-b", "topic-delete", "candidate")
    (repo / "topic-delete.txt").write_text("y\n", encoding="utf-8")
    _commit(repo, "topic delete")

    # ...and a second merge takes it back out, clearing the net diff.
    _git(repo, "checkout", "-q", "candidate")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "topic-delete")
    _git(repo, "rm", "-q", "dashboard/.env.local")
    _commit(repo, "merge topic-delete")

    assert "dashboard/.env.local" not in subprocess.run(
        ["git", "diff", "--name-only", "main...candidate"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout, "precondition: the net diff must be clean, or this tests the wrong hole"

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED secret-bearing path" in result.stdout
    assert "dashboard/.env.local" in result.stdout


def test_an_ordinary_merge_does_not_manufacture_a_refusal(tmp_path: Path) -> None:
    """The merge pass must add paths, never invent them.

    `-c` lists only what differs from EVERY parent, so a merge that simply takes
    one side's file contributes nothing of its own. Without this, widening the
    sweep to merges would refuse every candidate that merged a side branch.
    """
    repo = _candidate_repo(tmp_path)
    (repo / "candidate.txt").write_text("c\n", encoding="utf-8")
    _commit(repo, "candidate")

    _git(repo, "checkout", "-q", "-b", "topic", "main")
    (repo / "topic.txt").write_text("x\n", encoding="utf-8")
    _commit(repo, "topic")

    _git(repo, "checkout", "-q", "candidate")
    _git(repo, "merge", "-q", "--no-ff", "topic", "-m", "merge topic")

    result = _run_gate(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" not in result.stdout


def test_generated_oracle_reverted_within_the_branch_still_passes(tmp_path: Path) -> None:
    """The generated/ownerless guards stay TREE-shaped, deliberately.

    merge-gate.sh:829-831: a reverted WORKBOOK.md edit in a lane's history
    collides with nobody. Only the SECRET question is history-shaped, so this
    pins the split rather than letting the fix widen all three patterns.
    """
    repo = _candidate_repo(tmp_path)
    (repo / "ARCHI.md").write_text("generated\n", encoding="utf-8")
    _commit(repo, "touch the oracle")
    _git(repo, "rm", "-q", "ARCHI.md")
    _commit(repo, "put it back the way main had it")

    result = _run_gate(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" not in result.stdout


# A host where git cannot invent an identity: no global or system config to read
# one from, and auto-detection disabled outright. This is what a CI runner is,
# and `-c` on the command line still overrides it -- so only a git call that
# supplies no identity of its own dies here.
_IDENTITYLESS_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
}
# The child run must not re-enter this test. `--deselect` alone does NOT hold:
# pytest matches a deselect nodeid against the rootdir-relative path, so an
# absolute one is silently accepted and matches nothing -- measured, the child
# ran this test again and the grandchild only stopped at the 120s timeout. The
# env marker is the guard; the relative deselect just keeps the child's report
# free of a skip line.
_PROBE = "OMNI_FORBIDDEN_PATHS_IDENTITY_PROBE"
_SELF = "test_no_fixture_here_needs_the_host_to_have_a_git_identity"
_REL = "tests/gates_scripts/test_forbidden_paths.py"


def test_no_fixture_here_needs_the_host_to_have_a_git_identity() -> None:
    """Re-run this module where git has no identity to fall back on.

    The merge fixtures were green on the operator's Mac and exit-128 on CI for
    two runs before anyone read the stderr, because the difference was the host
    and not the branch. Asserting the property over the WHOLE module rather than
    over the two tests that happened to break means a fixture added later is
    covered by construction -- a hand-listed pair would go stale the first time
    someone adds a third repo-building test.
    """
    if os.environ.get(_PROBE):
        pytest.skip("this is the child run; the probe does not probe itself")

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "EMAIL"))}
    env.update(_IDENTITYLESS_GIT)
    env[_PROBE] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            _REL,
            "--deselect",
            f"{_REL}::{_SELF}",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--timeout=120",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "a fixture in this module depends on the host having a git identity:\n"
        + result.stdout[-4000:]
        + result.stderr[-2000:]
    )


# A directory whose name carries a literal newline. Git C-quotes it in
# --name-only output no matter what core.quotePath is set to, so the anchored
# rules below cannot classify it — `^configs/accounts\.yaml$` never matches
# `"ordinary\nconfigs/accounts.yaml"`.
_UNSCANNABLE_DIR = "ordinary\nconfigs"

# Ordinary filenames that the DEFAULT core.quotePath=true also C-quotes, purely
# because they carry bytes >= 0x80. Nothing about them is unscannable.
_NON_ASCII_NAMES = ("café-notes.md", "日本語.md", "plan-🚀.md")


def test_git_quoted_forbidden_path_is_refused_instead_of_being_split(tmp_path: Path) -> None:
    """An unscannable path must refuse, not read as clean.

    The anchored greps cannot classify a C-quoted name, so before this the gate
    reported "no forbidden paths" for a tree containing an accounts.yaml.
    """
    repo = _candidate_repo(tmp_path)
    sneaky = repo / _UNSCANNABLE_DIR / "accounts.yaml"
    sneaky.parent.mkdir(parents=True)
    sneaky.write_text("not a credential, but the path is forbidden\n", encoding="utf-8")
    _commit(repo, "add an unscannable path")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED unquotable path" in result.stdout


def test_unquotable_path_deleted_before_the_tip_is_still_refused(tmp_path: Path) -> None:
    """The refusal is asked of $SWEPT, not $FILES.

    Same hole as ``test_secret_added_then_deleted_before_the_tip_is_refused``:
    the net three-dot diff is EMPTY, so a $FILES-shaped check takes the "no files
    changed" early-out and exits 0 while `git merge --no-ff` still lands the blob.
    """
    repo = _candidate_repo(tmp_path)
    sneaky = repo / _UNSCANNABLE_DIR / "accounts.yaml"
    sneaky.parent.mkdir(parents=True)
    sneaky.write_text("token: sk-live-x\n", encoding="utf-8")
    _commit(repo, "add an unscannable path")
    _git(repo, "rm", "-q", "-r", _UNSCANNABLE_DIR)
    _commit(repo, "remove it again")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED unquotable path" in result.stdout


def test_non_ascii_filenames_are_not_mistaken_for_unquotable_paths(tmp_path: Path) -> None:
    """core.quotePath=true quotes every byte >= 0x80, so the refusal above would
    fire on an ordinary accented, CJK or emoji filename. A gate that refuses
    ordinary filenames gets disabled, which is worse than the hole it closed."""
    repo = _candidate_repo(tmp_path)
    docs = repo / "docs"
    docs.mkdir()
    for name in _NON_ASCII_NAMES:
        (docs / name).write_text("ordinary documentation\n", encoding="utf-8")
    _commit(repo, "add ordinary non-ASCII docs")

    result = _run_gate(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" not in result.stdout
    assert "no forbidden paths in 3 changed file(s)" in result.stdout


def test_invalid_utf8_byte_in_a_secret_path_does_not_fail_open(tmp_path: Path) -> None:
    """A filter that dies must refuse, not report an empty change set.

    Under a UTF-8 locale BSD sed rejects a path carrying an invalid byte with
    "illegal byte sequence" and exits 1. With no `set -e`, $SWEPT went empty and
    the gate answered "no files changed", exit 0, for a branch carrying
    `.env-\\377` — a tool error rendered as the most favourable possible answer.
    """
    repo = _candidate_repo(tmp_path)
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"token: sk-live-x\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    # git cannot be asked to create this path from the filesystem on macOS, so
    # the tree is written directly — which is exactly how it would arrive in a
    # candidate's history.
    entry = b"100644 blob " + blob.encode() + b"\t.env-\xff\x00"
    tree = (
        subprocess.run(
            ["git", "mktree", "-z"],
            cwd=repo,
            input=entry,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    parent = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    commit = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            "invalid-byte secret path",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # update-ref, not `branch -f`: candidate is the checked-out branch, and this
    # path cannot be materialised in a working tree on macOS anyway. The gate
    # reads commit objects, so the worktree is irrelevant to it.
    _git(repo, "update-ref", "refs/heads/candidate", commit)

    # Read as BYTES so the test itself cannot be what fails, then assert the
    # gate's own output is strict-UTF-8 clean. scripts/gate-runner.py:184 runs
    # every gate with `text=True`; once the walks pin quotePath=false git hands
    # back this path's raw bytes, and echoing them into stdout killed the runner
    # with UnicodeDecodeError instead of recording the refusal.
    env = os.environ.copy()
    env.pop("REPO", None)
    result = subprocess.run(
        [str(repo / "scripts/gates/forbidden-paths.sh"), "candidate", "main"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")

    # `!= 0` would NOT be enough, and the difference is the whole point of this
    # test. Exit 2 is "could not run" -- the filter died and the script refused
    # on principle. Exit 1 with the secret named is the gate CLASSIFYING the
    # path. Both are non-zero, so a `!= 0` assertion passes identically whether
    # LC_ALL=C is present or deleted, which makes it incapable of catching the
    # regression it exists to prevent. Verified: removing `export LC_ALL=C`
    # left the loose assertion green.
    assert result.returncode == 1, stdout + result.stderr.decode("utf-8", "replace")
    assert "REFUSED secret-bearing path" in stdout
    assert "no files changed" not in stdout
    result.stdout.decode("utf-8")  # strict: the runner does exactly this


def test_every_applicable_reason_is_reported_in_one_run(tmp_path: Path) -> None:
    """The unquotable refusal reports alongside the others, not instead of them.

    Revealing one reason per run costs a repair/re-run cycle per reason, and the
    base reported all applicable categories at once.
    """
    repo = _candidate_repo(tmp_path)
    sneaky = repo / _UNSCANNABLE_DIR / "accounts.yaml"
    sneaky.parent.mkdir(parents=True)
    sneaky.write_text("unscannable\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("generated\n", encoding="utf-8")
    (repo / "configs").mkdir()
    (repo / "configs/accounts.yaml").write_text("token: sk-live-x\n", encoding="utf-8")
    _commit(repo, "three reasons at once")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED unquotable path" in result.stdout
    assert "REFUSED generated" in result.stdout
    assert "REFUSED secret-bearing path" in result.stdout


def test_refusal_reports_the_path_byte_for_byte(tmp_path: Path) -> None:
    """POSIX `echo` interprets backslash escapes, and these paths are precisely
    the ones containing backslashes: a C-quoted path prints as
    `"ordinary\\nconfigs/accounts.yaml"` — backslash then n — and echo turned
    that into a real newline, so the gate misreported the path it refused."""
    repo = _candidate_repo(tmp_path)
    sneaky = repo / _UNSCANNABLE_DIR / "accounts.yaml"
    sneaky.parent.mkdir(parents=True)
    sneaky.write_text("unscannable\n", encoding="utf-8")
    _commit(repo, "add an unscannable path")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    refusal = next(line for line in result.stdout.splitlines() if "REFUSED unquotable path" in line)
    assert r'"ordinary\nconfigs/accounts.yaml"' in refusal, refusal


def test_quote_and_backslash_names_are_refused_too(tmp_path: Path) -> None:
    """`"` and `\\` are quoted whatever core.quotePath says, and are equally
    unmatchable by the anchored rules — so they fail closed as well.

    Pinned because the boundary is not "control characters", which is the
    plausible-sounding description that produced the original defect: the grep
    was built to match a sentence rather than to match what git does.
    """
    repo = _candidate_repo(tmp_path)
    (repo / 'my"file.md').write_text("legal, but unmatchable\n", encoding="utf-8")
    _commit(repo, "add a name containing a double quote")

    result = _run_gate(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED unquotable path" in result.stdout


def test_non_ascii_path_deleted_before_the_tip_is_not_mistaken_for_unquotable(
    tmp_path: Path,
) -> None:
    """The HISTORY walk needs core.quotePath=false as much as the tree walk does.

    Pinning it on `git diff` alone leaves `git log --name-only` quoting bytes
    >= 0x80, and since the refusal reads $SWEPT, an ordinary non-ASCII file that
    existed only mid-branch would be refused as unscannable. This is the case
    that distinguishes one patched walk from both.
    """
    repo = _candidate_repo(tmp_path)
    docs = repo / "docs"
    docs.mkdir()
    (docs / _NON_ASCII_NAMES[0]).write_text("ordinary documentation\n", encoding="utf-8")
    _commit(repo, "add an ordinary non-ASCII doc")
    _git(repo, "rm", "-q", f"docs/{_NON_ASCII_NAMES[0]}")
    _commit(repo, "remove it again")

    result = _run_gate(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" not in result.stdout


def test_every_path_producing_walk_pins_core_quotepath() -> None:
    """Any future walk added to $SWEPT must pin core.quotePath, or it reopens this.

    Deliberately a source-level invariant rather than a behavioural one, because
    the property IS a property of the git invocation: quoting is decided by the
    config in effect when git renders the name, so there is nothing to observe
    except the command line that produced it.

    The hazard was sequencing, and it has since been demonstrated rather than
    predicted. While this branch sat unmerged, main landed e8fcbcd2, which added
    exactly the anticipated THIRD walk -- `git log --merges -c --name-only` --
    and landed it UNPINNED. That put ordinary accented, CJK and emoji filenames
    back in front of the `^"` refusal for any path a merge commit introduces.
    This assertion is what caught it on rebase; the gate now pins all three.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    # Vacuity check, and a discrimination proof in one: strip the pins and the
    # guard must flag EVERY walk. Asserting only "no offenders" would pass just
    # as happily against a file with no walks in it at all, or against a
    # detector that silently stopped matching -- the guard has to be shown
    # capable of failing before its silence means anything.
    unpinned_mutant = _unpinned_git_path_walks(source.replace("-c core.quotePath=false ", ""))
    assert len(unpinned_mutant) >= 3, (
        "this guard is watching the wrong file or has stopped matching: with every "
        f"pin removed it should flag all three walks, it flagged {unpinned_mutant}"
    )

    unpinned = _unpinned_git_path_walks(source)
    assert not unpinned, (
        f"every git walk feeding $SWEPT must pin core.quotePath=false, these do not: {unpinned}"
    )


def test_the_walk_guard_is_not_evaded_by_a_different_flag_spelling() -> None:
    """The guard must key on the SUBCOMMAND, not on one flag's spelling.

    Found in cross-lineage review and reproduced: the first version of this
    guard selected statements containing the literal `--name-only`, so a walk
    written `git log --name-status ... | cut -f2-` fed $SWEPT identically,
    contained no `--name-only`, and was waved through UNPINNED -- the guard
    returning a clean answer for an input it could not actually see, which is
    the same favourable-absence shape the gate itself exists to stop.

    A guard that cannot fail is worse than no guard, because it is trusted. So
    this pins the guard's own kill-power against the exact evasion.
    """
    evasions = [
        'X=$(git log --name-status "$BASE..$BRANCH" | cut -f2-)',
        'X=$(git diff --raw "$BASE...$BRANCH")',
        'X=$(git -c color.ui=false log --name-only "$BASE..$BRANCH")',
        'X=$(git ls-tree -r --name-only "$BRANCH")',
    ]
    for evasion in evasions:
        assert _unpinned_git_path_walks(evasion), f"guard failed to flag: {evasion}"

    # ...and it must NOT fire on correctly pinned forms, including a legitimate
    # reformat that wraps the invocation across a continuation.
    pinned = [
        'X=$(git -c core.quotePath=false log --name-status "$BASE..$BRANCH")',
        'X=$(git -c core.quotePath=false \\\n  diff --name-only "$BASE...$BRANCH")',
        "BASE_SHA=$(git rev-parse HEAD)",  # produces no path; must not be forced to pin
    ]
    for ok in pinned:
        assert not _unpinned_git_path_walks(ok), f"guard false-refused: {ok}"


def test_a_classifier_that_could_not_run_refuses_instead_of_reporting_clean(
    tmp_path: Path,
) -> None:
    """grep exiting >=2 is a tool failure, and must never read as "nothing found".

    grep's exit status carries three meanings -- 0 matched, 1 no match, >=2 grep
    itself failed -- and the `|| true` that used to sit on every classifier
    erased the difference between the last two. A grep that could not run
    yielded an empty set, which each check reads as "nothing forbidden": the
    gate printed "no forbidden paths in  changed file(s)" and exited 0 with
    configs/accounts.yaml present in the branch.

    The empty count in that message is the tell, and it is the whole defect
    class this gate keeps re-learning: an instrument error rendered as the most
    favourable possible answer.
    """
    repo = _candidate_repo(tmp_path)
    (repo / "configs").mkdir()
    (repo / "configs/accounts.yaml").write_text("token: sk-live-x\n", encoding="utf-8")
    _commit(repo, "carry a secret-bearing path")

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "grep").write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    (shim_dir / "grep").chmod(0o755)

    env = os.environ.copy()
    env["REPO"] = str(repo)
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    result = subprocess.run(
        [str(repo / "scripts/gates/forbidden-paths.sh"), "candidate", "main"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    # 2 is "could not run", which the registry contract treats as a refusal.
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no forbidden paths" not in result.stdout
