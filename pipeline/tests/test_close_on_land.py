"""close_on_land.py -- dry-run default, close cap, and the R16 alert wiring.

No network: `_gh` (the ``gh`` CLI wrapper) and the ``gh pr close`` subprocess
call are both monkeypatched, so nothing here ever touches a real PR. The
fixture repo reproduces the "identical landing" shape from
``test_land_detect.py`` (cherry-picked, patch-id equivalent) so
``LandDetector`` reports genuinely closable PRs without any network access
either.

Two of these tests (dry-run-is-default, cap-refuses-over-cap) hold on BASE
main already -- ``close_on_land.py``'s ``run()``/``main()`` had this behaviour
before this change; the test exists here for regression coverage, not as a
red-first proof of new behaviour. The genuinely NEW behaviour this branch
adds -- ``--loops-root`` and its one-alert-ever wiring on a COULD-NOT-RUN
exit -- is covered by ``test_instrument_fault_alerts_once_via_loops_root``,
which fails on base (the flag does not exist there) and is this file's
red-first proof.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bridge"))

import close_on_land  # noqa: E402
from land_detect import InstrumentError  # noqa: E402

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


def _commit(repo: pathlib.Path, path: str, text: str, message: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _landed_pr(repo: pathlib.Path, number: int) -> dict:
    """Branch off main, add a unique file, cherry-pick it onto main after
    main has moved on (so the cherry-pick genuinely produces a NEW sha) --
    exactly ``test_land_detect.py``'s "identical landing" shape (PRs
    #38/#60/#77): patch-id equivalent, closable.
    """
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", f"pr-{number}", base)
    head = _commit(repo, f"file_{number}.py", f"x = {number}\n", f"feat: add {number}")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "unrelated.txt", f"noise {number}\n", "chore: unrelated main commit")
    _git(repo, "cherry-pick", "--no-edit", head)
    return {"number": number, "title": f"pr {number}", "url": "-", "headRefName": f"pr-{number}", "headRefOid": head}


def _fake_gh(prs: list[dict]):
    def _inner(args: list[str]) -> object:
        assert args[:2] == ["pr", "list"], f"unexpected _gh call: {args}"
        return prs
    return _inner


_REAL_SUBPROCESS_RUN = subprocess.run


def _forbid_real_close(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Guard: fail the test loudly if anything tries to actually shell out to
    ``gh pr close``. ``land_detect.py`` and ``close_on_land.py`` share the
    same ``subprocess`` module object, so this patches ``subprocess.run``
    globally for the test and must pass real (git) calls through -- only a
    ``gh pr close`` invocation is forbidden."""
    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        if args[:3] == ["gh", "pr", "close"]:
            raise AssertionError(
                f"real 'gh pr close' subprocess invoked in a dry-run test: {args}"
            )
        return _REAL_SUBPROCESS_RUN(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


# --------------------------------------------------------------------- tests


def test_dry_run_is_default_no_close_subprocess_ever_runs(tmp_path, monkeypatch):
    """Default CLI invocation (no --apply) must never shell out to close a
    PR, even though this fixture has two genuinely closable PRs."""
    repo = _repo(tmp_path)
    prs = [_landed_pr(repo, 42), _landed_pr(repo, 89)]
    monkeypatch.setattr(close_on_land, "_gh", _fake_gh(prs))
    _forbid_real_close(monkeypatch)  # any subprocess.run call at all is a bug here

    code = close_on_land.main(["--repo", "x/y", "--git-dir", str(repo)])

    assert code == 0
    # argparse default really is False -- not something only true "by luck"
    # from --max-close never being hit.
    import inspect
    assert inspect.signature(close_on_land.run).parameters["apply"].default is False


def test_apply_requires_the_explicit_flag(tmp_path, monkeypatch):
    """`--apply` is opt-in: passing it is the ONLY way anything closes."""
    repo = _repo(tmp_path)
    pr = _landed_pr(repo, 1)
    monkeypatch.setattr(close_on_land, "_gh", _fake_gh([pr]))

    closed_calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        if args[:3] == ["gh", "pr", "close"]:
            closed_calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return _REAL_SUBPROCESS_RUN(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    code, report = close_on_land.run("x/y", str(repo), apply=True, max_close=5)
    assert code == 0
    assert report["applied"] is True
    assert closed_calls and closed_calls[0][:3] == ["gh", "pr", "close"]
    assert report["closed"] == [1]


def test_max_close_cap_refuses_the_whole_run_over_cap(tmp_path, monkeypatch):
    """cap=1 with 2 closable PRs: the run REFUSES rather than closing a
    partial batch -- zero PRs get acted on, which is still <= the cap."""
    repo = _repo(tmp_path)
    prs = [_landed_pr(repo, 42), _landed_pr(repo, 89)]
    monkeypatch.setattr(close_on_land, "_gh", _fake_gh(prs))
    _forbid_real_close(monkeypatch)

    code, report = close_on_land.run("x/y", str(repo), apply=True, max_close=1)

    assert code == 2
    assert report["refused"] and "max-close" in report["refused"]
    assert report["closed"] == []
    assert len(report["would_close"]) == 2


def test_max_close_cap_allows_apply_exactly_at_cap(tmp_path, monkeypatch):
    """cap == the number of closable PRs: all of them (never more than the
    cap) are acted on."""
    repo = _repo(tmp_path)
    prs = [_landed_pr(repo, 42), _landed_pr(repo, 89)]
    monkeypatch.setattr(close_on_land, "_gh", _fake_gh(prs))

    closed_calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        if args[:3] == ["gh", "pr", "close"]:
            closed_calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return _REAL_SUBPROCESS_RUN(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    code, report = close_on_land.run("x/y", str(repo), apply=True, max_close=2)

    assert code == 0
    assert report["refused"] is None
    assert sorted(report["closed"]) == [42, 89]
    assert len(closed_calls) == 2  # exactly the cap, never more


def test_instrument_fault_alerts_once_via_loops_root(tmp_path, monkeypatch):
    """R16's actual new behaviour: --loops-root is a real CLI flag, and an
    InstrumentError (self_test canary failure) raises exactly ONE ALERTS.md
    line via the shared alert_once -- not a silent stderr-only exit 2 that a
    15-minute unattended timer would never surface to a human.

    This is the red-first proof: on base main (before this change),
    ``--loops-root`` does not exist as an argparse flag at all, so this test
    fails there (see the scratch-copy run recorded in the build report).
    """
    repo = _repo(tmp_path)
    loops_root = tmp_path / "loopqueue"

    def _boom(*a, **kw):
        raise InstrumentError("NEGATIVE CANARY FAILED (forced for test)")

    monkeypatch.setattr(close_on_land, "run", _boom)

    code = close_on_land.main([
        "--repo", "x/y", "--git-dir", str(repo), "--loops-root", str(loops_root),
    ])

    assert code == 2
    alerts = (loops_root / "ALERTS.md").read_text(encoding="utf-8")
    assert "close-on-land" in alerts
    assert "instrument fault" in alerts
    state = json.loads((loops_root / "state" / "alerted.json").read_text(encoding="utf-8"))
    assert "close-on-land:instrument-fault" in state

    # A second COULD-NOT-RUN exit for the SAME failure kind must not append a
    # second line -- "one alert per item, ever" (CONTRACT.md doctrine cited
    # in claim.alert_once's own docstring).
    code2 = close_on_land.main([
        "--repo", "x/y", "--git-dir", str(repo), "--loops-root", str(loops_root),
    ])
    assert code2 == 2
    alerts_after = (loops_root / "ALERTS.md").read_text(encoding="utf-8")
    assert alerts_after.count("close-on-land") == alerts.count("close-on-land")

