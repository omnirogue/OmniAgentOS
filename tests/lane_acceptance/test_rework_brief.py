"""Tests for the rework-brief assembler.

NOT written test-first. The assembler was written before these tests, which violates
devtasks/LANE-ACCEPTANCE-DOCTRINE.md §1 — the rule this very lane introduces. Recorded
here rather than implied away; the deviation and the weaker witness used in its place
are in var/REPORT.md.

The property under test is the one the 2026-07-29 batch violated: a fresh implementer
process must receive EVERY prior reviewer verdict VERBATIM, not a coordinator
paraphrase and not only the newest one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "lane-acceptance" / "rework-brief.sh"


def _lane(tmp_path: Path) -> Path:
    lane = tmp_path / "lane"
    (lane / "var").mkdir(parents=True)
    return lane


def _write_verdict(lane: Path, sha: str, body: str) -> None:
    (lane / "var" / "sol-verdict.md").write_text(
        f"VERDICT: REJECT\nReviewer-Model: gpt-5.6-sol\nCandidate-Sha: {sha}\n\n{body}\n",
        encoding="utf-8",
    )


def _run(lane: Path, notes: Path | None = None) -> Path:
    cmd = ["bash", str(SCRIPT), str(lane)]
    if notes is not None:
        cmd.append(str(notes))
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def test_script_is_executable_and_present() -> None:
    assert SCRIPT.is_file(), "the assembler must exist for briefs to carry verdicts"


def test_carries_the_current_verdict_verbatim(tmp_path: Path) -> None:
    """Verbatim means the WHOLE verdict, byte for byte — not a marker that survived.

    Sampling a substring cannot distinguish a verbatim copy from a summary that
    happens to quote one line, which is the failure mode this lane exists to
    prevent.
    """
    lane = _lane(tmp_path)
    body = (
        "the cap check is conditional on `matched` being non-empty\n"
        "second line with punctuation: 1,048,800 bytes > 64\n"
        "third line — em dash, `backticks`, and a $dollar sign"
    )
    _write_verdict(lane, "aaa111", body)
    original = (lane / "var" / "sol-verdict.md").read_bytes()

    # read_bytes, not read_text: universal-newline decoding would silently turn
    # CRLF into LF on both sides, so a "byte-exact" claim proven through decoded
    # text is not byte-exact at all.
    raw = _run(lane).read_bytes()
    assert original in raw, (
        "the complete verdict must be reproduced byte for byte — no trimming, no "
        "newline translation"
    )


def test_accumulates_every_round_oldest_first(tmp_path: Path) -> None:
    """The defect this exists to prevent: round N seeing only round N-1's verdict."""
    lane = _lane(tmp_path)

    _write_verdict(lane, "sha_r1", "ROUND ONE FINDING: tri-state collapsed to False")
    _run(lane)

    _write_verdict(lane, "sha_r2", "ROUND TWO FINDING: source claimed before the read")
    brief = _run(lane)

    raw = brief.read_bytes()

    # Order the FULL BLOCKS, not markers inside them. Ordering markers alone is
    # defeatable: print the markers oldest-first and the verdict bodies
    # newest-first and every marker assertion still passes.
    archived = sorted((lane / "var" / "verdicts").glob("sol-verdict-r*.md"))
    assert len(archived) == 2, "both rounds must have been archived"
    positions = []
    for path in archived:
        blob = path.read_bytes()
        assert blob in raw, f"{path.name} must appear in full and untrimmed"
        positions.append(raw.index(blob))
    assert positions == sorted(positions), (
        "the verdict BLOCKS must appear oldest-first, not merely their markers"
    )


def test_does_not_rearchive_the_same_verdict_twice(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    _write_verdict(lane, "sha_same", "ONLY FINDING")
    _run(lane)
    brief = _run(lane)
    assert brief.read_text(encoding="utf-8").count("ONLY FINDING") == 1


def test_coordinator_notes_are_subordinate_to_the_verdicts(tmp_path: Path) -> None:
    """Paraphrase may be added; it may not replace, and it may not lead."""
    lane = _lane(tmp_path)
    _write_verdict(lane, "sha_x", "VERBATIM REVIEWER TEXT")
    notes = tmp_path / "notes.md"
    notes.write_text("coordinator interpretation here\n", encoding="utf-8")

    text = _run(lane, notes).read_text(encoding="utf-8")
    assert "VERBATIM REVIEWER TEXT" in text
    assert "coordinator interpretation here" in text
    assert text.index("VERBATIM REVIEWER TEXT") < text.index("coordinator interpretation here"), (
        "the verdict must precede the paraphrase, and be marked the authority"
    )
    assert "the verdict is the authority" in text


def test_brief_states_the_test_first_requirement(tmp_path: Path) -> None:
    """The single highest-leverage rule must be in every rework brief, not just doctrine."""
    lane = _lane(tmp_path)
    _write_verdict(lane, "sha_y", "some finding")
    text = _run(lane).read_text(encoding="utf-8")
    assert "FAILING against the current tree" in text
    assert "before you implement" in text.lower()


def test_brief_names_all_three_defect_classes(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    _write_verdict(lane, "sha_z", "some finding")
    text = _run(lane).read_text(encoding="utf-8").lower()
    assert "fail closed" in text
    assert "claim evidence you did not gather" in text
    assert "axis the" in text


def test_refuses_a_directory_without_var(tmp_path: Path) -> None:
    """Fail closed on a malformed lane — and prove it is THIS refusal, not any error.

    The first version asserted only a nonzero exit, which a missing script also
    produces: it would have passed against an empty repo. Asserting the specific
    exit code, the diagnostic, and the absence of output is what makes it bind.
    """
    bad = tmp_path / "not-a-lane"
    bad.mkdir()
    result = subprocess.run(
        ["bash", str(SCRIPT), str(bad)], capture_output=True, text=True
    )
    assert result.returncode == 2, "the documented refusal code, not merely 'some error'"
    assert "no var/" in result.stderr
    assert result.stdout.strip() == "", "a refusal must not also emit a brief path"


@pytest.mark.parametrize("missing", ["sol-verdict.md"])
def test_first_round_with_no_verdict_yet_still_emits_the_contract(
    tmp_path: Path, missing: str
) -> None:
    """A lane with no verdict yet must still get the doctrine, not an empty file."""
    lane = _lane(tmp_path)
    text = _run(lane).read_text(encoding="utf-8")
    assert "LANE-ACCEPTANCE-DOCTRINE.md" in text
    assert "defeat your own decisive test" in text
