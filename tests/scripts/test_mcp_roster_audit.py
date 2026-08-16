"""The health-sentinel MCP roster audit must read the roster the runtime loads.

The shell gate ``scripts/gates/mech_gate.sh --check-mcp-roster`` and this audit
are two halves of one control -- ``configs/audit-checks.yaml`` names the gate as
this check's derivation, so the two disagreeing is itself the defect. Both read
``tools/mcp-servers.json`` on the premise that ``.mcp.json`` was a tracked
symlink to it; 00000000 (2026-08-02) replaced that symlink with a regular file,
and this check went on reporting ``[OK] 2 roster server(s) all approved`` about a
tree whose loaded roster held 11 servers.

Every tree here makes the two files DISAGREE, which is the only shape that
distinguishes reading the loaded roster from reading the mirror.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "health-sentinel" / "audit_checks.py"


def _load() -> Any:
    name = "mcp_roster_audit_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load()
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

APPROVED = """\
approved:
  fetch:
    justification: >-
      Only outbound-HTTP tool with recorded use in the measured week; retrieves
      and markdown-renders a URL so agents do not shell out to curl.
"""


def _ctx(tmp_path: Path, *, loaded: dict | None, mirror: dict | None, cfg: dict | None = None) -> Any:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "mcp-approved.yaml").write_text(APPROVED, encoding="utf-8")
    if loaded is not None:
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": loaded}), encoding="utf-8")
    if mirror is not None:
        (repo / "tools").mkdir()
        (repo / "tools" / "mcp-servers.json").write_text(
            json.dumps({"mcpServers": mirror}), encoding="utf-8"
        )
    registry = {"checks": {"mcp_roster": dict(cfg or {})}}
    return audit.AuditContext(repo_root=repo, accounts_root=tmp_path / "accounts", registry=registry, now=NOW)


def test_accretion_in_the_loaded_roster_is_detected_while_the_mirror_is_clean(tmp_path: Path) -> None:
    """The defect, stated as a test.

    Before the fix this returned OK: it read the 1-server mirror and never saw
    the two unapproved servers in the file the runtime loads.
    """
    ctx = _ctx(tmp_path, loaded={"fetch": {}, "tavily": {}, "playwright": {}}, mirror={"fetch": {}})
    result = audit.check_mcp_roster(ctx)
    assert result.status == audit.FAIL, result.evidence
    assert "tavily" in result.evidence and "playwright" in result.evidence, result.evidence


def test_two_rosters_that_disagree_fail_even_when_both_are_approved(tmp_path: Path) -> None:
    """The premise assertion, matching the shell gate.

    Both files hold only approved servers, so the subset check alone passes.
    They still disagree, so the reviewed roster is not necessarily the loaded
    one -- the condition that let the original defect hide for five days.
    """
    ctx = _ctx(tmp_path, loaded={"fetch": {}}, mirror={})
    result = audit.check_mcp_roster(ctx)
    assert result.status == audit.FAIL, result.evidence
    assert "disagree" in result.evidence, result.evidence


def test_agreeing_rosters_of_approved_servers_pass(tmp_path: Path) -> None:
    """The control: agreement plus approval is the only passing shape."""
    ctx = _ctx(tmp_path, loaded={"fetch": {}}, mirror={"fetch": {}})
    result = audit.check_mcp_roster(ctx)
    assert result.status == audit.OK, result.evidence


def test_a_mirror_only_tree_is_audited_rather_than_reported_missing(tmp_path: Path) -> None:
    """A tree still on the older layout has a roster; audit it, do not misreport it."""
    ctx = _ctx(tmp_path, loaded=None, mirror={"fetch": {}})
    result = audit.check_mcp_roster(ctx)
    assert result.status == audit.OK, result.evidence
    assert "roster-missing" not in result.evidence, result.evidence


def test_a_mirror_only_tree_still_fails_on_an_unapproved_server(tmp_path: Path) -> None:
    """The fallback must be a fallback, not an escape hatch."""
    ctx = _ctx(tmp_path, loaded=None, mirror={"fetch": {}, "tavily": {}})
    result = audit.check_mcp_roster(ctx)
    assert result.status == audit.FAIL, result.evidence
    assert "tavily" in result.evidence, result.evidence


def test_an_explicit_roster_key_still_wins(tmp_path: Path) -> None:
    """configs/audit-checks.yaml stays authoritative over the default.

    The acceptance suite plants defects in a synthetic tree and names the roster
    it planted them in; the default must not silently redirect that read.
    """
    ctx = _ctx(
        tmp_path,
        loaded={"fetch": {}},
        mirror={"fetch": {}, "tavily": {}},
        cfg={"roster": "tools/mcp-servers.json", "mirror": "tools/mcp-servers.json"},
    )
    result = audit.check_mcp_roster(ctx)
    assert result.status == audit.FAIL, result.evidence
    assert "tavily" in result.evidence, result.evidence
    assert Path(result.detail["roster"]).name == "mcp-servers.json", result.detail
