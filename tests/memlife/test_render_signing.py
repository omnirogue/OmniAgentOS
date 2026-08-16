"""Integrity tests for rendered memlife lessons.

These checks keep the signing key confined to pytest's temporary filesystem;
they deliberately never print, log, or assert its material.
"""

from __future__ import annotations

import logging
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniagentos.memlife.contracts import Lesson, LessonStatus, Provenance
from omniagentos.memlife.render import (
    LESSONS_BEGIN,
    LESSONS_END,
    _sign_block,
    extract_rendered_claims,
    render_lessons,
)


@pytest.fixture()
def memories_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "memories"
    root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_MEMORIES_DIR", str(root))
    return root


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "memlife.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memlife_lessons (id TEXT PRIMARY KEY, status TEXT)")
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("OMNIAGENTOS_DB", str(path))
    return path


def _set_status(db_path: Path, lesson_id: str, status: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO memlife_lessons (id, status) VALUES (?, ?)",
            (lesson_id, status),
        )
        connection.commit()
    finally:
        connection.close()


def _lesson(lesson_id: str, claim: str) -> Lesson:
    return Lesson(
        id=lesson_id,
        candidate_id="cand_1",
        claim=claim,
        status=LessonStatus.ACCEPTED,
        graduated_at=datetime(2026, 8, 3, tzinfo=UTC),
        graduated_by="test",
        provenance=Provenance(source="test"),
    )


def _write_signed_block(path: Path, unsigned: str, memories_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{LESSONS_BEGIN}\n{_sign_block(unsigned, memories_root=memories_root)}\n"
        f"{LESSONS_END}\n",
        encoding="utf-8",
    )


def test_db_lesson_renders_and_verifies(
    memories_root: Path,
    db_path: Path,
) -> None:
    claim = "Verified lesson claim"
    _set_status(db_path, "les_verified", "accepted")

    path = render_lessons(
        "project",
        [_lesson("les_verified", claim)],
        memories_root=memories_root,
    )
    rendered = path.read_text(encoding="utf-8")

    assert "<!-- hmac=" in rendered
    assert extract_rendered_claims(path, memories_root=memories_root) == [claim]

    key_path = memories_root.parent / "memlife.key"
    before = key_path.read_bytes()
    render_lessons("project", [_lesson("les_verified", claim)], memories_root=memories_root)
    assert key_path.read_bytes() == before
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_orphan_lesson_rejected_loudly(
    memories_root: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del db_path  # The table exists but deliberately has no row for this ID.
    path = memories_root / "project" / "LESSONS.md"
    _write_signed_block(
        path,
        "- Orphan claim  <!-- id=mlsn_orphan status=accepted -->",
        memories_root,
    )

    caplog.set_level(logging.WARNING, logger="omniagentos.memlife.render")
    assert extract_rendered_claims(path, memories_root=memories_root) == []
    assert any("no DB row" in record.message for record in caplog.records)


def test_tampered_block_rejected(
    memories_root: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    claim = "Verified lesson claim"
    _set_status(db_path, "les_tampered", "accepted")
    path = render_lessons(
        "project",
        [_lesson("les_tampered", claim)],
        memories_root=memories_root,
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace("Verified", "Tampered", 1),
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger="omniagentos.memlife.render")
    assert extract_rendered_claims(path, memories_root=memories_root) == []
    assert any("HMAC verification failed" in record.message for record in caplog.records)


def test_forged_dual_sentinel_rejected(
    memories_root: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_status(db_path, "les_real", "accepted")
    path = memories_root / "project" / "LESSONS.md"
    _write_signed_block(
        path,
        "- Forged claim  <!-- id=les_forged status=accepted -->",
        memories_root,
    )
    real = _sign_block(
        "- Real claim  <!-- id=les_real status=accepted -->",
        memories_root=memories_root,
    )
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"{LESSONS_BEGIN}\n{real}\n{LESSONS_END}\n",
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger="omniagentos.memlife.render")
    assert extract_rendered_claims(path, memories_root=memories_root) == []
    assert any("duplicate or forged sentinels" in record.message for record in caplog.records)


def test_db_status_mismatch_rejected_loudly(
    memories_root: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_status(db_path, "les_retracted", "retracted")
    path = memories_root / "project" / "LESSONS.md"
    _write_signed_block(
        path,
        "- Counterfeit claim  <!-- id=les_retracted status=accepted -->",
        memories_root,
    )

    caplog.set_level(logging.WARNING, logger="omniagentos.memlife.render")
    assert extract_rendered_claims(path, memories_root=memories_root) == []
    assert any("DB says 'retracted'" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# K11 — the fail-closed paths U-M2 claims but never exercised
# --------------------------------------------------------------------------


def test_the_mac_is_verified_against_an_independently_computed_hmac(
    memories_root: Path,
    db_path: Path,
) -> None:
    """Every signing fixture above is produced by production ``_sign_block``.

    A sign+verify pair that is WRONG IN THE SAME WAY — a MAC over the wrong
    bytes, a truncated key, a digest swap — round-trips perfectly and every one
    of those tests stays green. So this one computes the expected MAC here, from
    the key file and the block bytes, with no help from the module under test.
    """
    import hashlib
    import hmac as hmac_mod
    import re as re_mod

    claim = "Independently verified claim"
    _set_status(db_path, "les_independent", "accepted")
    path = render_lessons(
        "project",
        [_lesson("les_independent", claim)],
        memories_root=memories_root,
    )
    rendered = path.read_text(encoding="utf-8")

    body = rendered.split(LESSONS_BEGIN, 1)[1].split(LESSONS_END, 1)[0]
    signed = body[1:-1]  # the layout is exactly one newline either side
    match = re_mod.fullmatch(
        r"(?P<body>.*)\n<!-- hmac=(?P<mac>[0-9a-f]{64}) -->", signed, re_mod.DOTALL
    )
    assert match is not None, "the rendered block is not in the signed layout"

    key = (memories_root.parent / "memlife.key").read_bytes()
    assert len(key) == 32
    expected = hmac_mod.new(key, match.group("body").encode("utf-8"), hashlib.sha256).hexdigest()

    assert match.group("mac") == expected, (
        "the persisted MAC is not HMAC-SHA256(key, block); a sign/verify pair "
        "that agrees with itself but not with the algorithm proves nothing"
    )
    # ...and the claim really is inside the bytes the MAC covers.
    assert claim in match.group("body")


def test_a_missing_key_file_refuses_to_read_rather_than_minting_one(
    memories_root: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recall must never create key material: an absent key means REFUSE.

    Minting a key at read time would let anyone who can delete a file replace
    the signing authority and have their own forged block accepted.
    """
    claim = "Claim whose key disappears"
    _set_status(db_path, "les_keyless", "accepted")
    path = render_lessons(
        "project",
        [_lesson("les_keyless", claim)],
        memories_root=memories_root,
    )
    assert extract_rendered_claims(path, memories_root=memories_root) == [claim]

    key_path = memories_root.parent / "memlife.key"
    key_path.unlink()

    caplog.set_level(logging.WARNING, logger="omniagentos.memlife.render")
    assert extract_rendered_claims(path, memories_root=memories_root) == []
    assert any("HMAC key unavailable" in record.message for record in caplog.records)
    assert not key_path.exists(), "reading must not mint a signing key"


def test_a_truncated_key_file_is_not_accepted_as_a_key(
    memories_root: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A short key is a broken key, not a weaker one."""
    claim = "Claim whose key is truncated"
    _set_status(db_path, "les_shortkey", "accepted")
    path = render_lessons(
        "project",
        [_lesson("les_shortkey", claim)],
        memories_root=memories_root,
    )
    key_path = memories_root.parent / "memlife.key"
    key_path.write_bytes(key_path.read_bytes()[:16])

    caplog.set_level(logging.WARNING, logger="omniagentos.memlife.render")
    assert extract_rendered_claims(path, memories_root=memories_root) == []
    assert any("HMAC key unavailable" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    ("label", "mutate", "diagnostic"),
    [
        # A sentinel with anything else on its line is not the sentinel. Which
        # of the two fail-closed branches catches it depends on WHERE the extra
        # text lands, so the expected diagnostic is named per case: asserting a
        # substring of the log MESSAGE is not safe here, because the message
        # interpolates the file path and pytest puts the test name (containing
        # "prefixed") in that path -- an assertion that passes for the wrong
        # reason is the thing this review is about.
        ("prefix_before_begin", lambda t: t.replace(LESSONS_BEGIN, f"x {LESSONS_BEGIN}", 1),
         "prefixed_sentinel"),
        ("suffix_after_begin", lambda t: t.replace(LESSONS_BEGIN, f"{LESSONS_BEGIN} x", 1),
         "invalid_block_layout"),
        ("prefix_before_end", lambda t: t.replace(LESSONS_END, f"x {LESSONS_END}", 1),
         "invalid_block_layout"),
        ("suffix_after_end", lambda t: t.replace(LESSONS_END, f"{LESSONS_END} x", 1),
         "prefixed_sentinel"),
    ],
)
def test_a_prefixed_or_suffixed_sentinel_is_rejected(
    memories_root: Path,
    db_path: Path,
    label: str,
    mutate: Any,
    diagnostic: str,
) -> None:
    """The sentinel/layout fail-closed branches, exercised rather than inspected.

    The sentinel pair is what says "this region is signed". A marker that is
    only a SUBSTRING of its line is a second, unsigned region wearing the same
    label, so it must not be read as the block.
    """
    from omniagentos.memlife.render import RENDERED_CLAIM_DIAGNOSTICS

    claim = "Claim behind a tampered sentinel"
    _set_status(db_path, "les_sentinel", "accepted")
    path = render_lessons(
        "project",
        [_lesson("les_sentinel", claim)],
        memories_root=memories_root,
    )
    assert extract_rendered_claims(path, memories_root=memories_root) == [claim]

    path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")

    RENDERED_CLAIM_DIAGNOSTICS.clear()
    try:
        assert extract_rendered_claims(path, memories_root=memories_root) == [], (
            f"{label}: a tampered sentinel line was read as a signed block"
        )
        assert RENDERED_CLAIM_DIAGNOSTICS[diagnostic] == 1, (
            f"{label}: expected the {diagnostic!r} branch to refuse it, got "
            f"{dict(RENDERED_CLAIM_DIAGNOSTICS)}"
        )
    finally:
        RENDERED_CLAIM_DIAGNOSTICS.clear()
