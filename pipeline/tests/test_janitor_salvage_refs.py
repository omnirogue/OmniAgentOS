"""bridge/janitor.py -- the salvage-ref retention sweep (grok follow-up on #410).

`bridge/gate_loop.py`'s `_content_free_reconcile` mints a
`salvage/gl-contentfree-<UTCstamp>-<sha7>` branch on every content-free heal
to preserve the old tip, and nothing else in the repository ever deletes
them. These tests pin the sweep janitor.py adds for that: old + reachable
salvage branches are deleted with `git branch -d` (never `-D`, never a real
merge/force), young ones are kept regardless of reachability, and
unreachable ones are kept regardless of age. The sweep is entirely OPT-IN
(only runs when `Janitor` is constructed with a `repo`), so every existing
queue-only caller (`test_janitor_claim_reap.py` included) is unaffected.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

# `from bridge import janitor` (never a bare `import janitor`) -- same
# import-identity note as test_janitor_claim_reap.py and test_claim.py.
from bridge import janitor  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, check=False)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr or p.stdout}")
    return p.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "tester")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _stamp(age: timedelta) -> str:
    return (datetime.now(UTC) - age).strftime("%Y%m%dT%H%M%SZ")


def _mint_salvage_branch(repo: Path, *, age: timedelta, at_sha: str) -> str:
    """A branch shaped exactly like `_content_free_reconcile` mints, pointed
    at `at_sha`, with its embedded stamp `age` old (NOT the branch's real
    git-object age -- the sweep reads the NAME, never ref/commit metadata)."""
    name = f"salvage/gl-contentfree-{_stamp(age)}-{at_sha[:7]}"
    _git(repo, "branch", name, at_sha)
    return name


def _salvage_branches(repo: Path) -> set[str]:
    out = _git(repo, "branch", "--list", f"{janitor.SALVAGE_REF_PREFIX}*")
    return {ln.strip().lstrip("* ") for ln in out.splitlines() if ln.strip()}


def _queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    q.mkdir(parents=True)
    return q


# --------------------------------------------------------------- old+reachable
def test_old_reachable_salvage_branch_is_deleted(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")  # reachable from main by construction
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    branch = _mint_salvage_branch(repo, age=timedelta(days=20), at_sha=old_tip)

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert branch not in _salvage_branches(repo)
    assert any(branch in a and "delete branch" in a for a in j.actions), j.actions
    assert j.alerts == []


def test_report_mode_deletes_nothing(tmp_path: Path) -> None:
    """Without --apply, the sweep reports the same action but touches no ref --
    same reports-by-default contract as every other sweep in this file."""
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    branch = _mint_salvage_branch(repo, age=timedelta(days=20), at_sha=old_tip)

    j = janitor.Janitor(_queue(tmp_path), apply=False, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert branch in _salvage_branches(repo), "report mode must not delete"
    assert any(branch in a and "delete branch" in a for a in j.actions), j.actions


# --------------------------------------------------------------------- young
def test_young_reachable_salvage_branch_is_kept(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    branch = _mint_salvage_branch(repo, age=timedelta(days=1), at_sha=old_tip)

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert branch in _salvage_branches(repo), "under 14d must never be swept"
    assert j.actions == []


def test_salvage_branch_exactly_at_the_age_boundary_is_kept(tmp_path: Path) -> None:
    """`> 14d`, not `>= 14d` -- a branch exactly at the boundary is still young."""
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    # A hair under the boundary, not exactly on it: exactly 14d0m0s is
    # inherently racy against the sweep's own real-clock `now` (whatever
    # microseconds elapse between minting the stamp here and the sweep's
    # `_now()` call would tip it over 14d and this test would flake).
    branch = _mint_salvage_branch(
        repo, age=timedelta(days=janitor.SALVAGE_REF_MAX_AGE_DAYS, minutes=-1),
        at_sha=old_tip)

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert branch in _salvage_branches(repo)


# --------------------------------------------------------------- unreachable
def test_old_unreachable_salvage_branch_is_kept(tmp_path: Path) -> None:
    """A branch whose tip is a REAL divergence from main (not merged in) must
    survive regardless of age -- age alone is never sufficient, matching the
    same "reachable from main" gate `_content_free_reconcile` itself proves
    before it ever mints one of these branches."""
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", "diverged")
    (repo / "only-on-diverged.txt").write_text("real content\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "content main never gets")
    diverged_tip = _git(repo, "rev-parse", "diverged")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-D", "diverged")  # drop the ref; the commit survives via salvage
    branch = _mint_salvage_branch(repo, age=timedelta(days=20), at_sha=diverged_tip)

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert branch in _salvage_branches(repo), "unreachable-from-main must never be deleted"
    assert j.actions == []


# ------------------------------------------------------------- -d refusal
def test_d_refusal_keeps_the_branch_and_logs_an_alert(tmp_path: Path) -> None:
    """`git branch -d` itself can still refuse (e.g. it is the checked-out
    branch) even after the explicit reachability proof -- the sweep must
    keep the branch and log why, and never escalate to `-D`."""
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    branch = _mint_salvage_branch(repo, age=timedelta(days=20), at_sha=old_tip)
    _git(repo, "checkout", "-q", branch)  # -d refuses to delete the checked-out branch

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert branch in _salvage_branches(repo), "a `-d` refusal must keep the branch"
    assert any("branch -d" in a and "refused" in a for a in j.alerts), j.alerts
    assert not any("-D" in a for a in j.actions)


# --------------------------------------------------------------- naming/opt-in
def test_non_matching_branch_name_is_never_touched(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    tip = _git(repo, "rev-parse", "main")
    _git(repo, "branch", "salvage/gl-contentfree-not-a-stamp-ef647f0", tip)
    _git(repo, "branch", "salvage/something-else-entirely", tip)

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j._sweep_salvage_refs(janitor._now())

    assert "salvage/gl-contentfree-not-a-stamp-ef647f0" in _salvage_branches(repo)
    assert j.actions == []


def test_sweep_is_a_no_op_without_a_repo(tmp_path: Path) -> None:
    """OPT-IN: the existing queue-only constructor call (every other janitor
    test, and every caller before this change) must be entirely unaffected."""
    j = janitor.Janitor(_queue(tmp_path), apply=True)
    j._sweep_salvage_refs(janitor._now())  # must not raise with no repo at all
    assert j.actions == []
    assert j.alerts == []


def test_full_sweep_reaches_the_salvage_sweep(tmp_path: Path) -> None:
    """`sweep()` (not `--claims-only`) must actually call the salvage sweep,
    not just leave it reachable only via the private method in tests."""
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    branch = _mint_salvage_branch(repo, age=timedelta(days=20), at_sha=old_tip)
    queue = _queue(tmp_path)  # no ledger.jsonl: read_ledger treats "missing" as
    # "no events" (fine); only a zero-byte EXISTING file is torn.

    j = janitor.Janitor(queue, apply=True, repo=repo)
    j.sweep()

    assert branch not in _salvage_branches(repo)


def test_claims_only_sweep_never_touches_salvage_refs(tmp_path: Path) -> None:
    """--claims-only is a fast, narrow cadence (see module docstring) -- it
    must not also sweep salvage refs."""
    repo = _init_repo(tmp_path / "repo")
    old_tip = _git(repo, "rev-parse", "main")
    _git(repo, "commit", "--allow-empty", "-qm", "advance main")
    branch = _mint_salvage_branch(repo, age=timedelta(days=20), at_sha=old_tip)

    j = janitor.Janitor(_queue(tmp_path), apply=True, repo=repo)
    j.sweep(claims_only=True)

    assert branch in _salvage_branches(repo)
