"""Pins pipeline/bridge/base_red_report.py -- the READ-ONLY base-red
false-red detector (proposal
sha256:4f9469143d65a80a1c4d7f08aa0c3df1b8ca06d0dd2a43e634da63d2c75e00cd).

Three behaviours this module exists to guarantee, each with its own test:

1. `test_api_error_is_not_an_empty_report` -- the falsifier test. A `gh` API
   error must NEVER render as an empty `false_red_prs` list; that reads as
   "checked, found nothing" to a consumer that only tests truthiness
   (favourable absence, the class this estate keeps re-discovering --
   advice_writer.py:186-200, github_bridge.py's own docstring). It must
   carry an explicit non-empty `error` string and `false_red_prs: None`.

2. `test_test_job_is_never_reported` -- `test` runs with continue-on-error:
   true (.github/workflows/ci.yml ~line 159) and its 'fail' rendering is by
   design; a PR failing ONLY that job must never appear in the report.

3. `test_current_base_pr_is_not_reported` -- only a PR whose FAILING RUN's
   recorded base predates the fix commit is a false red; a PR whose base
   already contains the fix is a real signal and must not be reported.
"""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import base_red_report as B  # noqa: E402

assert Path(B.__file__).resolve() == (PKG / "bridge" / "base_red_report.py").resolve(), \
    f"imported the WRONG base_red_report.py: {B.__file__}"

REPO = "example-org/OmniAgentOS"


def test_api_error_is_not_an_empty_report(monkeypatch):
    """RED before the fix: a detector that swallows the gh error and returns
    an empty PR list. GREEN after: `false_red_prs` is explicitly None and
    `error` names the failure -- no consumer can read this as "clean"."""

    def _boom(repo: str) -> dict:
        raise B.GhApiError("gh: rate limit exceeded (403)")

    monkeypatch.setattr(B, "_required_check_fix_shas", _boom)

    result = B.detect_base_red(REPO)

    assert result.get("error"), "an API error produced no `error` field at all"
    assert isinstance(result["error"], str) and result["error"].strip()
    assert result.get("false_red_prs") is None, (
        "an API error rendered as false_red_prs="
        f"{result.get('false_red_prs')!r} -- must be None, never [] "
        "(an empty list reads as 'checked, nothing wrong')"
    )


def test_api_error_from_open_pr_failures_is_also_not_an_empty_report(monkeypatch):
    """Same contract, other call site: the fix-sha lookup can succeed while
    the PR-failures lookup errors. Both paths must fail closed identically."""
    monkeypatch.setattr(B, "_required_check_fix_shas", lambda repo: {"lint": "a" * 40})

    def _boom(repo: str) -> list[dict]:
        raise B.GhApiError("gh: 502 Bad Gateway")

    monkeypatch.setattr(B, "_open_pr_failures", _boom)

    result = B.detect_base_red(REPO)

    assert result.get("error")
    assert result.get("false_red_prs") is None


def test_test_job_is_never_reported(monkeypatch):
    """Fixture: one open PR failing ONLY `test` (continue-on-error by
    design). Must be absent from the report -- and this must not be an
    error state either."""
    monkeypatch.setattr(B, "_required_check_fix_shas", lambda repo: {"lint": "a" * 40})
    monkeypatch.setattr(
        B, "_open_pr_failures",
        lambda repo: [{"pr": 42, "check": "test", "base_sha": "b" * 40}],
    )

    result = B.detect_base_red(REPO)

    assert result["error"] is None
    assert result["false_red_prs"] == [], (
        "a PR failing only the non-required `test` job was reported as "
        f"false-red: {result['false_red_prs']!r}"
    )


def test_current_base_pr_is_not_reported(monkeypatch):
    """Two PRs both failing `type`: PR #1's failing run recorded a base that
    predates the fix (real false red, must be reported); PR #2's failing run
    recorded a base that already contains the fix (a real failure, must NOT
    be reported as false-red)."""
    fix_sha = "f" * 40
    stale_base = "1" * 40
    fixed_base = "2" * 40

    monkeypatch.setattr(B, "_required_check_fix_shas", lambda repo: {"type": fix_sha})
    monkeypatch.setattr(
        B, "_open_pr_failures",
        lambda repo: [
            {"pr": 336, "check": "type", "base_sha": stale_base},
            {"pr": 999, "check": "type", "base_sha": fixed_base},
        ],
    )

    def _contains(repo: str, base_sha: str, fs: str) -> bool:
        assert fs == fix_sha
        return base_sha == fixed_base  # only PR #999's base contains the fix

    monkeypatch.setattr(B, "_base_contains_fix", _contains)

    result = B.detect_base_red(REPO)

    assert result["error"] is None
    reported_prs = {entry["pr"] for entry in result["false_red_prs"]}
    assert reported_prs == {336}, (
        f"expected only PR #336 (stale base) reported, got {reported_prs!r}"
    )
    entry = result["false_red_prs"][0]
    assert entry["check"] == "type"
    assert entry["base_sha"] == stale_base
    assert entry["fix_sha"] == fix_sha


def test_no_known_fix_is_skipped_not_reported(monkeypatch):
    """A PR failing a REQUIRED check for which no failure->success
    transition was observed has nothing to compare its base against --
    it must be silently skipped, not manufactured into a false-red entry."""
    monkeypatch.setattr(B, "_required_check_fix_shas", lambda repo: {})
    monkeypatch.setattr(
        B, "_open_pr_failures",
        lambda repo: [{"pr": 7, "check": "dashboard", "base_sha": "c" * 40}],
    )

    result = B.detect_base_red(REPO)

    assert result["error"] is None
    assert result["false_red_prs"] == []


def test_required_and_excluded_do_not_overlap():
    """A cheap standing invariant: `test` (continue-on-error, by design) can
    never silently sneak into REQUIRED."""
    assert B.REQUIRED.isdisjoint(B.EXCLUDED_CHECKS)
    assert "test" in B.EXCLUDED_CHECKS
    assert {"lint", "type", "dashboard"} <= B.REQUIRED
