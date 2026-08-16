"""enroll.sh --reserved-for OWNER input validation (fail closed).

The OWNER value is substituted into a ``bash -lc "... ${RESERVED_FOR_ARG}"``
ExecStart STRING and sed-substituted into the generated launchd/systemd unit
with '#' as the sed delimiter. So a shell metacharacter (``;`` ``"`` backtick
``$()`` space) is command injection into the daemon unit and a '#' collides with
the sed delimiter. enroll.sh now validates OWNER against an allowlist
(``^[A-Za-z0-9_-]+$``) EARLY — before any install, network probe, or unit
render — and aborts on anything else.

gemini finding-2 (F2.sh) turned into a passing regression: the reject cases are
the exact injection payloads (``a#b`` sed-delimiter collision, ``a;b`` / ``a"b``
command injection, ``a b`` shell-word split); the accept case is a plain
username-like token, which must PASS validation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ENROLL = Path(__file__).resolve().parents[2] / "scripts" / "workqueue" / "enroll.sh"
# The abort message the allowlist gate prints — its presence proves the run
# aborted at OUR validation, not at some unrelated later preflight step.
_REJECT_MARKER = "outside [A-Za-z0-9_-]"


def _run(owner: str) -> subprocess.CompletedProcess[str]:
    # PRIMARY points at a dead local port: any run that gets PAST validation
    # aborts quickly in preflight/registration without touching a real server.
    return subprocess.run(
        ["bash", str(_ENROLL), "--primary", "127.0.0.1:1", "--reserved-for", owner],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "owner",
    [
        "a#b",  # sed-delimiter collision
        "a;b",  # command chaining
        'a"b',  # quote break-out
        "a b",  # shell word split
        "a`b",  # backtick command substitution
        "a$(id)",  # $() command substitution
        "a|b",  # pipe
    ],
)
def test_enroll_rejects_metacharacter_owner(owner):
    proc = _run(owner)
    assert proc.returncode != 0, f"enroll.sh accepted a metacharacter owner {owner!r}"
    assert _REJECT_MARKER in proc.stderr, (
        f"expected the allowlist abort for {owner!r}; got stderr:\n{proc.stderr}"
    )


def test_enroll_accepts_username_like_owner():
    # 'owner' must PASS validation. The run still aborts later (dead --primary /
    # missing preflight deps), but NOT at the allowlist gate — proven by the
    # reserved-worker echo appearing and the reject marker being absent.
    proc = _run("owner")
    assert _REJECT_MARKER not in proc.stderr, (
        f"enroll.sh wrongly rejected the valid owner 'owner':\n{proc.stderr}"
    )
    assert "submitted_by='owner'" in proc.stderr, (
        f"expected the reserved-worker echo proving 'owner' passed validation; "
        f"got stderr:\n{proc.stderr}"
    )


def test_enroll_still_rejects_blank_owner():
    # The pre-existing blank-abort must remain (blank still fails closed).
    proc = _run("")
    assert proc.returncode != 0
    assert "empty/whitespace" in proc.stderr
