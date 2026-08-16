"""scripts/ops/rotate_loop_logs.py -- threshold, live-fd truncation, retention, dry-run.

Every scenario uses a scratch `tmp_path`, never the real `var/loopqueue/logs`:
a rotator whose own tests could truncate a live operational log would be a
joke at its own expense.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

from scripts.ops import rotate_loop_logs

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _archives(logs_dir: Path, log_name: str) -> list[Path]:
    return sorted(logs_dir.glob(f"{log_name}.rotated-*.gz"))


# --------------------------------------------------------------- threshold --
def test_rotation_threshold_honored(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    under = logs_dir / "small.log"
    under.write_bytes(b"x" * 10)
    over = logs_dir / "big.log"
    over.write_bytes(b"y" * 20)

    report = rotate_loop_logs.rotate_logs(logs_dir, threshold_bytes=10, now=NOW)

    assert "small.log" in report["skipped"]
    assert not _archives(logs_dir, "small.log")
    assert under.read_bytes() == b"x" * 10  # untouched: size == threshold, not > threshold

    assert any(name.startswith("big.log ->") for name in report["rotated"])
    archives = _archives(logs_dir, "big.log")
    assert len(archives) == 1
    with gzip.open(archives[0], "rb") as fh:
        assert fh.read() == b"y" * 20
    assert over.read_bytes() == b""  # truncated in place


# -------------------------------------------------------- live-fd rewrite --
def test_truncation_keeps_live_fd_writable(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_path = logs_dir / "governor.log"
    log_path.write_bytes(b"")

    fd = log_path.open("ab")
    try:
        fd.write(b"first-chunk-before-rotation\n")
        fd.flush()

        rotate_loop_logs.rotate_logs(logs_dir, threshold_bytes=5, now=NOW)

        # The already-open append-mode fd keeps working against the SAME
        # (now-truncated) file -- no reopen, no signal.
        fd.write(b"second-chunk-after-rotation\n")
        fd.flush()
    finally:
        fd.close()

    archives = _archives(logs_dir, "governor.log")
    assert len(archives) == 1
    with gzip.open(archives[0], "rb") as gh:
        archived = gh.read()
    live = log_path.read_bytes()

    assert archived == b"first-chunk-before-rotation\n"
    assert live == b"second-chunk-after-rotation\n"
    # both halves accounted for, nothing dropped between archive and live file
    assert archived + live == b"first-chunk-before-rotation\nsecond-chunk-after-rotation\n"


# -------------------------------------------------------------- retention --
def test_retention_prunes_to_three_newest(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_path = logs_dir / "bridge.log"
    log_path.write_bytes(b"z" * 20)

    stamps = [
        "20260810T000000Z",
        "20260811T000000Z",
        "20260812T000000Z",
        "20260813T000000Z",
        "20260814T000000Z",
    ]
    for stamp in stamps:
        archive = logs_dir / f"bridge.log.rotated-{stamp}.gz"
        with gzip.open(archive, "wb") as fh:
            fh.write(b"old-archive")

    # A fresh rotation adds a 6th (newest) archive; retention must prune to 3.
    rotate_loop_logs.rotate_logs(logs_dir, threshold_bytes=10, now=NOW)

    archives = _archives(logs_dir, "bridge.log")
    assert len(archives) == 3
    kept_stamps = {p.name.split("rotated-")[1].removesuffix(".gz") for p in archives}
    assert kept_stamps == {
        "20260813T000000Z",
        "20260814T000000Z",
        NOW.strftime("%Y%m%dT%H%M%SZ"),
    }


def test_retention_never_prunes_a_different_logs_archives(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    # bridge.log has 4 old archives (over the keep-3 limit)...
    for stamp in ["20260810T000000Z", "20260811T000000Z", "20260812T000000Z", "20260813T000000Z"]:
        with gzip.open(logs_dir / f"bridge.log.rotated-{stamp}.gz", "wb") as fh:
            fh.write(b"bridge-old")
    # ...governor.log has only 1, well under the limit.
    with gzip.open(logs_dir / "governor.log.rotated-20260813T000000Z.gz", "wb") as fh:
        fh.write(b"governor-old")

    (logs_dir / "bridge.log").write_bytes(b"")
    (logs_dir / "governor.log").write_bytes(b"")

    rotate_loop_logs.rotate_logs(logs_dir, threshold_bytes=1_000_000, now=NOW)  # nothing over size

    # Pruning only runs for logs that were rotated THIS pass; neither log
    # exceeded the threshold, so both archive sets are left exactly as-is.
    assert len(_archives(logs_dir, "bridge.log")) == 4
    assert len(_archives(logs_dir, "governor.log")) == 1


# ---------------------------------------------------------------- dry-run --
def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_path = logs_dir / "big.log"
    content = b"y" * 20
    log_path.write_bytes(content)
    for stamp in ["20260810T000000Z", "20260811T000000Z", "20260812T000000Z", "20260813T000000Z"]:
        with gzip.open(logs_dir / f"big.log.rotated-{stamp}.gz", "wb") as fh:
            fh.write(b"old")

    before = {p.name: p.read_bytes() for p in logs_dir.iterdir()}

    report = rotate_loop_logs.rotate_logs(logs_dir, threshold_bytes=10, dry_run=True, now=NOW)

    after = {p.name: p.read_bytes() for p in logs_dir.iterdir()}
    assert before == after  # byte-identical directory, nothing created/deleted/truncated
    assert any(name.startswith("big.log ->") for name in report["rotated"])


# ------------------------------------------------------- non-.log files --
def test_never_touches_non_log_files(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    err_path = logs_dir / "bridge.err"
    err_path.write_bytes(b"e" * 50)
    misc_path = logs_dir / "notes.txt"
    misc_path.write_bytes(b"m" * 50)

    rotate_loop_logs.rotate_logs(logs_dir, threshold_bytes=10, now=NOW)

    assert err_path.read_bytes() == b"e" * 50
    assert misc_path.read_bytes() == b"m" * 50
    assert not list(logs_dir.glob("*.rotated-*.gz"))


# -------------------------------------------------------------------- cli --
def test_cli_dry_run_exits_zero(tmp_path: Path, capsys) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "big.log").write_bytes(b"y" * 20)

    rc = rotate_loop_logs.main(["--logs-dir", str(logs_dir), "--threshold-bytes", "10", "--dry-run"])

    assert rc == 0
    assert not list(logs_dir.glob("*.rotated-*.gz"))
    err = capsys.readouterr().err
    assert "[dry-run]" in err
