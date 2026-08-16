"""Gates G2/G3/G5/G8 can actually deny (HANDOFF Phase 1.3)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.store import SqliteStore
from omniagentos.gates.service import GateService
from omniagentos.grants import GrantsStore


def test_g2_denies_on_failed_capacity() -> None:
    g = GateService()
    d = g.g2_dispatch({"capacity_ok": False})
    assert d.decision == "deny"
    assert d.next_state == "dispatch_blocked"


def test_g3_denies_tool_outside_allowlist() -> None:
    g = GateService()
    d = g.g3_tool({"tool": "shell", "tools_allowed": ["file_read", "file_write"]})
    assert d.decision == "deny"


def test_g5_denies_failed_verify() -> None:
    g = GateService()
    d = g.g5_local_verify({"verify_ok": False, "tests": "2 failed"})
    assert d.decision == "deny"
    assert d.evidence.get("reason") == "local_verify_failed"


def test_g5_denies_self_attestation_despite_caller_independent_pass() -> None:
    """M-38: a caller-supplied independent_pass flag must not override the refusal."""
    g = GateService()
    for flag in ("self_attested", "self_attested_pass", "caller_self_attested"):
        d = g.g5_local_verify({flag: True, "independent_pass": True})
        assert d.decision == "deny"
        assert d.next_state == "verify_blocked"
        assert d.evidence.get("reason") == "self_attested_verification_rejected"
        assert d.evidence.get("verify_ok") is False
        assert d.evidence.get("mechanical_pass") is False
        # The spoofed verdict flag is stripped from the decision envelope.
        assert "independent_pass" not in d.evidence
        assert d.evidence.get("ignored_caller_verdict_flags") == ["independent_pass"]


def test_g5_denies_self_attestation_despite_similarly_named_flags() -> None:
    """M-38: similarly named caller verdict flags are ignored the same way."""
    g = GateService()
    d = g.g5_local_verify(
        {
            "self_attested": True,
            "independently_verified": True,
            "verify_override": True,
        }
    )
    assert d.decision == "deny"
    assert d.evidence.get("reason") == "self_attested_verification_rejected"
    assert "independently_verified" not in d.evidence
    assert "verify_override" not in d.evidence
    assert d.evidence.get("ignored_caller_verdict_flags") == [
        "independently_verified",
        "verify_override",
    ]


def test_g5_allows_legitimate_independent_mechanical_evidence() -> None:
    """Mechanical verify evidence from a real verify run still passes the gate."""
    g = GateService()
    d = g.g5_local_verify(
        {"verify_ok": True, "mechanical_pass": True, "verify_output": "12 passed"}
    )
    assert d.decision == "allow"
    assert d.next_state == "locally_verified"

    # A stray caller verdict flag is stripped but never changes the verdict.
    d2 = g.g5_local_verify({"verify_ok": True, "mechanical_pass": True, "independent_pass": True})
    assert d2.decision == "allow"
    assert d2.next_state == "locally_verified"
    assert "independent_pass" not in d2.evidence
    assert d2.evidence.get("ignored_caller_verdict_flags") == ["independent_pass"]


def test_g8_denies_missing_grant_and_dead_grant(tmp_path: Path) -> None:
    g = GateService()
    missing = g.g8_release({"kind": "send", "consequential": True})
    assert missing.decision == "deny"

    store = GrantsStore(SqliteStore(str(tmp_path / "g.db")))
    # No live grant for this id
    dead = g.g8_release(
        {
            "kind": "send",
            "consequential": True,
            "grant_id": "gnt_does_not_exist",
            "grant_store": store,
            "capability": "gmail.send",
        }
    )
    assert dead.decision == "deny"
    assert dead.evidence.get("reason") in {"grant_not_found", "grant_not_live", "not_approved"}
