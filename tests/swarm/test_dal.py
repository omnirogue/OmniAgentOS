"""SwarmDal: runs lifecycle, DAG eligibility, blocked propagation, attempts,
membership metadata invariants (category = cross-lane metadata; lane frozen)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.longhaul.store import LonghaulStore
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    # A CollabStore-migrated template copy carries the shared schema (incl.
    # 044) without re-applying all 86 migrations per test.
    return migrated_db(CollabStore, tmp_path / "swarm-dal.db")


def _card(collab: CollabStore, title: str) -> str:
    task = BoardTask(title=title, status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    return task.id


def _make_run(dal: SwarmDal, **overrides) -> str:
    defaults = {"working_dir": "/tmp/ws", "goal": "test goal"}
    defaults.update(overrides)
    return str(dal.create_run(**defaults, source="test")["id"])


class TestRuns:
    def test_create_get_and_status_edges(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run = dal.create_run(
                working_dir="/tmp/ws",
                goal="build it",
                budget_usd_max=5.0,
                target_concurrency=3, source="test")
            assert run["id"].startswith("swr_")
            assert run["status"] == "queued"
            assert run["plan_json"] == "{}"
            assert run["started_at"] is None

            assert dal.set_run_status(run["id"], "running") is True
            row = dal.get_run(run["id"])
            assert row["status"] == "running"
            assert row["started_at"] is not None
            assert row["finished_at"] is None
            assert row["heartbeat_at"] is not None

            assert dal.set_run_status(run["id"], "completed") is True
            row = dal.get_run(run["id"])
            assert row["finished_at"] is not None

            with pytest.raises(ValueError):
                dal.set_run_status(run["id"], "sprinting")
            with pytest.raises(ValueError):
                dal.create_run(working_dir="/tmp", goal="g", status="bogus", source="test")
        finally:
            dal.close()

    def test_admission_queries_and_stale_sweep_excludes_queued(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            first_queued = _make_run(dal)
            _make_run(dal)  # second queued run
            active = _make_run(dal)
            dal.set_run_status(active, "running")

            assert dal.active_run_count() == 1
            oldest = dal.oldest_queued()
            assert oldest is not None and oldest["id"] == first_queued

            # Age every heartbeat far past the cutoff: only ACTIVE runs fail.
            dal._connection.execute("UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z'")
            failed = dal.mark_stale_failed(stale_minutes=2)
            assert [row["id"] for row in failed] == [active]
            assert dal.get_run(first_queued)["status"] == "queued"
            assert dal.get_run(active)["status"] == "failed"
            assert "stale heartbeat" in str(dal.get_run(active)["error"])
        finally:
            dal.close()

    def test_heartbeat_and_metrics(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            dal._connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (run_id,),
            )
            dal.heartbeat(run_id)
            assert dal.get_run(run_id)["heartbeat_at"] > "2020-01-01T00:00:00Z"

            assert dal.set_metrics(run_id, {"speedup": 3.2}) is True
            assert dal.get_run(run_id)["metrics_json"] == '{"speedup":3.2}'
        finally:
            dal.close()


class TestTerminalCardCloseOut:
    """A run that has stopped must not leave live-looking cards on the board.

    Regression: only ``_complete_run`` closed its root card, so every failed /
    cancelled run left its root (``Swarm: <goal>``) plus every unfinished member
    pinned at ``in_progress``/``open`` forever — the board showed permanently
    "Running" work no coordinator would ever advance.
    """

    def _run_with_cards(self, db_path: str, dal: SwarmDal) -> tuple[str, dict[str, str]]:
        collab = CollabStore(db_path)
        run_id = _make_run(dal)
        ids = {
            "root": _card(collab, "Swarm: build it"),
            "running": _card(collab, "member running"),
            "open": _card(collab, "member open"),
            "done": _card(collab, "member done"),
            "blocked": _card(collab, "member blocked"),
        }
        for card_id in ids.values():
            dal.assign_task_to_run(card_id, run_id)
        collab.update_board_task(ids["root"], {"status": "in_progress"})
        collab.update_board_task(ids["running"], {"status": "in_progress"})
        collab.update_board_task(ids["done"], {"status": "done"})
        collab.update_board_task(ids["blocked"], {"status": "blocked"})
        return run_id, ids

    def _statuses(self, dal: SwarmDal, run_id: str) -> dict[str, str]:
        return {str(task["id"]): str(task["status"]) for task in dal.tasks_for_run(run_id)}

    def test_cancel_settles_live_cards_and_leaves_terminal_ones(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, ids = self._run_with_cards(db_path, dal)
            dal.set_run_status(run_id, "cancelled", error="cancelled by operator")
            statuses = self._statuses(dal, run_id)
            assert statuses[ids["root"]] == "cancelled"
            assert statuses[ids["running"]] == "cancelled"
            assert statuses[ids["open"]] == "cancelled"
            # Already-terminal cards keep their own outcome.
            assert statuses[ids["done"]] == "done"
            assert statuses[ids["blocked"]] == "blocked"
        finally:
            dal.close()

    def test_failure_lands_live_cards_on_blocked(self, db_path: str) -> None:
        """`blocked` is the honest state for a run that died mid-flight — and the
        one state the dashboard offers Resume on."""
        dal = SwarmDal(db_path)
        try:
            run_id, ids = self._run_with_cards(db_path, dal)
            dal.set_run_status(run_id, "failed", error="coordinator died")
            statuses = self._statuses(dal, run_id)
            assert statuses[ids["root"]] == "blocked"
            assert statuses[ids["running"]] == "blocked"
            assert statuses[ids["open"]] == "blocked"
            assert statuses[ids["done"]] == "done"
        finally:
            dal.close()

    def test_non_terminal_transitions_do_not_touch_cards(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, ids = self._run_with_cards(db_path, dal)
            dal.set_run_status(run_id, "running")
            dal.set_run_status(run_id, "merging")
            statuses = self._statuses(dal, run_id)
            assert statuses[ids["running"]] == "in_progress"
            assert statuses[ids["open"]] == "open"
        finally:
            dal.close()

    def test_close_out_is_idempotent_and_clears_the_claimant(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, ids = self._run_with_cards(db_path, dal)
            first = dal.close_out_run_cards(run_id, run_status="cancelled")
            assert set(first) == {ids["root"], ids["running"], ids["open"]}
            assert dal.close_out_run_cards(run_id, run_status="cancelled") == []
            claimants = {str(task["id"]): task["claimed_by"] for task in dal.tasks_for_run(run_id)}
            assert claimants[ids["running"]] is None
        finally:
            dal.close()

    def test_stale_sweep_settles_the_dead_runs_cards(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, ids = self._run_with_cards(db_path, dal)
            dal.set_run_status(run_id, "running")
            dal._connection.execute(  # noqa: SLF001 -- forcing a stale heartbeat
                "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (run_id,),
            )
            assert [str(row["id"]) for row in dal.mark_stale_failed(stale_minutes=5)] == [run_id]
            statuses = self._statuses(dal, run_id)
            assert statuses[ids["root"]] == "blocked"
            assert statuses[ids["open"]] == "blocked"
        finally:
            dal.close()


class TestFinishedRunsSince:
    """WP8 optimizer read: finished_runs_since — the (finished_at, rowid)
    watermark query."""

    def test_excludes_non_terminal_runs(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            queued = _make_run(dal)
            running = _make_run(dal)
            dal.set_run_status(running, "running")
            done = _make_run(dal)
            dal.set_run_status(done, "completed")

            ids = [row["id"] for row in dal.finished_runs_since(None, 0)]
            assert ids == [done]
            assert queued not in ids
            assert running not in ids
        finally:
            dal.close()

    def test_orders_oldest_first_and_carries_rowid(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            first = _make_run(dal)
            second = _make_run(dal)
            dal.set_run_status(second, "completed")
            dal.set_run_status(first, "completed")
            dal._connection.execute(
                "UPDATE swarm_runs SET finished_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                (first,),
            )
            dal._connection.execute(
                "UPDATE swarm_runs SET finished_at = '2026-01-02T00:00:00Z' WHERE id = ?",
                (second,),
            )
            rows = dal.finished_runs_since(None, 0)
            assert [row["id"] for row in rows] == [first, second]
            assert all("_rowid" in row for row in rows)
            assert rows[0]["_rowid"] < rows[1]["_rowid"]
        finally:
            dal.close()

    def test_since_finished_at_excludes_boundary_run(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            first = _make_run(dal)
            second = _make_run(dal)
            dal.set_run_status(first, "completed")
            dal.set_run_status(second, "completed")
            dal._connection.execute(
                "UPDATE swarm_runs SET finished_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                (first,),
            )
            dal._connection.execute(
                "UPDATE swarm_runs SET finished_at = '2026-01-02T00:00:00Z' WHERE id = ?",
                (second,),
            )
            rows_from_first = dal.finished_runs_since("2026-01-01T00:00:00Z", 10**9)
            # a huge since_rowid at the exact boundary timestamp excludes `first`
            # itself while still catching `second` (strictly later finished_at).
            assert [row["id"] for row in rows_from_first] == [second]

            rows_from_scratch = dal.finished_runs_since(None, 0)
            assert [row["id"] for row in rows_from_scratch] == [first, second]
        finally:
            dal.close()

    def test_rowid_tiebreak_on_identical_finished_at(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            first = _make_run(dal)
            second = _make_run(dal)
            dal.set_run_status(first, "completed")
            dal.set_run_status(second, "completed")
            tied = "2026-01-01T00:00:00Z"
            dal._connection.execute(
                "UPDATE swarm_runs SET finished_at = ? WHERE id IN (?, ?)",
                (tied, first, second),
            )
            all_rows = dal.finished_runs_since(None, 0)
            assert [row["id"] for row in all_rows] == [first, second]
            first_rowid = all_rows[0]["_rowid"]

            # Watermarked exactly at the first row's (finished_at, rowid): only
            # the second (higher-rowid) row at the SAME timestamp is returned.
            remaining = dal.finished_runs_since(tied, first_rowid)
            assert [row["id"] for row in remaining] == [second]
        finally:
            dal.close()


class TestFleetReadHelpers:
    """WP6a additions: tasks_for_run (any status) and live_attempt_count."""

    def test_tasks_for_run_returns_every_status(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            other_run_id = _make_run(dal)
            a, b = (_card(collab, name) for name in ("A", "B"))
            outside = _card(collab, "Outside")
            dal.assign_task_to_run(a, run_id)
            dal.assign_task_to_run(b, run_id)
            dal.assign_task_to_run(outside, other_run_id)
            collab.update_board_task(b, {"status": "blocked"})

            tasks = dal.tasks_for_run(run_id)
            assert {t["id"] for t in tasks} == {a, b}
            assert {t["status"] for t in tasks} == {"open", "blocked"}
        finally:
            dal.close()

    def test_tasks_for_run_empty_for_unknown_run(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            assert dal.tasks_for_run("swr_nope") == []
        finally:
            dal.close()

    def test_live_attempt_count_across_runs(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            a, b = (_card(collab, name) for name in ("A", "B"))
            dal.assign_task_to_run(a, run_id)
            dal.assign_task_to_run(b, run_id)
            assert dal.live_attempt_count() == 0

            attempt_a = dal.open_attempt(run_id, a, provider="claude", model="sonnet", source="test")
            assert dal.live_attempt_count() == 1
            dal.open_attempt(run_id, b, provider="codex", model="gpt-5.6-sol", source="test")
            assert dal.live_attempt_count() == 2

            dal.close_attempt(attempt_a["id"], "completed")
            assert dal.live_attempt_count() == 1
        finally:
            dal.close()


class TestEligibleTasks:
    def test_diamond_dag_unlocks_in_order(self, db_path: str) -> None:
        """A → {B, C} → D: eligibility follows done-ness, claims exclude."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            a, b, c, d = (_card(collab, name) for name in ("A", "B", "C", "D"))
            for task_id in (a, b, c, d):
                assert dal.assign_task_to_run(task_id, run_id) is True
            dal.add_deps(run_id, [(b, a), (c, a), (d, b), (d, c)])

            assert [t["id"] for t in dal.eligible_tasks(run_id)] == [a]

            collab.update_board_task(a, {"status": "done"})
            assert {t["id"] for t in dal.eligible_tasks(run_id)} == {b, c}

            # A live claim removes eligibility (status leaves 'open').
            assert collab.claim_task(b, "agt_worker", 0) is True
            assert {t["id"] for t in dal.eligible_tasks(run_id)} == {c}

            collab.update_board_task(b, {"status": "done"})
            collab.update_board_task(c, {"status": "done"})
            assert [t["id"] for t in dal.eligible_tasks(run_id)] == [d]
        finally:
            dal.close()

    def test_live_attempt_excludes_task_defensively(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            a = _card(collab, "solo")
            dal.assign_task_to_run(a, run_id)
            assert [t["id"] for t in dal.eligible_tasks(run_id)] == [a]

            attempt = dal.open_attempt(run_id, a, provider="claude", model="sonnet", source="test")
            assert dal.eligible_tasks(run_id) == []

            dal.close_attempt(attempt["id"], "crashed", "boom")
            assert [t["id"] for t in dal.eligible_tasks(run_id)] == [a]
        finally:
            dal.close()


class TestBlockedPropagation:
    def test_transitive_dependents_blocked_integration_exempt(self, db_path: str) -> None:
        """A→B→{D,E}, C→D; D is the integration task and stays runnable."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            a, b, c, d, e = (_card(collab, name) for name in ("A", "B", "C", "D", "E"))
            for task_id in (a, b, c, e):
                dal.assign_task_to_run(task_id, run_id)
            dal.assign_task_to_run(d, run_id, swarm_json={"integration": True})
            dal.add_deps(run_id, [(b, a), (d, b), (d, c), (e, b)])

            collab.update_board_task(a, {"status": "blocked"})
            updated = dal.propagate_blocked(run_id, a)

            assert sorted(updated) == sorted([b, e])
            assert collab.get_board_task(b)["status"] == "blocked"
            assert collab.get_board_task(e)["status"] == "blocked"
            assert collab.get_board_task(d)["status"] == "open"  # integration exempt
            assert collab.get_board_task(c)["status"] == "open"  # not a dependent
        finally:
            dal.close()

    def test_terminal_dependents_left_untouched_but_traversed(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            a, b, c = (_card(collab, name) for name in ("A", "B", "C"))
            for task_id in (a, b, c):
                dal.assign_task_to_run(task_id, run_id)
            dal.add_deps(run_id, [(b, a), (c, b)])
            collab.update_board_task(b, {"status": "done"})

            updated = dal.propagate_blocked(run_id, a)

            assert updated == [c]  # grandchild caught through the done middle task
            assert collab.get_board_task(b)["status"] == "done"
        finally:
            dal.close()


class TestAttempts:
    def test_one_live_attempt_and_seq_chain(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "worker task")
            dal.assign_task_to_run(task_id, run_id)

            first = dal.open_attempt(run_id, task_id, provider="claude", model="sonnet", source="test")
            assert (first["seq"], first["end_reason"]) == (0, None)
            assert dal.current_attempt(task_id)["id"] == first["id"]

            with pytest.raises(RuntimeError):
                dal.open_attempt(run_id, task_id, provider="codex", model="sol", source="test")

            assert dal.close_attempt(first["id"], "rate_limited", "429") is True
            assert dal.close_attempt(first["id"], "completed") is False  # CAS: already closed
            closed = dal.list_attempts(task_id)[0]
            assert (closed["end_reason"], closed["detail"]) == ("rate_limited", "429")

            second = dal.open_attempt(
                run_id,
                task_id,
                provider="codex",
                model="sol",
                tier="standard",
                account_id="acc_1",
                session_id="ses_1", source="test")
            assert second["seq"] == 1
            assert [a["seq"] for a in dal.list_attempts(task_id)] == [0, 1]

            with pytest.raises(ValueError):
                dal.close_attempt(second["id"], "wandered_off")
        finally:
            dal.close()

    def test_open_attempt_guards(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            other_run = _make_run(dal)
            member = _card(collab, "member")
            done = _card(collab, "done")
            dal.assign_task_to_run(member, run_id)
            dal.assign_task_to_run(done, run_id)
            collab.update_board_task(done, {"status": "done"})

            with pytest.raises(ValueError):
                dal.open_attempt(run_id, done, provider="claude", model="sonnet", source="test")
            with pytest.raises(ValueError):
                dal.open_attempt(other_run, member, provider="claude", model="sonnet", source="test")
            with pytest.raises(ValueError):
                dal.open_attempt(run_id, "btk_missing", provider="claude", model="sonnet", source="test")
        finally:
            dal.close()


class TestMembershipCategoryMetadata:
    """FB4+: category_id is cross-lane METADATA — a card may be swarm AND
    categorized. Safety moved into the lane-scoped WIP count
    (``LonghaulStore.claim_category_slot`` / ``next_waiting_in_category``
    filter ``lane='longhaul'``), tested in tests/longhaul/test_store.py."""

    def test_swarm_accepts_categorized_card_and_leaves_metadata_alone(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        longhaul = LonghaulStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            card = _card(collab, "categorized card")
            category = longhaul.create_category("Backend")
            assert longhaul.set_task_category(card, category["id"]) is True

            assert dal.assign_task_to_run(card, run_id) is True
            row = collab.get_board_task(card)
            assert row["swarm_run_id"] == run_id
            assert row["category_id"] == category["id"]  # untouched
            assert row["lane"] is None  # untouched
        finally:
            dal.close()
            longhaul.close()

    def test_assign_rejects_longhaul_lane_card(self, db_path: str) -> None:
        """F3: lane is the execution-ownership axis — a lane='longhaul' card is
        owned by the longhaul engine and must never bind to a swarm run. A
        lane='fast' card stays bindable (only longhaul ownership is exclusive)."""
        collab = CollabStore(db_path)
        longhaul = LonghaulStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            card = _card(collab, "longhaul-owned card")
            assert longhaul.set_lane(card, "longhaul") is True
            with pytest.raises(ValueError, match="longhaul-lane"):
                dal.assign_task_to_run(card, run_id)
            row = collab.get_board_task(card)
            assert row["swarm_run_id"] is None  # untouched
            assert row["lane"] == "longhaul"

            fast = _card(collab, "fast-lane card")
            assert longhaul.set_lane(fast, "fast") is True
            assert dal.assign_task_to_run(fast, run_id) is True
            assert collab.get_board_task(fast)["swarm_run_id"] == run_id
        finally:
            dal.close()
            longhaul.close()

    def test_assign_still_rejects_unknown_card(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            with pytest.raises(ValueError, match="unknown board task"):
                dal.assign_task_to_run("btk_missing", run_id)
        finally:
            dal.close()

    def test_longhaul_categorizes_swarm_card(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        longhaul = LonghaulStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            card = _card(collab, "swarm card")
            assert dal.assign_task_to_run(card, run_id) is True
            category = longhaul.create_category("Backend")

            assert longhaul.set_task_category(card, category["id"]) is True
            row = collab.get_board_task(card)
            assert row["category_id"] == category["id"]
            assert row["swarm_run_id"] == run_id
            # Clearing (None) stays allowed on any card.
            assert longhaul.set_task_category(card, None) is True
            assert collab.get_board_task(card)["category_id"] is None
        finally:
            dal.close()
            longhaul.close()

    def test_provision_run_persists_per_card_category_id(self, db_path: str) -> None:
        collab = CollabStore(db_path)
        longhaul = LonghaulStore(db_path)
        dal = SwarmDal(db_path)
        try:
            category = longhaul.create_category("Dashboard")
            run = dal.provision_run(
                run={"working_dir": "/tmp/ws", "goal": "categorized run", "plan": {}},
                root_card={
                    "id": "btk_root_cat",
                    "title": "root",
                    "status": "in_progress",
                    "category_id": category["id"],
                },
                cards=[
                    {
                        "id": "btk_member_cat",
                        "title": "member",
                        "swarm_json": {},
                        "category_id": category["id"],
                    },
                    {"id": "btk_member_plain", "title": "plain", "swarm_json": {}},
                ],
                edges=[],
            )
            root = collab.get_board_task("btk_root_cat")
            member = collab.get_board_task("btk_member_cat")
            plain = collab.get_board_task("btk_member_plain")
            assert root["category_id"] == category["id"]
            assert member["category_id"] == category["id"]
            assert plain["category_id"] is None
            for row in (root, member, plain):
                assert row["swarm_run_id"] == run["id"]
                assert row["lane"] is None  # lane stays frozen NULL
        finally:
            dal.close()
            longhaul.close()

    def test_lane_never_written_by_swarm_membership(self, db_path: str) -> None:
        """Frozen 043 CHECK: swarm membership must leave lane NULL."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            card = _card(collab, "lane check")
            dal.assign_task_to_run(card, run_id, swarm_json={"complexity": "simple"})
            row = collab.get_board_task(card)
            assert row["lane"] is None
            assert row["swarm_run_id"] == run_id
        finally:
            dal.close()

    def test_lane_check_still_rejects_swarm_value(self, db_path: str) -> None:
        connection = sqlite3.connect(db_path, isolation_level=None)
        try:
            collab = CollabStore(db_path)
            card = _card(collab, "frozen lane")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE board_tasks SET lane = 'swarm' WHERE id = ?", (card,))
        finally:
            connection.close()
