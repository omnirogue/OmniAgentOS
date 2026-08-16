"""Pins advice_writer.py's `base_red` line (proposal
sha256:4f9469143d65a80a1c4d7f08aa0c3df1b8ca06d0dd2a43e634da63d2c75e00cd, LANE
B): `run_once`, when given `--github-repo`, calls the read-only
`base_red_report.detect_base_red` and attaches its result to the advice
record under `base_red` -- following the SAME fail-closed discipline as
`adapter_error` (test_advice_selfwrite.py): a detector failure (even an
unhandled exception, not just its own returned `error` key) is recorded IN
the advice, never dropped, never rendered as null.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import advice_writer as A  # noqa: E402
from bridge import base_red_report as B  # noqa: E402

assert Path(A.__file__).resolve() == (PKG / "bridge" / "advice_writer.py").resolve(), \
    f"imported the WRONG advice_writer.py: {A.__file__}"

PY = sys.executable


def _dummy_ok_subprocess(monkeypatch):
    """Stand in for `integration.py --once --dry-run` so these tests exercise
    only the base_red attachment, not the real adapter subprocess."""

    class DummyProc:
        returncode = 0
        stdout = '{"summary": true}'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: DummyProc())


def test_no_github_repo_means_no_base_red_key(monkeypatch, tmp_path):
    """Advisory-only, opt-in: without --github-repo, run_once must not call
    the detector at all, and the advice record must not carry the key."""
    _dummy_ok_subprocess(monkeypatch)
    monkeypatch.setattr(
        B, "detect_base_red",
        lambda repo: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    advice = A.run_once(tmp_path, None, PY, github_repo=None)

    assert "base_red" not in advice


def test_detector_success_is_attached_verbatim(monkeypatch, tmp_path):
    _dummy_ok_subprocess(monkeypatch)
    stub_result = {"generated_at": "2026-08-13T00:00:00Z",
                   "false_red_prs": [{"pr": 336, "check": "type"}], "error": None}
    monkeypatch.setattr(A._base_red_report, "detect_base_red", lambda repo: stub_result)

    advice = A.run_once(tmp_path, None, PY, github_repo="example-org/OmniAgentOS")

    assert advice["base_red"] == stub_result


def test_detector_failure_is_recorded_not_dropped(monkeypatch, tmp_path):
    """RED before the fix: an advice_writer that either doesn't call the
    detector at all, or lets a raised exception propagate and abort the
    whole advice write, or catches it and writes `base_red: None`. GREEN
    after: a crashing detector still produces a full advice record whose
    `base_red` key carries a non-empty `error` and `false_red_prs: None` --
    mirrors the adapter_error contract asserted by test_advice_selfwrite.py.
    """
    _dummy_ok_subprocess(monkeypatch)

    def _crash(repo: str) -> dict:
        raise RuntimeError("gh: connection reset")

    monkeypatch.setattr(A._base_red_report, "detect_base_red", _crash)

    advice = A.run_once(tmp_path, None, PY, github_repo="example-org/OmniAgentOS")

    assert "base_red" in advice, "detector crash dropped the whole base_red key"
    base_red = advice["base_red"]
    assert base_red is not None, "base_red written as null on detector crash"
    assert base_red.get("error"), "detector crash recorded no error message"
    assert base_red.get("false_red_prs") is None, (
        "detector crash rendered false_red_prs as "
        f"{base_red.get('false_red_prs')!r} instead of None"
    )
    # the summary/rc from the (successful, dummied) adapter subprocess must
    # be unaffected by the detector crashing -- one failure must not corrupt
    # an unrelated part of the same advice record.
    assert advice["rc"] == 0
    assert advice["summary"] == {"summary": True}


def test_detector_own_error_shape_is_passed_through(monkeypatch, tmp_path):
    """The detector's OWN handled error (GhApiError -> its documented
    false_red_prs=None/error=str shape) must also survive unmodified --
    this is the non-crash sibling of the test above."""
    _dummy_ok_subprocess(monkeypatch)

    def _handled_error(repo: str) -> dict:
        return {"generated_at": "2026-08-13T00:00:00Z", "false_red_prs": None,
                "error": "gh: rate limit exceeded (403)"}

    monkeypatch.setattr(A._base_red_report, "detect_base_red", _handled_error)

    advice = A.run_once(tmp_path, None, PY, github_repo="example-org/OmniAgentOS")

    assert advice["base_red"]["false_red_prs"] is None
    assert advice["base_red"]["error"]
