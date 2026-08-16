"""land_detect.py — content-equivalence landing detection.

No network and no real repository: every test builds a throwaway git repo in
tmp_path whose history reproduces one real landing shape from the estate.

The shapes are taken from a hand-audited pass over 20 live PRs, and the names
below match what that pass found:

  identical landing   PRs #38 #60 #77 — cherry-picked, patch-id equivalent
  superset landing    PR  #19        — re-authored on main with an EXTRA step
                                       inserted inside the added block, so
                                       neither blob equality nor reverse-apply
                                       sees it; only the line rung plus the
                                       landing commit's own attestation do
  partial landing     PR  #42        — 5 of 9 files on main, one commit missing
  train-only landing  PRs #92-#100   — landed on a train branch, NOT on main
  unlanded            the other ten
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bridge"))

from land_detect import (  # noqa: E402
    InstrumentError,
    LandDetector,
    Verdict,
    _parse_name_status,
    _split_hunks,
    implausible_close_rate,
)

# ------------------------------------------------------------------ fixtures


def _git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# fixture\nbaseline\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _write(repo: pathlib.Path, path: str, text: str) -> None:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo: pathlib.Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _branch_from(repo: pathlib.Path, name: str, start: str) -> None:
    _git(repo, "checkout", "-q", "-b", name, start)


def _detector(repo: pathlib.Path, **kw) -> LandDetector:
    d = LandDetector(repo, "main", **kw)
    d.self_test()
    return d


# ------------------------------------------------------- the landing shapes


def test_identical_landing_is_closable_and_names_the_sha(tmp_path):
    """PRs #38/#60/#77: cherry-picked to main. Different SHA, same patch."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    _branch_from(repo, "pr", base)
    _write(repo, "src/a.py", "def a():\n    return 1\n")
    _write(repo, "tests/test_a.py", "def test_a():\n    assert True\n")
    head = _commit(repo, "feat: add a")

    _git(repo, "checkout", "-q", "main")
    # main moves on first, so the cherry-pick genuinely produces a NEW sha —
    # a cherry-pick onto an unchanged base reproduces the original commit
    # exactly, which is not the shape this test is about.
    _write(repo, "unrelated.txt", "main moved on\n")
    _commit(repo, "chore: unrelated")
    _git(repo, "cherry-pick", "--no-edit", head)
    landing = _git(repo, "rev-parse", "HEAD")
    assert landing != head

    v = _detector(repo).classify(head, number=38, branch="pr")
    assert v.status == "landed"
    assert v.closable is True
    assert v.landing_kind == "patch-id"
    assert v.landing_shas == [landing]
    assert {f.rung for f in v.files} == {"identical"}
    assert v.cherry_unlanded == 0


def test_superset_landing_needs_an_attestation_and_gets_one(tmp_path):
    """PR #19: main carries the PR's block PLUS an extra line inside it.

    Blob equality fails, reverse-apply fails (the added lines are no longer
    contiguous), patch-id fails. The only two things left are the line rung and
    the landing commit naming the branch tip — and the design requires BOTH.
    """
    repo = _repo(tmp_path)
    _write(repo, "ci.yml", "jobs:\n")
    base = _commit(repo, "ci: skeleton")

    _branch_from(repo, "pr", base)
    _write(repo, "ci.yml", "jobs:\n  gate:\n    steps:\n      - checkout\n      - run gate\n")
    head = _commit(repo, "ci: add the gate job")

    _git(repo, "checkout", "-q", "main")
    _write(
        repo, "ci.yml",
        "jobs:\n  gate:\n    steps:\n      - checkout\n"
        "      - configure git identity\n"          # <- inserted INSIDE the block
        "      - run gate\n",
    )
    landing = _commit(
        repo,
        "ci: gate job, with the identity it cannot run without\n\n"
        f"Carries the branch verbatim (tip {head[:8]}) plus the missing step.",
    )

    d = _detector(repo)
    v = d.classify(head, number=19, branch="pr")
    assert v.status == "landed"
    assert [f.rung for f in v.files] == ["line-contained"]
    assert v.closable is True
    assert v.landing_kind == "attestation"
    assert v.landing_shas == [landing]
    assert v.attestations[0]["kind"] == "head-sha"


def test_superset_landing_without_attestation_reports_instead_of_closing(tmp_path):
    """Same shape, but nothing on main claims the branch. The weak rung alone
    must not close a PR — that is exactly the evidence bar this tool exists to
    hold."""
    repo = _repo(tmp_path)
    _write(repo, "ci.yml", "jobs:\n")
    base = _commit(repo, "ci: skeleton")

    _branch_from(repo, "pr", base)
    _write(repo, "ci.yml", "jobs:\n  gate:\n    steps:\n      - checkout\n      - run gate\n")
    head = _commit(repo, "ci: add the gate job")

    _git(repo, "checkout", "-q", "main")
    _write(
        repo, "ci.yml",
        "jobs:\n  gate:\n    steps:\n      - checkout\n"
        "      - configure git identity\n      - run gate\n",
    )
    _commit(repo, "ci: gate job")  # says nothing about the branch

    v = _detector(repo).classify(head, number=19, branch="pr")
    assert v.status == "landed"
    assert v.closable is False
    assert "weak line rung" in v.reason


def test_partial_landing_is_never_closed(tmp_path):
    """PR #42: two of three commits landed, the third did not.

    The sharpest requirement. A boolean 'is it landed?' answers yes here and
    loses the third commit.
    """
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    _branch_from(repo, "pr", base)
    _write(repo, "configs/roster.yaml", "roster: loaded\n")
    _write(repo, "gate.sh", "read_roster\n")
    first = _commit(repo, "fix: roster")
    _write(repo, "tools/install.sh", "install --with-roster\n")
    _write(repo, "tools/README.md", "documents the roster\n")
    head = _commit(repo, "review: the operator-facing contract too")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "unrelated.txt", "main moved on\n")
    _commit(repo, "chore: unrelated")
    _git(repo, "cherry-pick", "--no-edit", first)  # ONLY the first commit lands

    v = _detector(repo).classify(head, number=42, branch="pr")
    assert v.status == "partial"
    assert v.closable is False
    assert sorted(f.path for f in v.missing) == ["tools/README.md", "tools/install.sh"]
    assert v.contained_count == 2
    assert "PARTIAL LANDING" in v.reason
    assert v.cherry_unlanded == 1


def test_partial_landing_stays_partial_even_with_an_attestation(tmp_path):
    """A commit message claiming the PR landed does NOT override file evidence.

    Attestation is a way to NAME a landing, never a way to overrule the content
    check. A train message that says 'Closes #42' while a commit is still
    missing must not close #42.
    """
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    _branch_from(repo, "pr", base)
    _write(repo, "a.txt", "one\n")
    first = _commit(repo, "part one")
    _write(repo, "b.txt", "two\n")
    head = _commit(repo, "part two")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "unrelated.txt", "x\n")
    _commit(repo, "chore: main moves on")
    _git(repo, "cherry-pick", "--no-edit", first)
    _write(repo, "trailer.txt", "x\n")
    _commit(repo, f"train: land the lane\n\nCloses #42\nCarries tip {head[:10]}")

    v = _detector(repo).classify(head, number=42, branch="pr")
    assert v.status == "partial"
    assert v.closable is False
    assert [f.path for f in v.missing] == ["b.txt"]


def test_train_branch_landing_is_not_a_main_landing(tmp_path):
    """PRs #92-#100: the work is on integration/prtrain3, which main has not
    taken. Not landed, and no amount of it existing 'somewhere' changes that."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    _branch_from(repo, "pr", base)
    _write(repo, "scripts/launchd.py", "render()\n")
    _write(repo, "tests/test_launchd.py", "def test(): pass\n")
    head = _commit(repo, "fix: importable shim")

    _branch_from(repo, "integration/train", base)
    _git(repo, "cherry-pick", "--no-edit", head)  # lands on the TRAIN, not on main
    _git(repo, "checkout", "-q", "main")

    v = _detector(repo).classify(head, number=92, branch="pr")
    assert v.status == "unlanded"
    assert v.closable is False
    assert any(f.rung == "absent-on-main" for f in v.files)


def test_unlanded_pr_is_left_alone(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _write(repo, "auth.py", "deny_machine_principal()\n")
    head = _commit(repo, "fix(auth): deny machine principal")
    _git(repo, "checkout", "-q", "main")

    v = _detector(repo).classify(head, number=15, branch="pr")
    assert v.status == "unlanded"
    assert v.closable is False
    assert v.landing_shas == []


def test_deleted_on_both_sides_counts_as_contained(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "dead.py", "legacy\n")
    base = _commit(repo, "add dead.py")

    _branch_from(repo, "pr", base)
    (repo / "dead.py").unlink()
    head = _commit(repo, "remove dead.py")

    _git(repo, "checkout", "-q", "main")
    (repo / "dead.py").unlink()
    _commit(repo, "drop the legacy module")

    v = _detector(repo).classify(head, number=7, branch="pr")
    assert [f.rung for f in v.files] == ["deleted-both"]
    assert v.status == "landed"


def test_deleted_on_branch_but_alive_on_main_is_not_contained(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "dead.py", "legacy\n")
    base = _commit(repo, "add dead.py")
    _branch_from(repo, "pr", base)
    (repo / "dead.py").unlink()
    head = _commit(repo, "remove dead.py")
    _git(repo, "checkout", "-q", "main")

    v = _detector(repo).classify(head, number=7, branch="pr")
    assert [f.rung for f in v.files] == ["divergent"]
    assert v.closable is False


# --------------------------------------------------- instrument self-defence


def test_empty_diff_is_suspicious_never_landed(tmp_path):
    """Zero matches is not agreement. This is the zsh word-splitting fault's
    signature: the comparison produced nothing, and nothing read as yes."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _git(repo, "checkout", "-q", "main")

    v = _detector(repo).classify(base, number=1, branch="pr")
    assert v.status == "empty"
    assert v.closable is False
    assert "suspicious" in v.reason


def test_classify_refuses_before_self_test(tmp_path):
    repo = _repo(tmp_path)
    d = LandDetector(repo, "main")
    with pytest.raises(InstrumentError, match="self_test"):
        d.classify(_git(repo, "rev-parse", "HEAD"))


def test_negative_canary_catches_a_detector_that_says_yes_to_everything(tmp_path):
    """The armour, tested. Force the containment primitive to agree with
    everything — the shape of the zsh bug — and self_test must refuse rather
    than grade a single PR."""
    repo = _repo(tmp_path)
    d = LandDetector(repo, "main")
    d._reverse_applies = lambda patch: True  # type: ignore[method-assign]
    with pytest.raises(InstrumentError, match="NEGATIVE CANARY FAILED"):
        d.self_test()


def test_positive_canary_catches_a_detector_that_says_no_to_everything(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "x.txt", "x\n")
    _commit(repo, "second commit so the tip has a parent")
    d = LandDetector(repo, "main")
    d._reverse_applies = lambda patch: False  # type: ignore[method-assign]
    with pytest.raises(InstrumentError, match="POSITIVE CANARY FAILED"):
        d.self_test()


def test_implausible_close_rate_refuses_a_uniform_sweep(tmp_path):
    landed = [Verdict(head="a" * 40, number=n, status="landed", closable=True) for n in range(15)]
    refusal = implausible_close_rate(landed)
    assert refusal and "REFUSING THE RUN" in refusal
    # The real shape of the audited day: 4 of 20 landed.
    mixed = landed[:4] + [
        Verdict(head="b" * 40, number=n, status="unlanded") for n in range(16)
    ]
    assert implausible_close_rate(mixed) is None
    # Too few PRs to have a meaningful shape: no opinion, rather than a wrong one.
    assert implausible_close_rate(landed[:3]) is None


def test_no_shell_word_splitting_on_paths_with_spaces(tmp_path):
    """The zsh fault reached the file list. A path with a space in it is the
    input that turns a naive split into a silent under-count — and an
    under-count reads as 'fewer files missing', which reads as landed."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _write(repo, "dir with space/file name.py", "x = 1\n")
    _write(repo, "plain.py", "y = 2\n")
    head = _commit(repo, "add both")
    _git(repo, "checkout", "-q", "main")

    v = _detector(repo).classify(head, number=3, branch="pr")
    assert sorted(f.path for f in v.files) == ["dir with space/file name.py", "plain.py"]
    assert v.status == "unlanded"


def test_line_rung_rejects_reordered_added_lines(tmp_path):
    """Ordered subsequence, not set membership: the same lines in a different
    order are a different change."""
    repo = _repo(tmp_path)
    _write(repo, "f.txt", "head\n")
    base = _commit(repo, "base")
    _branch_from(repo, "pr", base)
    _write(repo, "f.txt", "head\nalpha\nbeta\n")
    head = _commit(repo, "branch order")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "f.txt", "head\nbeta\nalpha\n")
    _commit(repo, "main order")

    v = _detector(repo).classify(head, number=4, branch="pr")
    assert [f.rung for f in v.files] == ["divergent"]
    assert v.closable is False


def test_line_rung_rejects_a_surviving_deletion(tmp_path):
    """The branch removes a line; main still has it. Half the change is absent,
    so the file is not contained however many added lines match."""
    repo = _repo(tmp_path)
    _write(repo, "f.txt", "keep\nremove-me\n")
    base = _commit(repo, "base")
    _branch_from(repo, "pr", base)
    _write(repo, "f.txt", "keep\nadded\n")
    head = _commit(repo, "swap")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "f.txt", "keep\nremove-me\nadded\nextra\n")  # added line is there…
    _commit(repo, "main keeps the removed line")

    v = _detector(repo).classify(head, number=5, branch="pr")
    assert [f.rung for f in v.files] == ["divergent"]


def test_attestation_needs_the_real_head_sha_not_a_lookalike(tmp_path):
    """A short hex token in prose must not vouch for a PR it does not name."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _write(repo, "a.txt", "one\n")
    head = _commit(repo, "work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "note.txt", "x\n")
    _commit(repo, "chore: see ad0dc62cf5966741 for context")

    d = _detector(repo)
    strong, _ = d._attestations(head, 99, "pr")
    assert strong == []


def test_bare_mention_is_recorded_but_never_acted_on(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _write(repo, "a.txt", "one\n")
    head = _commit(repo, "work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "note.txt", "x\n")
    _commit(repo, "chore: this is related to #77 somehow")

    d = _detector(repo)
    strong, mentions = d._attestations(head, 77, "pr")
    assert strong == []
    assert len(mentions) == 1


def test_closes_trailer_is_a_strong_attestation(tmp_path):
    """The second path the brief asks for: `Closes #NN` in a train message."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _write(repo, "a.txt", "one\n")
    head = _commit(repo, "work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "note.txt", "x\n")
    _commit(repo, "train: land lane 3\n\nCloses #77")

    d = _detector(repo)
    strong, _ = d._attestations(head, 77, "pr")
    assert [a["kind"] for a in strong] == ["closes-trailer"]


def test_range_spec_bounds_the_attestation_scan(tmp_path):
    """--since-ref must not let an older commit vouch for a landing."""
    repo = _repo(tmp_path)
    _write(repo, "ci.yml", "jobs:\n")
    base = _commit(repo, "ci: skeleton")
    _branch_from(repo, "pr", base)
    _write(repo, "ci.yml", "jobs:\n  gate:\n    steps:\n      - checkout\n      - run gate\n")
    head = _commit(repo, "ci: add the gate job")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "ci.yml", "jobs:\n  gate:\n    steps:\n      - checkout\n      - identity\n      - run gate\n")
    _commit(repo, f"ci: gate job (tip {head[:10]})")
    fence = _git(repo, "rev-parse", "HEAD")
    _write(repo, "later.txt", "later\n")
    _commit(repo, "chore: unrelated")

    v = _detector(repo, range_spec=f"{fence}..main").classify(head, number=19, branch="pr")
    assert v.status == "landed"
    assert v.closable is False  # the attestation is outside the scanned range
    assert "weak line rung" in v.reason
    assert v.attestations == []


def test_detector_never_touches_the_real_index_or_worktree(tmp_path):
    """It runs against live serving checkouts with dirty trees. It must leave
    both exactly as it found them."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _branch_from(repo, "pr", base)
    _write(repo, "src/a.py", "def a(): pass\n")
    head = _commit(repo, "feat")
    _git(repo, "checkout", "-q", "main")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    _git(repo, "add", "dirty.txt")

    before_status = _git(repo, "status", "--porcelain")
    before_head = _git(repo, "rev-parse", "HEAD")
    _detector(repo).classify(head, number=1, branch="pr")
    assert _git(repo, "status", "--porcelain") == before_status
    assert _git(repo, "rev-parse", "HEAD") == before_head


# ------------------------------------------------------------ pure helpers


def test_parse_name_status_handles_renames_and_spaces():
    raw = "M\0a b.py\0A\0new.py\0R100\0old path.py\0new path.py\0D\0gone.py\0"
    assert _parse_name_status(raw) == [
        ("M", "a b.py"), ("A", "new.py"), ("R", "new path.py"), ("D", "gone.py"),
    ]


def test_parse_name_status_of_nothing_is_nothing_not_a_guess():
    assert _parse_name_status("") == []
    assert _parse_name_status("\0\0") == []


def test_split_hunks_ignores_headers():
    patch = (
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
        " ctx\n-old\n+new\n"
    )
    assert _split_hunks(patch) == (["new"], ["old"])
