"""L4 acceptance: dream cycle — nightly consolidation with BYTE CONSERVATION.

Decisive assertion
------------------
After a cycle over a fixture containing 2 valid and 1 corrupt JSONL lines,
``report.bytes_conserved`` is True, the corrupt line lands in ``quarantine/``,
and its bytes are counted in ``quarantined_bytes``.

Counterfeit that must fail
--------------------------
Skip-on-parse-error (upstream ``auto_dream``): drop unparseable lines on rewrite
and report a clean run. That path must make ``bytes_conserved`` False. Written
deliberately below; confirmed failing; the real cycle must not do this.

NO_INPUT
--------
An empty or missing episodic source returns ``CycleStatus.NO_INPUT``, never
``COMPLETED``. "Nothing to do" and "nothing was there" are different facts.

Revert-check
------------
Both directions reported with real pytest output in the lane notes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.memlife.contracts import (
    CycleReport,
    CycleStatus,
    EpisodicEvent,
    EventResult,
)
from omniagentos.memlife.dream import (
    DREAM_CYCLE_CRON,
    DREAM_CYCLE_ROUTINE_NAME,
    dream_cycle_routine,
    ensure_dream_cycle_routine,
    run_dream_cycle,
)
from omniagentos.memlife.store import MemlifeStore
from omniagentos.scheduler.routines import cron_is_due, validate_routine

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _valid_event_line(
    event_id: str,
    *,
    reflection: str,
    skill: str = "swarm.coder",
) -> str:
    event = EpisodicEvent(
        id=event_id,
        ts=NOW,
        skill=skill,
        action="attempt",
        result=EventResult.FAILURE,
        pain=8.0,
        importance=9.0,
        reflection=reflection,
    )
    return event.model_dump_json()


def _write_fixture(path: Path, lines: list[str]) -> bytes:
    """Write JSONL lines and return the exact file bytes."""
    # Join with newline; trailing newline so every line has a terminator.
    text = "\n".join(lines) + "\n"
    raw = text.encode("utf-8")
    path.write_bytes(raw)
    return raw


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "var" / "memories" / "demo-project"
    store = MemlifeStore(root)
    store.ensure_layout()
    return root


# ---------------------------------------------------------------------------
# Decisive: byte conservation + quarantine of corrupt line
# ---------------------------------------------------------------------------


class TestDecisiveByteConservation:
    """THE decisive property for L4."""

    def test_two_valid_one_corrupt_conserves_and_quarantines(
        self, project_root: Path
    ) -> None:
        events_path = project_root / "episodic" / "events.jsonl"
        valid_a = _valid_event_line(
            "ev_a",
            reflection="Agents cannot commit inside a sandboxed worktree",
        )
        valid_b = _valid_event_line(
            "ev_b",
            reflection="Agents cannot commit inside sandboxed worktrees",
        )
        corrupt = "{this is not valid json at all!!!"
        raw = _write_fixture(events_path, [valid_a, valid_b, corrupt])
        assert len(raw) > 0

        report = run_dream_cycle(project_root, now=NOW)

        assert report.status == CycleStatus.COMPLETED
        assert report.input_bytes == len(raw)
        assert report.bytes_conserved is True, (
            f"conservation broken: kept={report.kept_bytes} "
            f"archived={report.archived_bytes} quarantined={report.quarantined_bytes} "
            f"input={report.input_bytes}"
        )
        assert report.quarantined_bytes > 0
        # Corrupt line (with its trailing newline) must be fully accounted.
        corrupt_line_bytes = len((corrupt + "\n").encode("utf-8"))
        assert report.quarantined_bytes == corrupt_line_bytes

        quarantine_dir = project_root / "quarantine"
        raw_blobs = list(quarantine_dir.glob("*.raw"))
        assert raw_blobs, "corrupt line must land in quarantine/, never dropped"
        blob_text = raw_blobs[0].read_text(encoding="utf-8")
        assert "this is not valid json" in blob_text

        # Valid lines must not vanish: either kept or archived.
        valid_bytes = len(raw) - corrupt_line_bytes
        assert report.kept_bytes + report.archived_bytes == valid_bytes

    def test_staged_candidates_when_events_cluster(self, project_root: Path) -> None:
        events_path = project_root / "episodic" / "events.jsonl"
        lines = [
            _valid_event_line(
                "ev_1",
                reflection="Agents cannot commit inside a sandboxed worktree",
            ),
            _valid_event_line(
                "ev_2",
                reflection="Agents cannot commit inside sandboxed worktrees",
            ),
        ]
        _write_fixture(events_path, lines)

        report = run_dream_cycle(project_root, now=NOW)

        assert report.status == CycleStatus.COMPLETED
        assert report.bytes_conserved
        assert report.staged >= 1
        store = MemlifeStore(project_root)
        pending = store.list_pending()
        assert len(pending) >= 1
        assert all(c.claim.strip() for c in pending)

    def test_recurring_cluster_outranks_one_off(self, project_root: Path) -> None:
        """W2.2 decisive: recurrence must actually reach the score.

        ``_attach_salience`` called ``salience_score(ev, now)`` and never passed
        the candidate's ``cluster_size``, so the function's ``recurrence``
        parameter was dead on the only path that calls it in production. The
        live store's 205-member recurring failure ranked identically to a
        one-off blurb — the second half of "every lesson scores the same".
        (Five largest clusters 205, 73, 69, 53, 52, all at 0.0, measured
        2026-08-06T18:36Z.)
        """
        events_path = project_root / "episodic" / "events.jsonl"
        recurring = [
            _valid_event_line(
                f"ev_rec_{i}",
                reflection="OAuth token refresh fails when the proxy pool rotates",
            )
            for i in range(5)
        ]
        one_off = [
            _valid_event_line(
                "ev_solo",
                reflection="Lockfiles should pin exact dependency versions",
            )
        ]
        _write_fixture(events_path, recurring + one_off)

        report = run_dream_cycle(project_root, now=NOW)
        assert report.status == CycleStatus.COMPLETED

        pending = MemlifeStore(project_root).list_pending()
        by_size = {c.cluster_size: c for c in pending}
        assert 5 in by_size, f"expected a 5-member cluster; got {sorted(by_size)}"
        assert 1 in by_size, f"expected a one-off cluster; got {sorted(by_size)}"

        big = by_size[5].salience
        solo = by_size[1].salience
        assert big is not None and solo is not None, (
            f"both candidates must be scorable; got big={big!r} solo={solo!r}"
        )
        assert solo > 0.0, "a scorable one-off must not be annihilated to 0.0"
        assert big > solo, (
            f"a 5-member recurring cluster must outrank a one-off; got {big} vs {solo}"
        )


class TestCaptureIsIdempotentByEventId:
    """The watermark boundary row must be archived exactly once.

    ``since`` is an inclusive lower bound, so the newest already-captured event
    is re-read on every tick. It cannot be made exclusive: attempt timestamps
    are second-resolution and co-timed attempts would be dropped for good. So
    duplicates are suppressed by event id, which cannot lose a distinct attempt.
    """

    _SCHEMA = """
    CREATE TABLE sessions (id TEXT PRIMARY KEY, cost_usd REAL, usage_source TEXT);
    CREATE TABLE swarm_attempts (
        id TEXT PRIMARY KEY, swarm_run_id TEXT NOT NULL, board_task_id TEXT NOT NULL,
        seq INTEGER NOT NULL, session_id TEXT, provider TEXT NOT NULL,
        model TEXT NOT NULL, tier TEXT, started_at TEXT NOT NULL, ended_at TEXT,
        end_reason TEXT, detail TEXT NOT NULL DEFAULT '', cost_usd REAL,
        usage_source TEXT);
    """

    def _db(self, path: Path) -> Path:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(self._SCHEMA)
            # Two attempts ending in the SAME second — the case an exclusive
            # watermark would silently drop.
            for i in (0, 1):
                conn.execute(
                    "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, "
                    "seq, session_id, provider, model, tier, started_at, ended_at, "
                    "end_reason, detail) VALUES (?,?,?,?,NULL,'codex','m','complex',"
                    "'2026-07-28T10:00:00Z','2026-07-28T11:00:00Z','crashed',?)",
                    (f"swa_{i}", "swr", f"btk_{i}", 0, f"co-timed failure {i}"),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_boundary_row_is_not_re_archived_and_co_timed_rows_survive(
        self, project_root: Path
    ) -> None:
        db = self._db(project_root.parent / "attempts.sqlite3")
        boundary = "2026-07-28T11:00:00Z"  # both attempts' ended_at

        run_dream_cycle(project_root, now=NOW, db_path=db)
        # Second tick with the watermark sitting exactly on both events.
        run_dream_cycle(project_root, now=NOW, db_path=db, since=boundary)
        run_dream_cycle(project_root, now=NOW, db_path=db, since=boundary)

        archive = (project_root / "episodic" / "archive.jsonl").read_text()
        ids = [json.loads(line)["id"] for line in archive.splitlines() if line.strip()]
        assert sorted(ids) == ["swa_0", "swa_1"], (
            f"each distinct attempt archived exactly once; got {ids}"
        )

    def test_nothing_captured_means_the_archive_is_not_scanned(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The archive scan is O(archive) and runs inside the scheduler's lock.

        ``builtin_jobs.run_memlife_dream_cycle`` wraps the cycle in
        ``with lock:``, and ``archive.jsonl`` is append-only and never rotated —
        measured on a copy of the live archive at 400x its size (220 MB), the
        old read_text()+splitlines() form took 1163 ms and peaked at 1657 MB RSS
        per tick. With a watermark, the steady-state tick captures nothing, so
        the scan has nothing to de-duplicate against and must not happen.
        """
        db = self._db(project_root.parent / "attempts_none.sqlite3")
        run_dream_cycle(project_root, now=NOW, db_path=db)  # captures both rows

        def _explode(_store: object) -> set[str]:
            raise AssertionError("the whole archive was scanned with nothing captured")

        monkeypatch.setattr("omniagentos.memlife.dream._known_event_ids", _explode)
        # Watermark past every attempt: capture returns zero rows.
        report = run_dream_cycle(
            project_root, now=NOW, db_path=db, since="2027-01-01T00:00:00Z"
        )
        assert report.status is not CycleStatus.FAILED, report.errors

    def test_the_scan_still_runs_when_there_is_something_to_deduplicate(
        self, project_root: Path
    ) -> None:
        """Skipping when captured is empty must not skip when it is not."""
        db = self._db(project_root.parent / "attempts_some.sqlite3")
        run_dream_cycle(project_root, now=NOW, db_path=db)
        run_dream_cycle(project_root, now=NOW, db_path=db)  # same rows again

        archive = (project_root / "episodic" / "archive.jsonl").read_text()
        ids = [json.loads(line)["id"] for line in archive.splitlines() if line.strip()]
        assert sorted(ids) == ["swa_0", "swa_1"], f"re-captured a known event: {ids}"


# ---------------------------------------------------------------------------
# Counterfeit: skip-on-parse-error must break conservation
# ---------------------------------------------------------------------------


def _counterfeit_skip_on_parse_error(raw: bytes) -> CycleReport:
    """Upstream auto_dream behaviour: drop unparseable lines, report clean.

    Deliberately does NOT quarantine. Conservation must fail whenever a corrupt
    line was present in the input.
    """
    input_bytes = len(raw)
    kept_bytes = 0
    archived_bytes = 0
    # Split like the real cycle, but skip corrupt without counting.
    start = 0
    for i, b in enumerate(raw):
        if b == 0x0A:
            line = raw[start : i + 1]
            start = i + 1
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                EpisodicEvent.model_validate_json(text)
            except Exception:
                # THE counterfeit: drop and do not count.
                continue
            archived_bytes += len(line)
    if start < len(raw):
        line = raw[start:]
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            try:
                EpisodicEvent.model_validate_json(text)
                archived_bytes += len(line)
            except Exception:
                pass

    return CycleReport(
        status=CycleStatus.COMPLETED,
        input_bytes=input_bytes,
        kept_bytes=kept_bytes,
        archived_bytes=archived_bytes,
        quarantined_bytes=0,  # never counted — the upstream lie
        staged=0,
        graduated=0,
        errors=[],
    )


class TestCounterfeitSkipOnParseError:
    def test_skip_on_parse_error_fails_conservation(self, project_root: Path) -> None:
        events_path = project_root / "episodic" / "events.jsonl"
        valid = _valid_event_line(
            "ev_ok",
            reflection="Switch accounts when rate limited instead of waiting forever",
        )
        corrupt = "NOT-JSON{{{{"
        raw = _write_fixture(events_path, [valid, corrupt])

        counterfeit = _counterfeit_skip_on_parse_error(raw)
        assert counterfeit.bytes_conserved is False, (
            "counterfeit must NOT conserve: it drops the corrupt line silently"
        )

        # Real path must conserve (revert direction: correct implementation).
        real = run_dream_cycle(project_root, now=NOW)
        assert real.bytes_conserved is True
        assert real.quarantined_bytes > 0


# ---------------------------------------------------------------------------
# NO_INPUT: empty / missing episodic source
# ---------------------------------------------------------------------------


class TestNoInput:
    def test_missing_events_file_is_no_input(self, project_root: Path) -> None:
        events_path = project_root / "episodic" / "events.jsonl"
        assert not events_path.exists()

        report = run_dream_cycle(project_root, now=NOW)

        assert report.status == CycleStatus.NO_INPUT
        assert report.status != CycleStatus.COMPLETED
        assert report.input_bytes == 0
        assert report.bytes_conserved  # 0+0+0 == 0

    def test_empty_events_file_is_no_input(self, project_root: Path) -> None:
        events_path = project_root / "episodic" / "events.jsonl"
        events_path.write_bytes(b"")

        report = run_dream_cycle(project_root, now=NOW)

        assert report.status == CycleStatus.NO_INPUT
        assert report.status != CycleStatus.COMPLETED
        assert report.input_bytes == 0

    def test_missing_store_root_is_no_input(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        report = run_dream_cycle(missing, now=NOW)
        assert report.status == CycleStatus.NO_INPUT
        assert report.input_bytes == 0


# ---------------------------------------------------------------------------
# Pipeline composition: capture path optional; pure stages always run
# ---------------------------------------------------------------------------


class TestPipelineStages:
    def test_near_duplicate_of_lesson_is_not_re_staged(
        self, project_root: Path
    ) -> None:
        from omniagentos.memlife.contracts import Lesson, LessonStatus

        store = MemlifeStore(project_root)
        claim = "Agents cannot commit inside a sandboxed worktree"
        lesson = Lesson(
            id="les_prior",
            candidate_id="cand_prior",
            claim=claim,
            status=LessonStatus.ACCEPTED,
            graduated_at=NOW,
            graduated_by="owner",
        )
        store.save_lesson(lesson)

        events_path = project_root / "episodic" / "events.jsonl"
        _write_fixture(
            events_path,
            [
                _valid_event_line("ev_dup", reflection=claim),
            ],
        )

        report = run_dream_cycle(project_root, now=NOW)
        assert report.status == CycleStatus.COMPLETED
        assert report.bytes_conserved
        # Near-dup of existing lesson must not re-stage.
        assert report.staged == 0
        assert store.list_pending() == []


# ---------------------------------------------------------------------------
# Routine registration (mirror improve-dispatcher)
# ---------------------------------------------------------------------------


class TestDreamCycleRoutine:
    def test_routine_payload_is_valid(self) -> None:
        payload = dream_cycle_routine()
        validate_routine(payload)
        assert payload["name"] == DREAM_CYCLE_ROUTINE_NAME
        assert payload["trigger_config"]["cron"] == DREAM_CYCLE_CRON

    def test_cron_is_nightly(self) -> None:
        # 03:00 UTC is due; 03:07 is not (already fired at 03:00).
        due = cron_is_due(DREAM_CYCLE_CRON, datetime(2026, 7, 28, 3, 0), last_fired=None)
        assert due is True
        not_due = cron_is_due(
            DREAM_CYCLE_CRON,
            datetime(2026, 7, 28, 3, 7),
            last_fired="2026-07-28T03:00:00Z",
        )
        assert not_due is False

    def test_ensure_registers_once(self, tmp_path: Path) -> None:
        store = SqliteStore(str(tmp_path / "routines.db"))
        res1 = ensure_dream_cycle_routine(store)
        res2 = ensure_dream_cycle_routine(store)
        assert res1["name"] == DREAM_CYCLE_ROUTINE_NAME
        assert res2["name"] == DREAM_CYCLE_ROUTINE_NAME
        rows = store._connection.execute(
            "SELECT COUNT(*) FROM routines WHERE name = ?",
            (DREAM_CYCLE_ROUTINE_NAME,),
        ).fetchone()
        assert rows[0] == 1


# ---------------------------------------------------------------------------
# Revert-check helpers (exercised by the counterfeit suite above)
# ---------------------------------------------------------------------------


def test_conservation_property_identity() -> None:
    """Sanity: the contracts property matches the accounting identity."""
    ok = CycleReport(
        status=CycleStatus.COMPLETED,
        input_bytes=100,
        kept_bytes=50,
        archived_bytes=30,
        quarantined_bytes=20,
    )
    assert ok.bytes_conserved
    bad = CycleReport(
        status=CycleStatus.COMPLETED,
        input_bytes=100,
        kept_bytes=50,
        archived_bytes=30,
        quarantined_bytes=0,
    )
    assert not bad.bytes_conserved
