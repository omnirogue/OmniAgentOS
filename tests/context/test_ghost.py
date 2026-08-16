"""Ghost context hermetic packet tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos.context import ghost as ghost_mod
from omniagentos.context.ghost import (
    assert_no_ambient_leak,
    build_ghost_packet,
)


def test_fingerprint_stable() -> None:
    a = build_ghost_packet([("role", "executor"), ("task", "do X")])
    b = build_ghost_packet([("role", "executor"), ("task", "do X")])
    assert a.fingerprint == b.fingerprint
    assert "executor" in a.text()


def test_fingerprint_changes_with_content() -> None:
    a = build_ghost_packet([("task", "A")])
    b = build_ghost_packet([("task", "B")])
    assert a.fingerprint != b.fingerprint


def test_ambient_leak_detection(tmp_path: Path) -> None:
    planted = "UNIQUE_PLANTED_AMBIENT_INSTRUCTION_BODY_FOR_LEAK_TEST_XYZ"
    (tmp_path / "CLAUDE.md").write_text(planted + "\n" + ("x" * 40), encoding="utf-8")
    clean = build_ghost_packet([("task", "only approved")])
    assert assert_no_ambient_leak(clean.text(), repo_root=tmp_path) == []
    dirty = build_ghost_packet([("task", "only approved"), ("leak", planted + "\n" + ("x" * 40))])
    assert "CLAUDE.md" in assert_no_ambient_leak(dirty.text(), repo_root=tmp_path)


def test_unreadable_ambient_is_not_reported_as_clean(tmp_path: Path) -> None:
    """Unreadable ambient source must not look like a verified-clean empty leak list.

    Defect class: missing/unreadable source reported identically to a genuinely
    clean measurement; health check reports healthy when it could not measure.

    Named counterfeit: ``except OSError: continue`` (or broader swallow) so an
    unreadable CLAUDE.md yields ``[]`` — the same favourable result as a
    successful clean scan. This test must fail under that counterfeit.
    """
    planted = "UNIQUE_UNREADABLE_AMBIENT_BODY_FOR_FAIL_CLOSED_CHECK" + ("y" * 40)
    ambient = tmp_path / "CLAUDE.md"
    ambient.write_text(planted, encoding="utf-8")
    os.chmod(ambient, 0)
    packet = build_ghost_packet([("task", "only approved")])
    try:
        # Dedicated fail-closed error is required so callers cannot treat
        # "could not measure" as the clean success path (empty list).
        err_type = getattr(ghost_mod, "AmbientLeakUnreadable", None)
        assert err_type is not None, (
            "AmbientLeakUnreadable must exist so unreadable ambient cannot "
            "collapse to the same [] success as a verified-clean scan"
        )
        with pytest.raises(err_type) as caught:
            assert_no_ambient_leak(packet.text(), repo_root=tmp_path)
        assert "CLAUDE.md" in str(caught.value)
    finally:
        os.chmod(ambient, 0o644)


def test_undecodable_ambient_is_not_reported_as_clean(tmp_path: Path) -> None:
    """Non-UTF-8 ambient body is unreadable for leak measurement — not clean."""
    ambient = tmp_path / "AGENTS.md"
    ambient.write_bytes(b"\xff\xfe" + b"not-utf8-ambient-body-" + b"z" * 40)
    packet = build_ghost_packet([("task", "only approved")])
    err_type = getattr(ghost_mod, "AmbientLeakUnreadable", None)
    assert err_type is not None, "AmbientLeakUnreadable must exist"
    with pytest.raises(err_type) as caught:
        assert_no_ambient_leak(packet.text(), repo_root=tmp_path)
    assert "AGENTS.md" in str(caught.value)


def test_short_ambient_full_body_leak_is_not_reported_as_clean(tmp_path: Path) -> None:
    """Complete contents of a short readable ambient file in the packet is a leak.

    Defect class: non-result presented as favourable clean. When an ambient
    file is shorter than 40 characters and its full body appears in the
    packet, returning ``[]`` renders a real leak as a verified-clean scan.

    Named counterfeit: ``if len(sample) >= 40 and sample[:200] in packet_text``
    so short ambient files are never reported. This test must fail under that
    counterfeit. Positive control: short ambient body absent from packet is
    still clean.
    """
    # Distinct body under 40 chars (requirement boundary the defect gated on).
    planted = "SHORT_AMBIENT_LEAK_XYZ"
    assert len(planted) < 40
    (tmp_path / "CLAUDE.md").write_text(planted + "\n", encoding="utf-8")

    dirty = build_ghost_packet([("task", "approved"), ("leak", planted)])
    leaks = assert_no_ambient_leak(dirty.text(), repo_root=tmp_path)
    assert "CLAUDE.md" in leaks, (
        "full contents of short ambient file present in packet must count as a leak"
    )

    clean = build_ghost_packet([("task", "approved only, no ambient body")])
    assert assert_no_ambient_leak(clean.text(), repo_root=tmp_path) == []
