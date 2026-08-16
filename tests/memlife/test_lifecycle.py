"""L3 acceptance: lifecycle state machine + store — all-or-nothing graduation.

Decisive assertion
------------------
``graduate(candidate_id, actor)`` writes **three** artifacts in one call:
  1. the lesson under ``lessons/``
  2. the decision entry on the candidate (append-only history)
  3. the refreshed queue snapshot

Assert on all three — not just a truthy return value. Upstream
``mark_graduated`` reports success without writing the lesson and swallows
queue-refresh errors with ``except Exception: pass``.

Counterfeit that must fail
--------------------------
Inject a writer that raises during the queue refresh: the transition must
FAIL and leave **no lesson** behind. A version that swallows the exception
and reports success must fail this suite (deliberately written and confirmed).

Also: a missing store directory is an ERROR, never an empty queue.

Revert-check
------------
Wrap the queue-refresh call in ``except Exception: pass`` — the decisive
three-artifact / rollback tests must fail under that mutation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    Decision,
    DecisionAction,
    Lesson,
    LessonStatus,
)
from omniagentos.memlife.lifecycle import (
    IllegalTransition,
    Lifecycle,
)
from omniagentos.memlife.store import (
    MemlifeStore,
    StoreUnavailableError,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _stage_decision(actor: str = "dream-cycle") -> Decision:
    return Decision(action=DecisionAction.STAGE, at=NOW, actor=actor, reason="clustered")


def _staged_candidate(cid: str = "cand_1") -> Candidate:
    return Candidate(
        id=cid,
        key="swarm.coder/commit-refused",
        claim="Agents cannot commit inside a sandboxed worktree.",
        conditions="when worktree is git-excluded",
        evidence_ids=["ev_1", "ev_2"],
        cluster_size=3,
        status=CandidateStatus.STAGED,
        decisions=[_stage_decision()],
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "var" / "memories" / "demo-project"
    root.mkdir(parents=True)
    (root / "episodic").mkdir()
    (root / "candidates").mkdir()
    (root / "lessons").mkdir()
    (root / "quarantine").mkdir()
    return root


@pytest.fixture
def store(project_root: Path) -> MemlifeStore:
    return MemlifeStore(project_root)


@pytest.fixture
def life(store: MemlifeStore) -> Lifecycle:
    return Lifecycle(store)


# ---------------------------------------------------------------------------
# Decisive: three artifacts on graduate
# ---------------------------------------------------------------------------


class TestGraduateWritesAllThreeArtifacts:
    """THE decisive property for L3."""

    def test_graduate_writes_lesson_decision_and_queue(
        self, store: MemlifeStore, life: Lifecycle
    ) -> None:
        cand = _staged_candidate()
        store.save_candidate(cand)
        store.refresh_queue()

        # Precondition: staged appears in queue, no lessons yet
        queue_before = store.load_queue()
        assert cand.id in queue_before["pending"]
        assert store.list_lessons() == []

        lesson = life.graduate(cand.id, actor="owner", reason="confirmed in production")

        # --- artifact 1: lesson on disk ---
        lessons = store.list_lessons()
        assert len(lessons) == 1
        on_disk = store.load_lesson(lesson.id)
        assert on_disk.candidate_id == cand.id
        assert on_disk.claim == cand.claim
        assert on_disk.status is LessonStatus.ACCEPTED
        assert on_disk.graduated_by == "owner"
        assert on_disk.evidence_ids == cand.evidence_ids

        # --- artifact 2: decision appended (never rewritten) ---
        updated = store.load_candidate(cand.id)
        assert updated.status is CandidateStatus.GRADUATED
        assert len(updated.decisions) == 2  # stage + graduate
        assert updated.decisions[0].action is DecisionAction.STAGE
        grad_decision = updated.decisions[-1]
        assert grad_decision.action is DecisionAction.GRADUATE
        assert grad_decision.actor == "owner"
        assert grad_decision.reason == "confirmed in production"
        # original stage decision still present byte-for-byte as first entry
        assert updated.decisions[0] == cand.decisions[0]

        # --- artifact 3: queue refreshed (graduated candidate removed) ---
        queue_after = store.load_queue()
        assert cand.id not in queue_after["pending"]
        assert queue_after["count"] == 0

        # Return value alone is not enough — all three must exist
        assert isinstance(lesson, Lesson)
        assert (store.lessons_dir / f"{lesson.id}.json").is_file()
        assert (store.candidates_dir / f"{cand.id}.json").is_file()
        assert store.queue_path.is_file()


# ---------------------------------------------------------------------------
# Counterfeit: queue refresh failure must abort and leave no lesson
# ---------------------------------------------------------------------------


class TestQueueRefreshFailureIsNotSwallowed:
    """Named counterfeit: upstream wraps queue refresh in ``except Exception: pass``."""

    def test_queue_refresh_raise_fails_transition_and_leaves_no_lesson(
        self, store: MemlifeStore, life: Lifecycle
    ) -> None:
        cand = _staged_candidate()
        store.save_candidate(cand)
        store.refresh_queue()

        def boom() -> list[str]:
            raise RuntimeError("injected queue refresh failure")

        store.refresh_queue = boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="injected queue refresh failure"):
            life.graduate(cand.id, actor="owner")

        # No lesson may remain — partial success is failure
        assert store.list_lessons() == []
        assert list(store.lessons_dir.glob("*.json")) == []

        # Candidate must not be left in GRADUATED state without a lesson
        still = store.load_candidate(cand.id)
        assert still.status is CandidateStatus.STAGED
        assert still.decisions == cand.decisions  # history not advanced
        assert all(d.action is not DecisionAction.GRADUATE for d in still.decisions)

    def test_swallowing_counterfeit_would_hide_the_bug(
        self, store: MemlifeStore, project_root: Path
    ) -> None:
        """Deliberate upstream-shaped counterfeit: swallow refresh errors, report success.

        This function is the named fake. The *suite* must reject implementations
        that behave like it — demonstrated by showing that after this counterfeit
        runs, the three artifacts are NOT all present (lesson may exist without a
        refreshed queue), which is exactly what the decisive test asserts against.
        """
        cand = _staged_candidate()
        store.save_candidate(cand)
        store.refresh_queue()
        queue_before = store.load_queue()

        def counterfeit_graduate(candidate_id: str, actor: str, reason: str = "") -> Lesson:
            """Mirrors review_state.mark_graduated: success without reliable side effects."""
            c = store.load_candidate(candidate_id)
            decision = Decision(
                action=DecisionAction.GRADUATE,
                at=datetime.now(tz=UTC),
                actor=actor,
                reason=reason,
            )
            updated = c.model_copy(
                update={
                    "status": CandidateStatus.GRADUATED,
                    "decisions": list(c.decisions) + [decision],
                }
            )
            lesson = Lesson(
                id=f"les_{c.id}",
                candidate_id=c.id,
                claim=c.claim,
                conditions=c.conditions,
                status=LessonStatus.ACCEPTED,
                graduated_at=decision.at,
                graduated_by=actor,
                evidence_ids=list(c.evidence_ids),
            )
            # Write lesson + decision...
            store.save_lesson(lesson)
            store.save_candidate(updated)
            # ...then swallow queue refresh — THE upstream defect
            try:
                raise RuntimeError("queue refresh failed")
            except Exception:
                pass
            return lesson  # reports success

        lesson = counterfeit_graduate(cand.id, actor="owner")

        # Counterfeit reports success
        assert lesson is not None
        # But the queue was NOT refreshed — graduated id still pending
        queue_after = store.load_queue()
        assert cand.id in queue_after["pending"]
        assert queue_after == queue_before
        # And a lesson *was* written without a consistent queue — partial success
        assert store.load_lesson(lesson.id).candidate_id == cand.id

        # The real Lifecycle.graduate must not behave this way: re-seed and prove
        # that the real path refuses the same injected failure.
        store2 = MemlifeStore(project_root)
        # Clean lessons from counterfeit run so we can re-test cleanly
        for p in store2.lessons_dir.glob("*.json"):
            p.unlink()
        cand2 = _staged_candidate("cand_2")
        store2.save_candidate(cand2)
        store2.refresh_queue()

        def boom() -> list[str]:
            raise RuntimeError("injected queue refresh failure")

        store2.refresh_queue = boom  # type: ignore[method-assign]
        real = Lifecycle(store2)
        with pytest.raises(RuntimeError):
            real.graduate(cand2.id, actor="owner")
        assert store2.list_lessons() == []


# ---------------------------------------------------------------------------
# Missing store directory is an ERROR, never empty queue
# ---------------------------------------------------------------------------


class TestMissingStoreIsError:
    def test_missing_root_is_error_not_empty_queue(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert not missing.exists()
        store = MemlifeStore(missing)

        with pytest.raises(StoreUnavailableError):
            store.load_queue()

        with pytest.raises(StoreUnavailableError):
            store.list_pending()

        with pytest.raises(StoreUnavailableError):
            store.list_candidates()

    def test_empty_but_present_store_is_zero_pending_not_error(
        self, store: MemlifeStore
    ) -> None:
        """Distinguish absent from empty — the whole point of the error class."""
        store.refresh_queue()
        queue = store.load_queue()
        assert queue["pending"] == []
        assert queue["count"] == 0
        assert store.list_pending() == []


# ---------------------------------------------------------------------------
# Transition legality + append-only decisions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_stage_persist_and_queue(self, store: MemlifeStore, life: Lifecycle) -> None:
        cand = life.stage(
            id="cand_s",
            key="k",
            claim="Prefer clones over worktrees for agent lanes.",
            cluster_size=2,
            actor="dream-cycle",
            evidence_ids=["ev_a"],
        )
        assert cand.status is CandidateStatus.STAGED
        assert cand.decisions[-1].action is DecisionAction.STAGE
        loaded = store.load_candidate("cand_s")
        assert loaded.claim == cand.claim
        assert "cand_s" in store.load_queue()["pending"]

    def test_reject_then_reopen(self, store: MemlifeStore, life: Lifecycle) -> None:
        life.stage(
            id="cand_r",
            key="k",
            claim="Do not swallow queue errors.",
            cluster_size=1,
            actor="dream",
        )
        rejected = life.reject("cand_r", actor="owner", reason="duplicate")
        assert rejected.status is CandidateStatus.REJECTED
        assert rejected.rejection_count == 1
        assert rejected.decisions[-1].action is DecisionAction.REJECT
        assert "cand_r" not in store.load_queue()["pending"]

        reopened = life.reopen("cand_r", actor="owner", reason="reconsider")
        assert reopened.status is CandidateStatus.REOPENED
        assert reopened.decisions[-1].action is DecisionAction.REOPEN
        assert "cand_r" in store.load_queue()["pending"]

        # history is append-only: stage, reject, reopen
        assert [d.action for d in reopened.decisions] == [
            DecisionAction.STAGE,
            DecisionAction.REJECT,
            DecisionAction.REOPEN,
        ]

    def test_reopened_can_graduate(
        self, store: MemlifeStore, life: Lifecycle
    ) -> None:
        life.stage(
            id="cand_g",
            key="k",
            claim="All three artifacts or none.",
            cluster_size=1,
            actor="dream",
        )
        life.reject("cand_g", actor="owner")
        life.reopen("cand_g", actor="owner")
        lesson = life.graduate("cand_g", actor="owner")
        assert lesson.candidate_id == "cand_g"
        assert store.load_candidate("cand_g").status is CandidateStatus.GRADUATED

    def test_illegal_graduate_from_rejected(self, life: Lifecycle) -> None:
        life.stage(
            id="cand_x",
            key="k",
            claim="Cannot graduate a rejected claim without reopening.",
            cluster_size=1,
            actor="dream",
        )
        life.reject("cand_x", actor="owner")
        with pytest.raises(IllegalTransition):
            life.graduate("cand_x", actor="owner")

    def test_illegal_reopen_from_staged(self, life: Lifecycle) -> None:
        life.stage(
            id="cand_y",
            key="k",
            claim="Only rejected candidates reopen.",
            cluster_size=1,
            actor="dream",
        )
        with pytest.raises(IllegalTransition):
            life.reopen("cand_y", actor="owner")

    def test_quarantine_removes_from_queue_and_retains(
        self, store: MemlifeStore, life: Lifecycle
    ) -> None:
        life.stage(
            id="cand_q",
            key="k",
            claim="Unparseable-adjacent claim held in quarantine.",
            cluster_size=1,
            actor="dream",
        )
        q = life.quarantine("cand_q", actor="system", reason="suspect")
        assert q.status is CandidateStatus.QUARANTINED
        assert q.decisions[-1].action is DecisionAction.QUARANTINE
        assert "cand_q" not in store.load_queue()["pending"]
        # retained on disk (quarantine dir), never silently dropped
        assert store.load_quarantined("cand_q").id == "cand_q"


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


class TestAtomicWrites:
    def test_save_uses_tmp_then_rename(self, store: MemlifeStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """Writes go through tmp + fsync + rename; no bare open/write of the target."""
        calls: list[str] = []
        real_replace = __import__("os").replace

        def tracking_replace(src: object, dst: object) -> None:
            calls.append(str(dst))
            return real_replace(src, dst)

        monkeypatch.setattr("os.replace", tracking_replace)
        cand = _staged_candidate("cand_atom")
        store.save_candidate(cand)
        assert any(cand.id in c for c in calls)
        assert store.load_candidate("cand_atom").id == "cand_atom"


# ---------------------------------------------------------------------------
# Revert-check: except Exception: pass must break the decisive tests
# ---------------------------------------------------------------------------


class TestRevertSwallowBreaksSuite:
    """If graduate swallows queue-refresh errors, the rollback test must fail.

    This is the mechanical revert-check required by the lane contract. It
    mutates lifecycle.py in place (restored always), runs the named node, and
    demands a failure — proving the suite is not decoration.
    """

    def test_swallow_mutation_is_caught_by_rollback_node(self) -> None:
        from tests.doctrine import revert_test
        from tests.doctrine._mutate import TextReplace

        # The graduate commit lives in store.commit_graduation — that is the
        # call site whose ``except Exception: pass`` is the named upstream defect.
        target = (
            Path(__file__).resolve().parents[2]
            / "omniagentos"
            / "memlife"
            / "store.py"
        )
        assert target.is_file(), "store.py must exist for revert-check"

        report = revert_test(
            target=target,
            mutation=TextReplace(
                old=(
                    "            # Queue refresh is part of the commit — never swallowed.\n"
                    "            self.refresh_queue()\n"
                ),
                new=(
                    "            # Queue refresh is part of the commit — never swallowed.\n"
                    "            try:\n"
                    "                self.refresh_queue()\n"
                    "            except Exception:\n"
                    "                pass\n"
                ),
            ),
            nodeid=(
                "tests/memlife/test_lifecycle.py::"
                "TestQueueRefreshFailureIsNotSwallowed::"
                "test_queue_refresh_raise_fails_transition_and_leaves_no_lesson"
            ),
        )
        assert report.mutated_failure.failed
        # Failure text must show the assertion/raise path, not a collection error
        text = report.failure_text.lower()
        assert "assert" in text or "runtimeerror" in text or "failed" in text
