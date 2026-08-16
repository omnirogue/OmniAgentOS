"""memlife dream/compaction over corrupt JSONL — every input byte accounted for.

Byte conservation is the decisive property (memlife.contracts.CycleReport):
``kept_bytes + archived_bytes + quarantined_bytes == input_bytes``.  This
feeds the cycle the three classic corruption shapes — a truncated line, an
interleaved partial double-write, and undecodable binary garbage — and proves
nothing is silently dropped: valid records are archived, every corrupt record
is quarantined AND counted, and the on-disk quarantine blobs exist.

Follows tests/memlife/test_dream.py idioms (MemlifeStore layout + real
EpisodicEvent JSON lines).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.memlife.contracts import CycleStatus, EpisodicEvent, EventResult
from omniagentos.memlife.dream import run_dream_cycle
from omniagentos.memlife.store import MemlifeStore

NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)


def _valid_event_line(event_id: str, reflection: str) -> str:
    return EpisodicEvent(
        id=event_id,
        ts=NOW,
        skill="swarm.coder",
        action="attempt",
        result=EventResult.FAILURE,
        pain=8.0,
        importance=9.0,
        reflection=reflection,
    ).model_dump_json()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "var" / "memories" / "fh-corrupt-project"
    MemlifeStore(root).ensure_layout()
    return root


def test_corrupt_jsonl_conserves_every_byte_and_record(project_root: Path) -> None:
    events_path = project_root / "episodic" / "events.jsonl"

    valid_a = _valid_event_line("evt_fh_a", "always pin sqlite busy_timeout in workers")
    valid_b = _valid_event_line("evt_fh_b", "never launch lane subprocesses via login shells")

    # 1. Truncated line: a valid record cut mid-JSON (crash mid-append).
    truncated = valid_a[: len(valid_a) // 2]
    # 2. Interleaved partial writes: two writers' fragments glued on one line.
    interleaved = valid_a[: len(valid_a) // 3] + valid_b[len(valid_b) // 2 :]
    # 3. Binary garbage: invalid UTF-8, so the line cannot even decode.
    garbage = b"\xff\xfe\x00\x01BINARY-GARBAGE\x80\x81\xfd"
    assert pytest.raises(UnicodeDecodeError, garbage.decode, "utf-8")

    raw = (
        valid_a.encode("utf-8")
        + b"\n"
        + truncated.encode("utf-8")
        + b"\n"
        + interleaved.encode("utf-8")
        + b"\n"
        + garbage
        + b"\n"
        + valid_b.encode("utf-8")
        + b"\n"
    )
    events_path.write_bytes(raw)
    input_bytes = len(raw)
    corrupt_bytes = (
        len(truncated.encode("utf-8")) + 1 + len(interleaved.encode("utf-8")) + 1 + len(garbage) + 1
    )
    valid_bytes = len(valid_a.encode("utf-8")) + 1 + len(valid_b.encode("utf-8")) + 1
    assert corrupt_bytes + valid_bytes == input_bytes

    report = run_dream_cycle(project_root, now=NOW)

    # Accounting is exact, byte for byte and record for record.
    assert report.status is CycleStatus.COMPLETED
    assert report.input_bytes == input_bytes
    assert report.bytes_conserved, (
        f"kept={report.kept_bytes} archived={report.archived_bytes} "
        f"quarantined={report.quarantined_bytes} != input={report.input_bytes}"
    )
    assert report.kept_bytes + report.archived_bytes + report.quarantined_bytes == input_bytes
    assert report.quarantined_bytes == corrupt_bytes
    assert report.archived_bytes == valid_bytes
    assert report.kept_bytes == 0

    # All 3 corrupt records are visible in the errors, none silently dropped.
    quarantine_notes = [e for e in report.errors if "quarantined unparseable line" in e]
    assert len(quarantine_notes) == 3

    # Corrupt content is retained on disk under quarantine/, not deleted.
    quarantine_dir = project_root / "quarantine"
    blobs = list(quarantine_dir.rglob("*"))
    blob_files = [p for p in blobs if p.is_file()]
    assert blob_files, "quarantine directory must retain the corrupt records"
    quarantined_text = "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in blob_files
    )
    assert truncated[:40] in quarantined_text
    assert "BINARY-GARBAGE" in quarantined_text

    # Both valid records were archived verbatim; the hot log was compacted.
    archive_text = (project_root / "episodic" / "archive.jsonl").read_text(encoding="utf-8")
    assert valid_a in archive_text
    assert valid_b in archive_text
    assert events_path.read_bytes() == b""


def test_partial_write_without_trailing_newline_is_counted(project_root: Path) -> None:
    """A crash mid-append leaves an unterminated fragment — still conserved."""
    events_path = project_root / "episodic" / "events.jsonl"
    valid = _valid_event_line("evt_fh_tail", "tail fragment case")
    fragment = valid[: len(valid) - 25]  # no trailing newline, invalid JSON
    raw = valid.encode("utf-8") + b"\n" + fragment.encode("utf-8")
    events_path.write_bytes(raw)

    report = run_dream_cycle(project_root, now=NOW)

    assert report.status is CycleStatus.COMPLETED
    assert report.input_bytes == len(raw)
    assert report.bytes_conserved
    assert report.quarantined_bytes == len(fragment.encode("utf-8"))
    assert report.archived_bytes == len(valid.encode("utf-8")) + 1
