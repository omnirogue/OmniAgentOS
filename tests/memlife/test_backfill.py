"""BLK-001: the already-corrupted backlog must be repairable from its evidence.

The scoring fix is correct going forward, but 210 live candidates were already
persisted at salience 0.0 in BOTH carriers, and the dream cycle cannot reach
them: it re-scores only what is in the hot ``events.jsonl``, that file is
emptied at the end of every cycle, and an empty one returns ``NO_INPUT``.

Decisive: after a re-score the degenerate backlog separates, and a recurring
cluster outranks a one-off. Counterfeit: a candidate whose evidence is gone must
come back ``None`` — unknown and kept — never a placeholder 0.0, and never
silently left at the stale value while the report claims a repair.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.memlife.backfill import main, rederive_archive, rescore_candidates
from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    Decision,
    DecisionAction,
)
from omniagentos.memlife.store import MemlifeStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, cost_usd REAL, usage_source TEXT);
CREATE TABLE swarm_attempts (
    id TEXT PRIMARY KEY, swarm_run_id TEXT NOT NULL, board_task_id TEXT NOT NULL,
    seq INTEGER NOT NULL, session_id TEXT, provider TEXT NOT NULL, model TEXT NOT NULL,
    tier TEXT, started_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT,
    detail TEXT NOT NULL DEFAULT '', cost_usd REAL, usage_source TEXT);
CREATE TABLE memlife_candidates (
    id TEXT PRIMARY KEY, key TEXT NOT NULL, claim TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '', evidence_ids_json TEXT NOT NULL,
    cluster_size INTEGER NOT NULL, status TEXT NOT NULL,
    rejection_count INTEGER NOT NULL DEFAULT 0, salience REAL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def _attempt(conn: sqlite3.Connection, attempt_id: str, *, reason: str, tier: str) -> None:
    conn.execute(
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, session_id, "
        "provider, model, tier, started_at, ended_at, end_reason, detail) "
        "VALUES (?,?,?,?,NULL,'codex','m',?,'2026-07-28T10:00:00Z',"
        "'2026-07-28T11:00:00Z',?,'detail')",
        (attempt_id, "swr", f"btk_{attempt_id}", 0, tier, reason),
    )


def _stage(
    conn: sqlite3.Connection,
    candidate_id: str,
    evidence: list[str],
    *,
    status: str = "staged",
    salience: float | None = 0.0,
) -> None:
    conn.execute(
        "INSERT INTO memlife_candidates (id, key, claim, conditions, evidence_ids_json, "
        "cluster_size, status, rejection_count, salience, created_at, updated_at) "
        "VALUES (?,?,?,'',?,?,?,0,?,'t0','t0')",
        (
            candidate_id,
            f"k/{candidate_id}",
            f"claim {candidate_id}",
            json.dumps(evidence),
            len(evidence),
            status,
            salience,
        ),
    )


@pytest.fixture()
def corrupted(tmp_path: Path) -> tuple[sqlite3.Connection, Path, MemlifeStore]:
    """A backlog in the state the live store is in: everything at 0.0."""
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)

    recurring = [f"swa_r{i}" for i in range(4)]
    for a in recurring:
        _attempt(conn, a, reason="crashed", tier="complex")
    _attempt(conn, "swa_solo", reason="completed", tier="simple")

    _stage(conn, "cand_recurring", recurring)
    _stage(conn, "cand_one_off", ["swa_solo"])
    _stage(conn, "cand_orphan", ["swa_pruned"])  # evidence no longer exists
    _stage(conn, "cand_decided", recurring, status="graduated", salience=0.0)
    conn.commit()

    store = MemlifeStore(tmp_path / "memlife")
    store.ensure_layout()
    for cid, evidence in (
        ("cand_recurring", recurring),
        ("cand_one_off", ["swa_solo"]),
        ("cand_orphan", ["swa_pruned"]),
    ):
        store.save_candidate(
            Candidate(
                id=cid,
                key=f"k/{cid}",
                claim=f"claim {cid}",
                evidence_ids=evidence,
                cluster_size=len(evidence),
                status=CandidateStatus.STAGED,
                decisions=[
                    Decision(action=DecisionAction.STAGE, at=NOW, actor="dream-cycle")
                ],
                salience=0.0,
            )
        )
    return conn, db, store


class TestDecisiveBacklogRepair:
    def test_degenerate_backlog_separates(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted

        before = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT id, salience FROM memlife_candidates WHERE status='staged'"
            )
        }
        assert set(before.values()) == {0.0}, f"precondition: all zero; got {before}"

        report = rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()

        assert report.examined == 3, report.as_dict()
        assert report.before_distinct == 1
        assert report.after_distinct > 1, "salience is still degenerate after the repair"

        after = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT id, salience FROM memlife_candidates WHERE status='staged'"
            )
        }
        assert after["cand_recurring"] is not None
        assert after["cand_one_off"] is not None
        assert after["cand_recurring"] > after["cand_one_off"], (
            f"a recurring cluster must outrank a one-off; got {after}"
        )
        assert after["cand_one_off"] > 0.0

    def test_repair_reaches_the_filesystem_mirror(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """Both carriers or neither — a split brain nothing would detect."""
        conn, db, store = corrupted
        report = rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()

        assert report.mirror_written == 3
        mirrored = store.load_candidate("cand_recurring").salience
        sql = conn.execute(
            "SELECT salience FROM memlife_candidates WHERE id='cand_recurring'"
        ).fetchone()[0]
        assert mirrored == sql
        assert mirrored is not None and mirrored > 0.0

    def test_decided_candidates_are_left_alone(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """Re-scoring is a data repair, not a lifecycle decision."""
        conn, db, store = corrupted
        rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()
        row = conn.execute(
            "SELECT status, salience FROM memlife_candidates WHERE id='cand_decided'"
        ).fetchone()
        assert row[0] == "graduated"
        assert row[1] == 0.0, "a decided candidate must keep the score it was decided under"

    def test_one_offs_are_rescored_not_retired(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """A cluster of one is a real observation a human may still want to judge.

        With a working score it WILL sort to the bottom once something ranks
        by salience — nothing does yet, measured 2026-08-06T18:36Z — which is
        the correct outcome; deleting it is a product decision nobody
        authorised.
        """
        conn, db, store = corrupted
        rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()
        row = conn.execute(
            "SELECT status, salience FROM memlife_candidates WHERE id='cand_one_off'"
        ).fetchone()
        assert row[0] == "staged", "a one-off must remain reviewable"
        assert row[1] is not None and row[1] > 0.0


class TestCounterfeitPlaceholderRepair:
    """Unknown must survive the repair as unknown."""

    def test_pruned_evidence_yields_none_not_zero(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        report = rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()

        assert report.unresolvable == 1
        value = conn.execute(
            "SELECT salience FROM memlife_candidates WHERE id='cand_orphan'"
        ).fetchone()[0]
        assert value is None, (
            f"a candidate whose evidence is gone must be unknown, not 0.0; got {value!r}"
        )
        assert store.load_candidate("cand_orphan").salience is None


class TestCliRefusesAHalfLandedRepair:
    """BLK-001: ``main()`` is the only path that will ever run in production.

    It used to return 0 unless ``examined == 0``: ``report.errors`` was never
    consulted and ``conn.commit()`` ran unconditionally under ``--apply``. Two
    silent half-lands, both executed against a copy of the live store:

    - ``--apply`` without ``--store`` (and ``--store`` defaulted to ``None``, so
      this was the DEFAULT invocation): 209 SQLite rows repaired, **0** mirror
      files written, ``errors: []``, rc 0, nothing on stderr.
    - ``--apply`` with an unwritable mirror: 210 ``mirror write failed`` entries,
      SQLite committed anyway, rc 0, stderr empty.

    The module's own comment says "Both carriers or neither: a queue row and its
    candidate file that disagree about salience is a split brain that nothing
    would detect."
    """

    @staticmethod
    def _sqlite_scores(db: Path) -> dict[str, float | None]:
        conn = sqlite3.connect(db)
        try:
            return dict(
                conn.execute(
                    "SELECT id, salience FROM memlife_candidates WHERE status='staged'"
                )
            )
        finally:
            conn.close()

    def test_apply_without_a_named_carrier_is_refused(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The default invocation must not silently repair one carrier."""
        conn, db, store = corrupted
        conn.close()

        rc = main(["--db", str(db), "--apply"])

        assert rc == 2, "a carrier-less --apply must refuse, not half-land"
        assert set(self._sqlite_scores(db).values()) == {0.0}, "SQLite was written anyway"
        assert store.load_candidate("cand_recurring").salience == 0.0

    def test_store_and_sqlite_only_together_are_refused(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        conn.close()
        rc = main(["--db", str(db), "--store", str(store.root), "--apply", "--sqlite-only"])
        assert rc == 2
        assert set(self._sqlite_scores(db).values()) == {0.0}

    def test_sqlite_only_is_an_explicit_opt_in_and_says_so(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Split-braining on purpose is allowed; doing it by default is not."""
        conn, db, store = corrupted
        conn.close()

        rc = main(["--db", str(db), "--apply", "--sqlite-only"])

        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["store_path"] is None, (
            "a SQLite-only run must record that no mirror was touched"
        )
        assert report["mirror_written"] == 0
        assert set(self._sqlite_scores(db).values()) != {0.0}
        assert store.load_candidate("cand_recurring").salience == 0.0

    def test_both_carriers_land_together_on_the_normal_invocation(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        conn, db, store = corrupted
        conn.close()

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["committed"] is True
        assert report["errors"] == []
        assert report["mirror_written"] == report["examined"]
        sql = self._sqlite_scores(db)
        assert sql["cand_recurring"] == store.load_candidate("cand_recurring").salience
        assert sql["cand_recurring"] is not None

    def test_unwritable_mirror_writes_nothing_and_exits_nonzero(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The decisive counterfeit: 210 mirror failures used to exit 0."""
        conn, db, store = corrupted
        conn.close()
        store.candidates_dir.chmod(0o500)
        try:
            rc = main(["--db", str(db), "--store", str(store.root), "--apply"])
        finally:
            store.candidates_dir.chmod(0o700)

        captured = capsys.readouterr()
        assert rc == 1, "mirror failures must not exit 0"
        report = json.loads(captured.out)
        assert report["errors"], "the failure must be named, not counted silently"
        assert report["committed"] is False
        assert "ERROR:" in captured.err, "stderr must not be empty on a failed repair"
        assert set(self._sqlite_scores(db).values()) == {0.0}, (
            "SQLite must be rolled back, not left ahead of the mirror"
        )

    def test_dry_run_is_the_default_and_writes_nothing(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        conn, db, store = corrupted
        conn.close()

        rc = main(["--db", str(db), "--store", str(store.root)])

        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["dry_run"] is True
        assert report["committed"] is False
        assert report["after_distinct"] > 1, "a dry run must still compute the outcome"
        assert set(self._sqlite_scores(db).values()) == {0.0}

    def test_a_locked_database_is_a_named_refusal_with_a_receipt(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """R2-003: the live scheduler writes to this database.

        ``sqlite3.OperationalError: database is locked`` was caught nowhere —
        it escaped ``rescore_candidates``, escaped ``main``'s ``try`` (which had
        only ``finally: conn.close()``) and killed the process with a traceback:
        measured ``elapsed=5.4s rc=None uncaught=OperationalError``,
        ``receipts written: []``. The module's own docstring says the receipt
        "is written on FAILURE too — that is the case where it matters most",
        and on this path it was not written at all.
        """
        conn, db, store = corrupted
        conn.close()
        archive = store.episodic_dir / "archive.jsonl"
        TestArchiveIsRederivedNotFrozen._archive(
            store, [TestArchiveIsRederivedNotFrozen._laundered("swa_r0")]
        )
        before = archive.read_text(encoding="utf-8")

        blocker = sqlite3.connect(db, timeout=0.1)
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute("UPDATE memlife_candidates SET updated_at = updated_at")
        try:
            rc = main(["--db", str(db), "--store", str(store.root), "--apply"])
        finally:
            blocker.rollback()
            blocker.close()

        captured = capsys.readouterr()
        assert rc == 1, "contention must be a refusal, not a traceback"
        report = json.loads(captured.out)
        assert report["committed"] is False
        assert any("refused the repair" in e for e in report["errors"])
        assert "ERROR:" in captured.err
        assert archive.read_text(encoding="utf-8") == before, (
            "a run stopped by contention must not have rewritten the archive"
        )
        assert set(self._sqlite_scores(db).values()) == {0.0}
        (receipt_path,) = list(store.root.parent.rglob("*backfill-receipt-*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["exit_code"] == 1
        assert receipt["committed"] is False
        assert receipt["summary"]["errors"], "the receipt must say why it failed"

    def test_no_carrier_is_written_until_every_carrier_is_pre_flighted(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """R2-002: the archive used to be rewritten before the mirror pre-flight.

        Measured with one candidate file removed: rc 1, stderr
        "FAILED: 1 error(s); SQLite rolled back, nothing repaired." — while the
        archive had gone from 910 lines to 904. A permanent record WAS rewritten
        and the operator was told the opposite.
        """
        conn, db, store = corrupted
        conn.close()
        archive = store.episodic_dir / "archive.jsonl"
        TestArchiveIsRederivedNotFrozen._archive(
            store,
            [
                TestArchiveIsRederivedNotFrozen._laundered("swa_r0"),
                TestArchiveIsRederivedNotFrozen._laundered("swa_r0"),
            ],
        )
        before = archive.read_text(encoding="utf-8")
        (store.candidates_dir / "cand_one_off.json").unlink()

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        captured = capsys.readouterr()
        assert rc == 1
        assert archive.read_text(encoding="utf-8") == before, (
            "the archive was rewritten by a run that then refused"
        )
        assert list(store.episodic_dir.glob("archive-pre-*")) == []
        assert json.loads(captured.out)["archive"]["written"] is False
        assert "archive untouched" in captured.err
        assert set(self._sqlite_scores(db).values()) == {0.0}

    def test_a_mid_write_mirror_failure_stops_and_names_what_it_wrote(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R2-004: BLK-001 inverted — the mirror ahead of a rolled-back SQLite.

        Pass 3 caught OSError per candidate and CONTINUED. One injected failure
        at candidate 50 of 210 left 209 candidate files carrying the new score
        against 210 SQLite rows rolled back to 0.0, while stderr said "nothing
        repaired". The review queue reads the mirror.

        Filesystem writes do not roll back, but they can be compensated: the
        pre-flight holds every candidate as it was read, so the files already
        written are put back and the mirror ends where SQLite ends.
        """
        conn, db, store = corrupted
        conn.close()
        real = MemlifeStore.save_candidate
        calls: list[str] = []

        def _flaky(self: MemlifeStore, candidate: Candidate) -> Path:
            calls.append(candidate.id)
            if len(calls) == 2:
                raise OSError("injected ENOSPC at the second candidate")
            return real(self, candidate)

        monkeypatch.setattr(MemlifeStore, "save_candidate", _flaky)
        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        captured = capsys.readouterr()
        assert rc == 1
        assert len(calls) == 3, (
            f"expected one write, one failure, one restore; got {calls}"
        )
        assert calls[2] == calls[0], "the restore must put back the file it wrote"
        report = json.loads(captured.out)
        assert report["mirror_written"] == 1
        assert report["mirror_restored"] == 1
        assert report["mirror_diverged_ids"] == []
        sql = self._sqlite_scores(db)
        assert set(sql.values()) == {0.0}
        for candidate_id, score in sql.items():
            path = store.candidates_dir / f"{candidate_id}.json"
            if path.is_file():
                assert json.loads(path.read_text())["salience"] == score, (
                    f"{candidate_id}: the mirror is ahead of the rolled-back database"
                )
        receipt = json.loads(
            next(store.root.parent.rglob("*backfill-receipt-*.json")).read_text()
        )
        assert receipt["mirror_written_ids"] == [calls[0]]

    def test_a_compensation_that_fails_names_every_file_left_ahead(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The irreducible case: name it, never round it down to "nothing repaired"."""
        conn, db, store = corrupted
        conn.close()
        real = MemlifeStore.save_candidate
        calls: list[str] = []

        def _flaky(self: MemlifeStore, candidate: Candidate) -> Path:
            calls.append(candidate.id)
            if len(calls) == 1:
                return real(self, candidate)
            raise OSError("the volume went away")

        monkeypatch.setattr(MemlifeStore, "save_candidate", _flaky)
        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        captured = capsys.readouterr()
        assert rc == 1
        report = json.loads(captured.out)
        assert report["mirror_diverged_ids"] == [calls[0]]
        assert any("reconcile by hand" in e for e in report["errors"])
        assert "LEFT AHEAD of the database" in captured.err

    def test_a_failure_after_the_archive_landed_says_the_archive_landed(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Some failures can only happen mid-write. Those must not lie either."""
        conn, db, store = corrupted
        conn.close()
        TestArchiveIsRederivedNotFrozen._archive(
            store, [TestArchiveIsRederivedNotFrozen._laundered("swa_r0")]
        )

        def _boom(self: MemlifeStore, candidate: object) -> None:
            raise OSError("injected ENOSPC")

        monkeypatch.setattr(MemlifeStore, "save_candidate", _boom)
        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        captured = capsys.readouterr()
        assert rc == 1
        report = json.loads(captured.out)
        assert report["archive"]["written"] is True
        assert "archive.jsonl WAS REWRITTEN" in captured.err
        assert report["archive"]["backup_path"] in captured.err, (
            "an operator told the archive changed must be told where the pre-image is"
        )
        assert set(self._sqlite_scores(db).values()) == {0.0}

    def test_the_dry_run_pre_flights_the_mirror_it_would_have_written(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """R2-001: a rehearsal that skips the pre-flight rehearses nothing.

        Measured on a copy of the live store: a dry run over a store with two
        candidate files deleted and the candidates directory chmod 500 returned
        ``{"examined": 210, "mirror_missing": 0, "errors": []}``, rc 0, empty
        stderr — a perfectly green rehearsal for a run that could not succeed.
        The pre-flight is the ONLY check whose failure produces the half-landed
        state this module exists to prevent, and it sat after the dry-run return.
        """
        conn, db, store = corrupted
        conn.close()
        (store.candidates_dir / "cand_one_off.json").unlink()
        store.candidates_dir.chmod(0o500)
        try:
            rc = main(["--db", str(db), "--store", str(store.root)])
        finally:
            store.candidates_dir.chmod(0o700)

        captured = capsys.readouterr()
        assert rc == 1, "a rehearsal for an apply that cannot land must not exit 0"
        report = json.loads(captured.out)
        assert report["dry_run"] is True
        assert report["mirror_missing"] == 1
        assert any("cand_one_off" in e for e in report["errors"])
        assert any("not writable" in e for e in report["errors"])
        assert "ERROR:" in captured.err

    def test_the_dry_run_still_writes_no_repair_while_pre_flighting(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The pre-flight's only footprint is a probe file it removes again."""
        conn, db, store = corrupted
        conn.close()
        before = {
            p.name: p.read_bytes() for p in sorted(store.candidates_dir.iterdir())
        }

        assert main(["--db", str(db), "--store", str(store.root)]) == 0

        assert {p.name: p.read_bytes() for p in sorted(store.candidates_dir.iterdir())} == (
            before
        ), "the dry run left something behind in the mirror"
        assert set(self._sqlite_scores(db).values()) == {0.0}

    def test_empty_database_is_refused_not_reported_as_a_repair(
        self, tmp_path: Path
    ) -> None:
        """"Nothing to do" and "nothing was there" are different facts."""
        db = tmp_path / "empty.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        assert main(["--db", str(db)]) == 2

    def test_a_missing_mirror_file_is_a_refusal_not_a_counter(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """A repaired row whose candidate file is absent is still a split brain."""
        conn, db, store = corrupted
        conn.close()
        (store.candidates_dir / "cand_one_off.json").unlink()

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        assert rc == 1
        assert set(self._sqlite_scores(db).values()) == {0.0}


class TestCorruptEvidenceIsNamedNotGuessedAt:
    """MAJ-002: ``except ValueError: evidence_ids = []`` made corruption invisible.

    A candidate whose ``evidence_ids_json`` will not parse and one whose
    evidence rows were legitimately pruned both landed in the same aggregate
    ``unresolvable`` counter, neither was named, both scores were overwritten
    with NULL, and the process exited 0 with ``errors: []`` — measured by the
    reviewer against a copy of the live store (2026-08-06T18:36Z corpus),
    having nulled the 205-member candidate:
    ``{"examined":210,"rescored":210,"unresolvable":3,"mirror_written":210,"errors":[]}``.

    ``dream.run_dream_cycle`` quarantines and COUNTS unparseable input one file
    over. Two distinct facts, two distinct outcomes:

    - pruned evidence → ``unresolvable``, score ``None`` (unknown and kept),
      named in ``unresolvable_ids``, the run still succeeds;
    - unreadable evidence → ``evidence_corrupt``, named in ``errors``, the
      candidate is left EXACTLY as it was, and the run fails.
    """

    @staticmethod
    def _corrupt(conn: sqlite3.Connection, candidate_id: str, raw: str) -> None:
        conn.execute(
            "UPDATE memlife_candidates SET evidence_ids_json=? WHERE id=?",
            (raw, candidate_id),
        )
        conn.commit()

    @pytest.mark.parametrize(
        "raw",
        ["{corrupt", '{"not": "a list"}', '[123]', '[""]'],
        ids=["unparseable", "not-a-list", "non-string-member", "empty-id"],
    )
    def test_unreadable_evidence_is_named_and_the_run_fails(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        raw: str,
    ) -> None:
        conn, db, store = corrupted
        self._corrupt(conn, "cand_recurring", raw)

        report = rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)

        assert report.evidence_corrupt == 1, report.as_dict()
        assert report.errors, "corruption must not produce an empty error list"
        assert any("cand_recurring" in e for e in report.errors), (
            f"the corrupt candidate must be named by id; got {report.errors}"
        )

    def test_a_corrupt_row_leaves_every_candidate_untouched(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """Unreadable input is a reason to stop, not a reason to write NULL."""
        conn, db, store = corrupted
        self._corrupt(conn, "cand_recurring", "{corrupt")
        before = dict(conn.execute("SELECT id, salience FROM memlife_candidates"))

        rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()

        after = dict(conn.execute("SELECT id, salience FROM memlife_candidates"))
        assert after == before, (
            "a repair that cannot read one candidate's evidence must write nothing"
        )
        assert store.load_candidate("cand_recurring").salience == 0.0

    def test_cli_exits_nonzero_and_says_which_candidate(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        conn, db, store = corrupted
        self._corrupt(conn, "cand_recurring", "{corrupt")
        conn.close()

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        captured = capsys.readouterr()
        assert rc == 1, "nulling a candidate from unreadable evidence must not exit 0"
        report = json.loads(captured.out)
        assert report["evidence_corrupt"] == 1
        assert report["committed"] is False
        assert "cand_recurring" in captured.err

    def test_pruned_evidence_is_distinguishable_from_corruption(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The whole point: the two facts must not share one counter."""
        conn, db, store = corrupted

        report = rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)

        assert report.unresolvable == 1
        assert report.unresolvable_ids == ["cand_orphan"], (
            "legitimately-pruned evidence must be named, not just counted"
        )
        assert report.evidence_corrupt == 0
        assert report.errors == [], "a pruned-evidence candidate is not an error"


class TestArchiveIsRederivedNotFrozen:
    """BLK-002: ``episodic/archive.jsonl`` is the fourth carrier, and it was frozen.

    Measured on the live store (2026-08-06T18:36Z): 910 lines / 904 distinct ids,
    every one of those 910 lines carrying ``pain: 0.0, importance: 0.0``.
    Because ``_usable_score``
    correctly treats a MEASURED 0.0 as measured, those re-score to exactly 0.0 —
    a confident LOWEST score, not ``None``. The unknown was laundered into the
    permanent episodic record.

    Nothing repaired it: ``dream._known_event_ids`` de-duplicates on id alone, so
    ``capture(since=None)`` returned ``rows=904 novel=0``. Comparing
    ``(id, result, pain, importance)`` instead would append the corrected line
    and leave the wrong one beside it. Re-derivation leaves the file correct.
    """

    @staticmethod
    def _archive(store: MemlifeStore, lines: list[dict[str, object]]) -> Path:
        store.episodic_dir.mkdir(parents=True, exist_ok=True)
        path = store.episodic_dir / "archive.jsonl"
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        return path

    @staticmethod
    def _laundered(event_id: str, result: str = "unknown") -> dict[str, object]:
        """A line exactly as the defect wrote it: measured-looking zeros."""
        return {
            "id": event_id,
            "ts": "2026-07-28T11:00:00Z",
            "skill": "swarm.codex",
            "action": "attempt",
            "result": result,
            "pain": 0.0,
            "importance": 0.0,
            "reflection": "detail",
            "model": "m",
            "cost_usd": None,
            "provenance": {
                "run_id": "swr",
                "attempt_id": event_id,
                "session_id": None,
                "commit_sha": None,
                "source": "swarm_attempt",
            },
        }

    def test_laundered_zeros_are_rewritten_from_the_attempt_rows(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0"), self._laundered("swa_solo")])

        report = rederive_archive(store, db, dry_run=False)

        assert report.errors == []
        assert report.repaired == 2
        assert report.score_changed == 2
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert all(r["pain"] != 0.0 for r in rows), (
            f"a measured-looking zero survived the repair: {rows}"
        )
        assert all(r["importance"] != 0.0 for r in rows)

    def test_a_stale_result_is_corrected(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """15 live lines said ``unknown`` for an attempt that has since landed."""
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_solo", result="unknown")])

        report = rederive_archive(store, db, dry_run=False)

        assert report.result_changed == 1
        (row,) = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert row["result"] == "success", "swa_solo is end_reason='completed' today"

    def test_duplicate_ids_collapse_to_the_first_occurrence(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """4 live ids appeared twice — 910 lines for 904 distinct events."""
        conn, db, store = corrupted
        conn.close()
        path = self._archive(
            store, [self._laundered("swa_r0"), self._laundered("swa_r1"), self._laundered("swa_r0")]
        )

        report = rederive_archive(store, db, dry_run=False)

        assert report.lines_in == 3
        assert report.lines_out == 2
        assert report.duplicates_dropped == 1
        ids = [json.loads(x)["id"] for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert ids == ["swa_r0", "swa_r1"], "input order must be preserved"

    def test_an_unresolved_duplicate_keeps_the_terminal_copy_not_the_in_flight_one(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """R2-006: for a line we keep VERBATIM, which copy survives is the answer.

        Two of the four duplicated ids in the live archive are an in-flight
        capture (``result: unknown``) followed by the terminal capture — the
        round-1 defect, visible in the data. "Keep the first occurrence" kept
        the in-flight lie. It is benign today only because both ids still
        resolve to an attempt row and are re-derived; for an id whose evidence
        has been pruned, the kept line IS the permanent record.
        """
        conn, db, store = corrupted
        conn.close()
        in_flight = self._laundered("swa_pruned", result="unknown")
        in_flight["action"] = "attempt"
        terminal = self._laundered("swa_pruned", result="success")
        terminal["action"] = "completed"
        path = self._archive(store, [in_flight, self._laundered("swa_r0"), terminal])

        report = rederive_archive(store, db, dry_run=False)

        assert report.duplicates_dropped == 1
        assert report.unresolved_duplicates_repicked == 1
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert [r["id"] for r in rows] == ["swa_pruned", "swa_r0"], (
            "the survivor must stay in the position of the first occurrence"
        )
        assert rows[0] == terminal, (
            "the in-flight capture survived over the terminal one: "
            f"{rows[0]['action']}/{rows[0]['result']}"
        )

    def test_an_unknown_duplicate_never_displaces_a_recorded_outcome(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """"Keep the last" alone would re-bury a known result. Same defect, mirrored."""
        conn, db, store = corrupted
        conn.close()
        terminal = self._laundered("swa_pruned", result="failure")
        stray = self._laundered("swa_pruned", result="unknown")
        path = self._archive(store, [terminal, stray])

        report = rederive_archive(store, db, dry_run=False)

        assert report.duplicates_dropped == 1
        assert report.unresolved_duplicates_repicked == 0
        (row,) = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert row["result"] == "failure"

    def test_a_resolvable_duplicate_is_unaffected_by_the_rule(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The survivor is overwritten from the attempt row, so the choice cannot show."""
        conn, db, store = corrupted
        conn.close()
        path = self._archive(
            store,
            [
                self._laundered("swa_solo", result="unknown"),
                self._laundered("swa_solo", result="denied"),
            ],
        )

        report = rederive_archive(store, db, dry_run=False)

        assert report.unresolved_duplicates_repicked == 0
        (row,) = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert row["result"] == "success", "swa_solo is end_reason='completed' today"

    def test_an_event_with_no_attempt_row_is_kept_verbatim(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """We cannot re-derive it, so we do not delete it. Never invent, never drop."""
        conn, db, store = corrupted
        conn.close()
        orphan = self._laundered("swa_pruned")
        path = self._archive(store, [orphan])

        report = rederive_archive(store, db, dry_run=False)

        assert report.unresolved_kept == 1
        assert report.repaired == 0
        (row,) = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        assert row == orphan

    def test_an_unparseable_line_is_kept_and_counted(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The dream cycle's discipline: nothing is silently dropped."""
        conn, db, store = corrupted
        conn.close()
        store.episodic_dir.mkdir(parents=True, exist_ok=True)
        path = store.episodic_dir / "archive.jsonl"
        path.write_text('{"broken\n' + json.dumps(self._laundered("swa_r0")) + "\n")

        report = rederive_archive(store, db, dry_run=False)

        assert report.unparseable_kept == 1
        assert report.lines_out == 2
        assert '{"broken' in path.read_text(encoding="utf-8")

    def test_every_distinct_id_survives(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """Conservation: repair may collapse duplicates, never lose an event."""
        conn, db, store = corrupted
        conn.close()
        ids = ["swa_r0", "swa_r1", "swa_r2", "swa_r3", "swa_solo", "swa_pruned", "swa_r0"]
        path = self._archive(store, [self._laundered(i) for i in ids])

        rederive_archive(store, db, dry_run=False)

        after = {json.loads(x)["id"] for x in path.read_text(encoding="utf-8").splitlines() if x}
        assert after == set(ids)

    def test_the_pre_image_is_kept_before_an_irreversible_rewrite(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0")])
        original = path.read_text(encoding="utf-8")

        report = rederive_archive(store, db, dry_run=False)

        assert report.backup_path is not None
        assert Path(report.backup_path).read_text(encoding="utf-8") == original
        assert path.read_text(encoding="utf-8") != original

    def test_the_pre_image_is_written_durably_not_buffered(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R2-005: the one file whose loss is unrecoverable must be fsync'd.

        The replacement archive goes through tmp + fsync + rename. The pre-image
        used to be a plain buffered ``write_text``, which returns before the
        bytes reach the disk — so a crash in that window leaves the replacement
        durable and the only copy of the original possibly truncated.
        """
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0")])

        import omniagentos.memlife.backfill as backfill_module

        durable: list[str] = []
        real = backfill_module.atomic_write_text

        def _record(target: Path, content: str) -> None:
            durable.append(str(target))
            real(target, content)

        monkeypatch.setattr(backfill_module, "atomic_write_text", _record)
        report = rederive_archive(store, db, dry_run=False)

        assert report.backup_path in durable, (
            "the pre-image was not written through the durable writer: " f"{durable}"
        )
        assert str(path) in durable

    def test_a_pre_image_that_cannot_be_written_stops_the_rewrite(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No copy of the original, no irreversible rewrite. Named, not swallowed."""
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0")])
        original = path.read_text(encoding="utf-8")

        import omniagentos.memlife.backfill as backfill_module

        def _boom(target: Path, content: str) -> None:
            raise OSError("injected pre-image failure")

        monkeypatch.setattr(backfill_module, "atomic_write_text", _boom)
        report = rederive_archive(store, db, dry_run=False)

        assert report.errors, "a failed pre-image must be named"
        assert path.read_text(encoding="utf-8") == original, (
            "the archive was rewritten without a surviving pre-image"
        )

    def test_a_second_run_does_not_overwrite_the_first_pre_image(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The stamp is second-resolution, and second-resolution stamps collide.

        Two repairs inside one second would otherwise write the second run's
        pre-image (already repaired) over the first run's — destroying the only
        copy of the original while appearing to have made one.
        """
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0")])
        original = path.read_text(encoding="utf-8")
        fixed = datetime(2026, 8, 6, 18, 36, tzinfo=UTC)

        first = rederive_archive(store, db, dry_run=False, now=fixed)
        second = rederive_archive(store, db, dry_run=False, now=fixed)

        assert first.backup_path != second.backup_path, "same-second stamp collided"
        assert Path(first.backup_path or "").read_text(encoding="utf-8") == original, (
            "the original pre-image was overwritten by a later run"
        )
        assert second.repaired == 0, "the second pass had nothing left to repair"

    def test_dry_run_reports_everything_and_writes_nothing(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0"), self._laundered("swa_r0")])
        original = path.read_text(encoding="utf-8")

        report = rederive_archive(store, db, dry_run=True)

        assert report.repaired == 1
        assert report.duplicates_dropped == 1
        assert report.backup_path is None
        assert path.read_text(encoding="utf-8") == original

    def test_the_repaired_archive_no_longer_scores_to_zero(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The decisive end: laundered zeros scored 0.0, not None. Both are wrong."""
        from omniagentos.memlife.contracts import EpisodicEvent
        from omniagentos.memlife.salience import salience_score

        conn, db, store = corrupted
        conn.close()
        path = self._archive(store, [self._laundered("swa_r0"), self._laundered("swa_solo")])
        before = [
            salience_score(EpisodicEvent.model_validate_json(x), NOW, recurrence=3)
            for x in path.read_text(encoding="utf-8").splitlines()
            if x
        ]
        assert before == [0.0, 0.0], f"precondition: laundered lines score 0.0; got {before}"

        rederive_archive(store, db, dry_run=False)

        after = [
            salience_score(EpisodicEvent.model_validate_json(x), NOW, recurrence=3)
            for x in path.read_text(encoding="utf-8").splitlines()
            if x
        ]
        assert all(s is not None and s > 0.0 for s in after), after

    def test_the_cli_repairs_the_archive_alongside_the_candidates(
        self,
        corrupted: tuple[sqlite3.Connection, Path, MemlifeStore],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        conn, db, store = corrupted
        conn.close()
        self._archive(store, [self._laundered("swa_r0"), self._laundered("swa_r0")])

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["archive"]["duplicates_dropped"] == 1
        assert report["archive"]["repaired"] == 1
        assert report["archive"]["dry_run"] is False


class TestTheReadingTreeMustBeAbleToParseWhatWeWrite:
    """R2-000: mutating production ahead of the code that can read the mutation.

    ``EventResult.MOVED`` exists only on this branch. The live API is
    ``uvicorn omniagentos.api:app`` running from the SERVING checkout, whose
    enum is ``['success','failure','denied','unknown']``. A repaired line
    carrying ``"result":"moved"`` fails ``EpisodicEvent.model_validate_json``,
    and ``scheduler/builtin_jobs`` swallows the rejection in a bare
    ``except Exception: continue`` — so ~30 repaired records become **silently
    invisible** to the running system. No error, no counter, no receipt: a
    favourable absence manufactured by the repair itself.

    "Merge, restart, then repair" is the right order, and an order an operator
    has to remember is not a control. The gate makes it mechanical.
    """

    _SERVING_ENUM = '''
class EventResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    UNKNOWN = "unknown"
'''

    @staticmethod
    def _reader_tree(root: Path, source: str) -> Path:
        """A checkout shaped like the one that serves a store, with a given enum."""
        contracts = root / "omniagentos" / "memlife" / "contracts.py"
        contracts.parent.mkdir(parents=True, exist_ok=True)
        contracts.write_text(f"from enum import StrEnum\n{source}", encoding="utf-8")
        return contracts

    @staticmethod
    def _store_under(root: Path, store: MemlifeStore) -> MemlifeStore:
        """Move a store to ``<root>/var/memories/memlife``, the live shape."""
        moved = root / "var" / "memories" / "memlife"
        moved.parent.mkdir(parents=True, exist_ok=True)
        store.root.rename(moved)
        return MemlifeStore(moved)

    @pytest.fixture()
    def moving(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> tuple[Path, MemlifeStore]:
        """An archive line whose attempt row now says the work MOVED."""
        conn, db, store = corrupted
        conn.execute("UPDATE swarm_attempts SET end_reason='split' WHERE id='swa_solo'")
        conn.commit()
        conn.close()
        TestArchiveIsRederivedNotFrozen._archive(
            store, [TestArchiveIsRederivedNotFrozen._laundered("swa_solo")]
        )
        return db, store

    def test_a_result_the_reader_cannot_parse_is_refused_before_any_write(
        self, moving: tuple[Path, MemlifeStore], tmp_path: Path
    ) -> None:
        db, store = moving
        served = self._store_under(tmp_path / "serving", store)
        self._reader_tree(tmp_path / "serving", self._SERVING_ENUM)
        archive = served.episodic_dir / "archive.jsonl"
        before = archive.read_text(encoding="utf-8")

        report = rederive_archive(served, db, dry_run=False)

        assert report.errors, "a repair the reader cannot read must refuse"
        assert "moved" in report.errors[0]
        assert "success" in report.errors[0], "the refusal must name what IS readable"
        assert archive.read_text(encoding="utf-8") == before, "refused, but wrote anyway"
        assert report.backup_path is None
        assert list(served.episodic_dir.glob("archive-pre-*")) == []

    def test_the_cli_refuses_and_touches_no_other_carrier(
        self,
        moving: tuple[Path, MemlifeStore],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        db, store = moving
        served = self._store_under(tmp_path / "serving", store)
        self._reader_tree(tmp_path / "serving", self._SERVING_ENUM)

        rc = main(["--db", str(db), "--store", str(served.root), "--apply"])

        assert rc == 1
        assert "REFUSED" in capsys.readouterr().err
        conn = sqlite3.connect(db)
        try:
            values = {r[0] for r in conn.execute("SELECT salience FROM memlife_candidates")}
        finally:
            conn.close()
        assert values == {0.0}, "the candidate carriers were touched by a refused run"

    def test_the_dry_run_predicts_the_refusal(
        self, moving: tuple[Path, MemlifeStore], tmp_path: Path
    ) -> None:
        """A rehearsal that passes for an apply that refuses is not a rehearsal."""
        db, store = moving
        served = self._store_under(tmp_path / "serving", store)
        self._reader_tree(tmp_path / "serving", self._SERVING_ENUM)

        assert rederive_archive(served, db, dry_run=True).errors

    def test_the_gate_opens_once_the_reading_tree_carries_the_member(
        self, moving: tuple[Path, MemlifeStore], tmp_path: Path
    ) -> None:
        """The merge is the remedy, and the gate must actually clear after it."""
        db, store = moving
        served = self._store_under(tmp_path / "serving", store)
        self._reader_tree(
            tmp_path / "serving",
            self._SERVING_ENUM.replace('    UNKNOWN', '    MOVED = "moved"\n    UNKNOWN'),
        )
        archive = served.episodic_dir / "archive.jsonl"

        report = rederive_archive(served, db, dry_run=False)

        assert report.errors == []
        assert "moved" in report.reader_results
        (row,) = [json.loads(x) for x in archive.read_text(encoding="utf-8").splitlines() if x]
        assert row["result"] == "moved"

    def test_the_report_names_the_tree_it_checked_and_what_it_can_read(
        self, moving: tuple[Path, MemlifeStore], tmp_path: Path
    ) -> None:
        """"We verified the reader" is evidence only if it says WHICH reader."""
        db, store = moving
        served = self._store_under(tmp_path / "serving", store)
        contracts = self._reader_tree(tmp_path / "serving", self._SERVING_ENUM)

        report = rederive_archive(served, db, dry_run=True)

        assert report.reader_contracts_path == str(contracts)
        assert report.reader_results == ["denied", "failure", "success", "unknown"]

    def test_a_vocabulary_that_cannot_be_read_is_an_error_not_a_pass(
        self, moving: tuple[Path, MemlifeStore], tmp_path: Path
    ) -> None:
        """Not knowing what the reader accepts is not permission to write."""
        db, store = moving
        served = self._store_under(tmp_path / "serving", store)
        self._reader_tree(tmp_path / "serving", "class EventResult(StrEnum:\n")
        archive = served.episodic_dir / "archive.jsonl"
        before = archive.read_text(encoding="utf-8")

        report = rederive_archive(served, db, dry_run=False)

        assert report.errors, "an undeterminable reader vocabulary must not pass"
        assert archive.read_text(encoding="utf-8") == before

    def test_a_store_outside_any_checkout_falls_back_to_the_running_tree(
        self, moving: tuple[Path, MemlifeStore]
    ) -> None:
        """A scratch copy has no serving checkout; the running tree is consistent."""
        import omniagentos.memlife.contracts as contracts_module

        db, store = moving
        report = rederive_archive(store, db, dry_run=True)

        assert report.errors == []
        assert report.reader_contracts_path == str(Path(contracts_module.__file__).resolve())
        assert "moved" in report.reader_results


class TestApplyLeavesAReceipt:
    """This tool mutates production data once, by hand.

    Its only record was stdout, and stdout from a one-shot operator command is
    not evidence: not addressable, not timestamped, gone with the scrollback.
    "Did the repair land, and land completely" has to be answerable later
    without re-deriving it.
    """

    @staticmethod
    def _receipts(root: Path) -> list[Path]:
        """Every receipt anywhere under ``root`` — the default location is a policy."""
        return sorted(root.rglob("*backfill-receipt-*.json"))

    def test_the_default_receipt_lands_outside_every_memory_root(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore], tmp_path: Path
    ) -> None:
        """R2-007: the live store is ``var/memories/memlife``.

        The old default put receipts in the store's parent — ``var/memories/``,
        which is the ``omniagentos-agent-memories`` source enrolled in the ``wis``
        knowledge catalog. Every receipt would have been indexed as a curated
        note. They belong under ``var/``, outside the enrolled tree.
        """
        conn, db, store = corrupted
        conn.close()
        checkout = tmp_path / "serving"
        TestTheReadingTreeMustBeAbleToParseWhatWeWrite._reader_tree(
            checkout, TestTheReadingTreeMustBeAbleToParseWhatWeWrite._SERVING_ENUM
        )
        live_shape = checkout / "var" / "memories" / "memlife"
        live_shape.parent.mkdir(parents=True)
        store.root.rename(live_shape)

        rc = main(["--db", str(db), "--store", str(live_shape), "--apply"])

        assert rc == 0
        assert self._receipts(live_shape.parent) == [], (
            "a receipt landed inside the enrolled memory root var/memories/"
        )
        (receipt,) = self._receipts(checkout / "var")
        assert receipt.parent == checkout / "var" / "backfill-receipts"

    def test_a_store_outside_a_checkout_gets_a_named_directory_beside_it(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore], tmp_path: Path
    ) -> None:
        """Never escape upward looking for a ``var``: on macOS that finds ``/var``."""
        conn, db, store = corrupted
        conn.close()

        assert main(["--db", str(db), "--store", str(store.root), "--apply"]) == 0

        (receipt,) = self._receipts(tmp_path)
        assert receipt.parent == store.root.resolve().parent / "backfill-receipts"
        assert receipt.parent != store.root, "a receipt is not a memlife artifact"
        assert tmp_path in receipt.parents, (
            f"the receipt escaped the store's own tree: {receipt}"
        )

    def test_apply_writes_a_receipt_with_per_candidate_before_and_after(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore], tmp_path: Path
    ) -> None:
        conn, db, store = corrupted
        conn.close()

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        assert rc == 0
        (receipt_path,) = self._receipts(store.root.parent)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["exit_code"] == 0
        assert receipt["committed"] is True
        assert receipt["db_path"] == str(db.resolve())
        assert receipt["store_path"] == str(store.root.resolve())
        assert receipt["at"], "a receipt with no timestamp answers nothing later"
        assert receipt["archive"] is not None
        by_id = {c["id"]: c for c in receipt["changes"]}
        assert set(by_id) == {"cand_recurring", "cand_one_off", "cand_orphan"}
        assert by_id["cand_recurring"]["before"] == 0.0
        assert by_id["cand_recurring"]["after"] > 0.0
        assert by_id["cand_orphan"]["after"] is None, (
            "unknown must read as unknown in the receipt too, never 0.0"
        )

    def test_the_receipt_records_a_failed_run_as_failed(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """The failure case is the one where the receipt matters most."""
        conn, db, store = corrupted
        conn.execute(
            "UPDATE memlife_candidates SET evidence_ids_json='{corrupt' "
            "WHERE id='cand_recurring'"
        )
        conn.commit()
        conn.close()

        rc = main(["--db", str(db), "--store", str(store.root), "--apply"])

        assert rc == 1
        (receipt_path,) = self._receipts(store.root.parent)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["exit_code"] == 1
        assert receipt["committed"] is False
        assert receipt["summary"]["errors"], "a failed run's receipt must say why"

    def test_a_second_run_does_not_overwrite_the_first_receipt(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """Same second-resolution-stamp hazard as the archive pre-image."""
        conn, db, store = corrupted
        conn.close()
        assert main(["--db", str(db), "--store", str(store.root), "--apply"]) == 0
        assert main(["--db", str(db), "--store", str(store.root), "--apply"]) == 0

        receipts = self._receipts(store.root.parent)
        assert len(receipts) == 2, f"a run overwrote an earlier receipt: {receipts}"
        loaded = [json.loads(p.read_text(encoding="utf-8")) for p in receipts]
        befores = sorted(r["summary"]["before_distinct"] for r in loaded)
        assert befores[0] == 1, (
            "one receipt must still describe the FIRST run, which saw the "
            f"degenerate single-value backlog; got {befores}"
        )
        assert befores[1] > 1, f"the second run saw the repaired backlog; got {befores}"

    def test_a_dry_run_leaves_no_receipt(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        """A receipt is a record of a mutation. A dry run mutates nothing."""
        conn, db, store = corrupted
        conn.close()
        assert main(["--db", str(db), "--store", str(store.root)]) == 0
        assert self._receipts(store.root.parent) == []

    def test_an_unwritable_receipt_destination_refuses_before_mutating(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore], tmp_path: Path
    ) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        conn, db, store = corrupted
        conn.close()
        try:
            rc = main(
                [
                    "--db",
                    str(db),
                    "--store",
                    str(store.root),
                    "--apply",
                    "--receipt",
                    str(locked / "r.json"),
                ]
            )
        finally:
            locked.chmod(0o700)

        assert rc == 2, "a repair whose only record cannot be written must refuse"
        conn2 = sqlite3.connect(db)
        try:
            values = {
                r[0]
                for r in conn2.execute(
                    "SELECT salience FROM memlife_candidates WHERE status='staged'"
                )
            }
        finally:
            conn2.close()
        assert values == {0.0}, "nothing may be mutated before the receipt is provable"


class TestDryRunAndIdempotence:
    def test_dry_run_writes_nothing_but_reports_everything(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        report = rescore_candidates(conn, db, store=store, now=NOW, dry_run=True)
        conn.commit()

        assert report.examined == 3
        assert report.after_distinct > 1, "the dry run must still compute the outcome"
        untouched = {
            r[0]
            for r in conn.execute(
                "SELECT salience FROM memlife_candidates WHERE status='staged'"
            )
        }
        assert untouched == {0.0}, "dry run must not write"
        assert store.load_candidate("cand_recurring").salience == 0.0

    def test_second_pass_is_idempotent_at_a_fixed_clock(
        self, corrupted: tuple[sqlite3.Connection, Path, MemlifeStore]
    ) -> None:
        conn, db, store = corrupted
        rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()
        first = dict(conn.execute("SELECT id, salience FROM memlife_candidates"))

        second = rescore_candidates(conn, db, store=store, now=NOW, dry_run=False)
        conn.commit()
        again = dict(conn.execute("SELECT id, salience FROM memlife_candidates"))

        assert first == again
        assert second.rescored == 0, "a repeated repair must be a no-op"
        assert second.unchanged == second.examined
