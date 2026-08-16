"""serving_deploy_watch: classification, stamping, parking, and swap safety.

Every test drives ``tick()`` with an injected fake ``run`` and a tmp repo
root, so no git, launchctl, npm, or network is ever touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.ops import serving_deploy_watch as watch


class FakeRun:
    """Scripted subprocess.run replacement keyed on argv[0..1]."""

    def __init__(self, head: str, diff: list[str] | None) -> None:
        self.head = head
        self.diff = diff
        self.kickstarted: list[str] = []
        self.built = False

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == watch.GIT and "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout=self.head + "\n", stderr="")
        if argv[0] == watch.GIT and "diff" in argv:
            if self.diff is None:
                return SimpleNamespace(returncode=128, stdout="", stderr="bad range")
            return SimpleNamespace(returncode=0, stdout="\n".join(self.diff) + "\n", stderr="")
        if argv[0] == watch.LAUNCHCTL:
            self.kickstarted.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(watch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "var" / "deploy-watch" / "state.json")
    monkeypatch.setattr(watch, "LOG_PATH", tmp_path / "var" / "log" / "watch.log")
    monkeypatch.setattr(watch, "LOCK_PATH", tmp_path / "var" / "locks" / "watch.lock")
    monkeypatch.setattr(watch, "load_ratio", lambda: 0.1)
    return tmp_path


def state() -> dict[str, Any]:
    return watch.read_state()


def seed(sha: str, **extra: Any) -> None:
    watch.write_state({"deployed_sha": sha, **extra})


# --------------------------------------------------------------------- classify

def test_classify_maps_prefixes_to_targets() -> None:
    assert watch.classify(["omniagentos/api/main.py"]) == (True, False)
    assert watch.classify(["contracts/openapi.json"]) == (True, False)
    assert watch.classify(["dashboard/src/app/testing/page.tsx"]) == (False, True)
    assert watch.classify(["docs/x.md", "README.md"]) == (False, False)
    assert watch.classify(["omniagentos/db/store.py", "dashboard/src/a.ts"]) == (True, True)


def test_classify_is_prefix_anchored_not_substring() -> None:
    # A path merely MENTIONING dashboard must not trigger a rebuild.
    assert watch.classify(["docs/dashboard/notes.md", "tests/dashboard_test.py"]) == (False, False)


# ------------------------------------------------------------------------ tick

def test_unchanged_head_is_a_quiet_noop(repo: Path) -> None:
    seed("abc123")
    run = FakeRun(head="abc123", diff=[])
    assert watch.tick(run) == 0
    assert run.kickstarted == []


def test_docs_only_advance_moves_stamp_without_deploying(repo: Path) -> None:
    seed("old0000")
    run = FakeRun(head="new1111", diff=["docs/a.md", "HANDOFF/b.md"])
    assert watch.tick(run) == 0
    assert state()["deployed_sha"] == "new1111"
    assert run.kickstarted == []


def test_api_advance_kickstarts_api_only(repo: Path) -> None:
    seed("old0000")
    run = FakeRun(head="new1111", diff=["omniagentos/testobs/readers.py"])
    assert watch.tick(run) == 0
    assert run.kickstarted == [f"gui/{__import__('os').getuid()}/{watch.API_SERVICE}"]
    assert state()["deployed_sha"] == "new1111"


def test_dashboard_advance_builds_then_swaps_then_kickstarts(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed("old0000")
    run = FakeRun(head="new1111", diff=["dashboard/src/x.ts"])
    calls: list[str] = []

    def fake_build(head: str, _run: Any) -> bool:
        calls.append(head)
        return True

    monkeypatch.setattr(watch, "build_dashboard", fake_build)
    assert watch.tick(run) == 0
    assert calls == ["new1111"]
    assert run.kickstarted[-1].endswith(watch.DASH_SERVICE)
    assert state()["deployed_sha"] == "new1111"


def test_unresolvable_range_deploys_both_targets(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # First run / GC'd stamp: over-deploy once rather than silently skip.
    seed("gone0000")
    run = FakeRun(head="new1111", diff=None)
    monkeypatch.setattr(watch, "build_dashboard", lambda head, _run: True)
    assert watch.tick(run) == 0
    assert any(watch.API_SERVICE in k for k in run.kickstarted)
    assert any(watch.DASH_SERVICE in k for k in run.kickstarted)


def test_failed_build_keeps_old_stamp_and_parks_after_two(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed("old0000")
    monkeypatch.setattr(watch, "build_dashboard", lambda head, _run: False)

    run = FakeRun(head="70c882c", diff=["dashboard/src/x.ts"])
    assert watch.tick(run) == 1
    assert state()["deployed_sha"] == "old0000"
    assert state()["fail_count"] == 1

    assert watch.tick(run) == 1
    assert state()["fail_count"] == 2

    # Third tick on the SAME sha: parked, no work, quiet success.
    run3 = FakeRun(head="70c882c", diff=["dashboard/src/x.ts"])
    assert watch.tick(run3) == 0
    assert run3.kickstarted == []

    # A NEW head clears the park.
    monkeypatch.setattr(watch, "build_dashboard", lambda head, _run: True)
    run4 = FakeRun(head="good3333", diff=["dashboard/src/x.ts"])
    assert watch.tick(run4) == 0
    assert state()["deployed_sha"] == "good3333"


def test_high_load_skips_build_but_leaves_stamp_for_retry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed("old0000")
    monkeypatch.setattr(watch, "load_ratio", lambda: 0.95)
    built: list[str] = []
    monkeypatch.setattr(watch, "build_dashboard", lambda head, _run: built.append(head) or True)
    run = FakeRun(head="new1111", diff=["dashboard/src/x.ts"])
    assert watch.tick(run) == 0
    assert built == []
    assert state().get("deployed_sha") == "old0000"  # untouched: next tick retries


# ------------------------------------------------------------------- swap logic

def test_build_dashboard_swaps_staging_into_live_only_on_success(repo: Path) -> None:
    dash = repo / "dashboard"
    (dash / watch.LIVE_DIST).mkdir(parents=True)
    (dash / watch.LIVE_DIST / "BUILD_ID").write_text("old", encoding="utf-8")

    def ok_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        staging = Path(kwargs["cwd"]) / kwargs["env"]["OMNIAGENTOS_NEXT_DIST_DIR"]
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "BUILD_ID").write_text("new", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert watch.build_dashboard("sha11111111", ok_run) is True
    assert (dash / watch.LIVE_DIST / "BUILD_ID").read_text(encoding="utf-8") == "new"
    assert not (dash / watch.STAGING_DIST).exists()


def test_build_dashboard_failure_leaves_live_untouched(repo: Path) -> None:
    dash = repo / "dashboard"
    (dash / watch.LIVE_DIST).mkdir(parents=True)
    (dash / watch.LIVE_DIST / "BUILD_ID").write_text("old", encoding="utf-8")

    def bad_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="boom", stderr="")

    assert watch.build_dashboard("sha11111111", bad_run) is False
    assert (dash / watch.LIVE_DIST / "BUILD_ID").read_text(encoding="utf-8") == "old"


# ----------------------------------------------------------------------- lock

def test_lock_is_exclusive_and_stale_takeover_works(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert watch.acquire_lock() is True
    assert watch.acquire_lock() is False  # second holder yields
    # Age the lock past the stale window and it is taken over.
    import os as _os
    old = watch.LOCK_PATH.stat().st_mtime - watch.LOCK_STALE_S - 60
    _os.utime(watch.LOCK_PATH, (old, old))
    assert watch.acquire_lock() is True
    watch.release_lock()
