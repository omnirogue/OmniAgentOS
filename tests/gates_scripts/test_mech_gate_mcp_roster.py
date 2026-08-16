"""The MCP roster check must read the roster the runtime actually loads.

Regression for the real defect: the check read ``tools/mcp-servers.json`` on the
premise that ``.mcp.json`` was a tracked symlink to it. Commit 00000000
(2026-08-02) replaced that symlink with a regular file for an unrelated reason,
and from then on the gate passed on a 2-server file while ``.mcp.json`` carried
11 servers -- the exact roster the control exists to prevent.

Every test here builds a tree where the two files DISAGREE, which is the only
shape that distinguishes reading the loaded file from reading the mirror.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

GATE = Path(__file__).resolve().parents[2] / "scripts" / "gates" / "mech_gate.sh"

APPROVED = """\
approved:
  fetch:
    justification: >-
      Only outbound-HTTP tool with recorded use in the measured week; retrieves
      and markdown-renders a URL so agents do not shell out to curl.
"""


def _tree(tmp_path: Path, *, loaded: dict | None, mirror: dict | None) -> Path:
    gates = tmp_path / "scripts" / "gates"
    gates.mkdir(parents=True)
    shutil.copy(GATE, gates / "mech_gate.sh")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "mcp-approved.yaml").write_text(APPROVED, encoding="utf-8")
    if loaded is not None:
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": loaded}), encoding="utf-8")
    if mirror is not None:
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "mcp-servers.json").write_text(
            json.dumps({"mcpServers": mirror}), encoding="utf-8"
        )
    return gates / "mech_gate.sh"


def _run(script: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--check-mcp-roster", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_unapproved_server_in_the_loaded_roster_is_refused(tmp_path: Path) -> None:
    """The defect, stated as a test: mirror is clean, loaded file has accreted.

    Before the fix the check defaulted to the mirror and reported success here.
    """
    script = _tree(
        tmp_path,
        loaded={"fetch": {}, "tavily": {}, "playwright": {}},
        mirror={"fetch": {}},
    )
    result = _run(script, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "tavily" in combined and "playwright" in combined, combined


def test_two_rosters_that_disagree_are_refused_even_when_both_are_approved(
    tmp_path: Path,
) -> None:
    """The premise assertion.

    Both files contain only approved servers, so the subset check alone passes.
    They still disagree, which means the reviewed roster is not necessarily the
    loaded one -- the condition that let the original defect hide.
    """
    script = _tree(tmp_path, loaded={"fetch": {}}, mirror={})
    result = _run(script, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "disagree" in (result.stdout + result.stderr)


def test_agreeing_rosters_of_approved_servers_pass(tmp_path: Path) -> None:
    """The control: agreement plus approval is the only passing shape."""
    script = _tree(tmp_path, loaded={"fetch": {}}, mirror={"fetch": {}})
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_loaded_roster_alone_is_checked_when_no_mirror_exists(tmp_path: Path) -> None:
    """A tree that has moved fully off the mirror is still checked, not skipped."""
    script = _tree(tmp_path, loaded={"fetch": {}, "tavily": {}}, mirror=None)
    result = _run(script, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "tavily" in (result.stdout + result.stderr)


def test_a_mirror_only_tree_is_audited_rather_than_reported_missing(tmp_path: Path) -> None:
    """A tree still on the older layout has a roster; check it, do not misdiagnose it.

    Reading .mcp.json unconditionally turned a passing mirror-only tree into
    ``roster not found: .mcp.json`` -- the wrong diagnosis for a tree that does
    have a roster, and a regression against origin/main, which answered
    ``1 server(s) approved`` here.
    """
    script = _tree(tmp_path, loaded=None, mirror={"fetch": {}})
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not found" not in (result.stdout + result.stderr)


def test_a_mirror_only_tree_still_refuses_an_unapproved_server(tmp_path: Path) -> None:
    """The fallback must be a fallback, not an escape hatch.

    Guards the shape that would make the previous test pass vacuously: a tree
    with no .mcp.json must still be GATED, not skipped.
    """
    script = _tree(tmp_path, loaded=None, mirror={"fetch": {}, "tavily": {}})
    result = _run(script, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "tavily" in (result.stdout + result.stderr)


def test_an_explicitly_named_absent_roster_is_an_error_not_a_fallback(tmp_path: Path) -> None:
    """The standalone contract: naming a roster that is not there is an error.

    The mirror-only fallback applies to DEFAULT resolution only. A probe that
    names its own roster must never be silently answered about a different file.
    """
    script = _tree(tmp_path, loaded=None, mirror={"fetch": {}})
    result = _run(script, tmp_path, str(tmp_path / "nope.json"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "not found" in (result.stdout + result.stderr)


def test_an_explicit_mirror_argument_scopes_the_premise_assertion(tmp_path: Path) -> None:
    """Standalone mode must let a probe say which mirror it is asserting about.

    Without this, a probe roster written to a scratch directory always diverges
    from the repo's tools/mcp-servers.json, so the premise assertion refuses
    first and the probe exits non-zero while proving nothing about the approval
    logic it exists to exercise -- the shape that quietly made two acceptance
    probes tautologies.
    """
    script = _tree(tmp_path, loaded={"fetch": {}}, mirror={"fetch": {}, "tavily": {}})
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"mcpServers": {"fetch": {}, "s12-probe": {}}}), encoding="utf-8")

    scoped = _run(script, tmp_path, str(probe), str(probe))
    assert scoped.returncode != 0, scoped.stdout + scoped.stderr
    combined = scoped.stdout + scoped.stderr
    assert "s12-probe: absent from" in combined, combined
    assert "disagree" not in combined, combined

    # Same probe, mirror left implicit: refuses for the premise instead, which is
    # a correct refusal but not the one the probe means to demonstrate.
    unscoped = _run(script, tmp_path, str(probe))
    assert unscoped.returncode != 0
    assert "disagree" in (unscoped.stdout + unscoped.stderr)
