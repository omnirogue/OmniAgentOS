"""Split admission RESERVES the projected cost of the children it admits.

plan-08 defect 4. GUARD 5 historically compared only ACCRUED spend to
``budget_usd_max``, and every spawned worker was handed the WHOLE remaining run
budget as its ceiling. Neither statement is a function of how many live siblings
a split has already put in the field, so depth multiplied the effective budget:
a depth-2 split of width 2 left four live leaves each holding a ceiling equal to
the entire root budget -- a 4x overrun only discoverable after the money was
spent. Measured on the base commit, before this change::

    4 live leaves each handed a $10.0 ceiling = $40.0 against a $10.0 root budget

The fix reserves at ADMISSION. A split hands its children exactly the
entitlement its parent held -- the parent's own reservation when it carries one
(it is itself a split child), otherwise the run's remaining headroom, which is
precisely the ceiling that parent would have been handed had it spawned instead
of splitting. So a split can only ever DIVIDE a ceiling, never multiply one, and
the reservation rides in the child's ``swarm_json.budget_reserved_usd`` where the
same transaction that terminalizes the card releases it -- a split parent closes
``cancelled``, handing its slice straight to the children that replaced it, with
no separate release write for a crash to lose.

Deliberately NOT a run-wide "sum of every live reservation vs the cap" check:
that refuses a sibling root card's first, entirely legitimate split the moment
another card has split, because reservations are promises rather than spend.
Inheriting the parent's entitlement kills the multiplication (the actual defect)
without ever refusing work the accrued-only guard would have admitted at the
same accrued spend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm.scheduler import _RunState
from tests.swarm.scheduler_fakes import Harness, make_harness, make_scheduler

BUDGET = 10.0
RESERVED_KEY = "budget_reserved_usd"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every DB touch goes through the harness; workbooks land under tmp_path."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))
    monkeypatch.setattr(
        "omniagentos.swarm.spawn.default_swarm_var_root", lambda: tmp_path / "var" / "swarm"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root_of(task_key: str) -> str:
    return task_key.split(".", 1)[0]


def _parent_swarm_json(task_key: str, **overrides: Any) -> dict[str, Any]:
    swarm_json: dict[str, Any] = {
        "task_key": task_key,
        "owned_paths": [f"src/{_root_of(task_key)}"],
        "risk_class": "none",
        "acceptance": "ok",
        "verify_command": "true",
    }
    swarm_json.update(overrides)
    return swarm_json


def _subtasks(parent_key: str, width: int = 2) -> list[dict[str, Any]]:
    root = _root_of(parent_key)
    stem = parent_key.replace(".", "_")
    return [
        {
            "title": f"{parent_key} part {n}",
            "description": "implement one disjoint half",
            "owned_paths": [f"src/{root}/{stem}_{n}.txt"],
            "est_minutes": 10,
            "est_agent_minutes": 10,
        }
        for n in range(1, width + 1)
    ]


def _request(parent_key: str, width: int = 2) -> dict[str, Any]:
    return {"reason": "splits cleanly into disjoint files", "subtasks": _subtasks(parent_key, width)}


def _row_for_key(harness: Harness, task_key: str) -> dict[str, Any]:
    for task in harness.dal.tasks_for_run(harness.run_id):
        swarm_json = harness.dal.get_swarm_json(str(task["id"])) or {}
        if str(swarm_json.get("task_key") or "") == task_key:
            return dict(task)
    raise AssertionError(f"no task with task_key {task_key!r}")


def _split(harness: Harness, scheduler: Any, parent_key: str, width: int = 2) -> None:
    """Admit ``width`` children under ``parent_key`` through the SHARED durable
    half of a split -- the same call the timeout path and the worker-request
    path both make, so the reservation cannot differ between the two."""
    task_row = _row_for_key(harness, parent_key)
    swarm_json = harness.dal.get_swarm_json(str(task_row["id"])) or {}
    state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
    scheduler._provision_split(state, task_row, swarm_json, _subtasks(parent_key, width))


def _live_cards(harness: Harness) -> dict[str, dict[str, Any]]:
    """``task_key -> swarm_json`` for every LIVE plan card in the run (the run's
    bookkeeping root card carries no ``task_key`` and is skipped)."""
    out: dict[str, dict[str, Any]] = {}
    for task in harness.dal.tasks_for_run(harness.run_id):
        if str(task["status"]) in {"done", "blocked", "cancelled"}:
            continue
        swarm_json = harness.dal.get_swarm_json(str(task["id"])) or {}
        task_key = str(swarm_json.get("task_key") or "")
        if task_key:
            out[task_key] = swarm_json
    return out


def _spend(harness: Harness, task_key: str, usd: float) -> None:
    """Burn ``usd`` against ``task_key`` through the real attempt ledger."""
    attempt = harness.dal.open_attempt(
        harness.run_id,
        str(_row_for_key(harness, task_key)["id"]),
        provider="claude",
        model="opus",
        source="test",
    )
    harness.dal.record_attempt_usage(str(attempt["id"]), cost_usd=usd)
    harness.dal.close_attempt(str(attempt["id"]), "completed")


def _spawn_ceiling(harness: Harness, scheduler: Any, task_key: str) -> float | None:
    """The ``budget_usd_max`` the scheduler actually hands the worker for
    ``task_key`` -- read off the real SpawnRequest, not recomputed here."""
    harness.dal.set_run_status(harness.run_id, "running")
    state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
    before = len(harness.world.spawn_requests)
    scheduler._execute_task(state, 0, _row_for_key(harness, task_key))
    assert len(harness.world.spawn_requests) > before, f"no spawn happened for {task_key}"
    return harness.world.spawn_requests[before].budget_usd_max


def _guard(harness: Harness, scheduler: Any, task_key: str, width: int = 2) -> tuple[Any, Any]:
    """Run the six PKG-REQUEST-SUBTASKS guards over a well-formed request for
    ``task_key``; returns ``(deny_reason, specs)``."""
    run = harness.dal.get_run(harness.run_id)
    assert run is not None
    row = _row_for_key(harness, task_key)
    swarm_json = harness.dal.get_swarm_json(str(row["id"])) or {}
    deny, specs, _reason = scheduler._validate_subtasks_payload(
        _request(task_key, width), swarm_json, str(row["id"]), run
    )
    return deny, specs


def _make(
    tmp_path: Path, *, budget: float | None = BUDGET, keys: tuple[str, ...] = ("a",)
) -> tuple[Harness, Any]:
    harness = make_harness(
        tmp_path,
        [{"id": key, "owned_paths": [f"src/{key}"]} for key in keys],
        integration=False,
        budget=budget,
        max_concurrency=10,
    )
    return harness, make_scheduler(harness)


# ---------------------------------------------------------------------------
# THE FALSIFIER: "a depth-2 split tree still admits children whose combined
# ceilings exceed the root budget_usd_max"
# ---------------------------------------------------------------------------


class TestDepthTwoSplitTreeCannotExceedRootBudget:
    def test_combined_leaf_reservations_never_exceed_the_root_budget(
        self, tmp_path: Path
    ) -> None:
        """a -> {a.1, a.2} -> {a.1.1, a.1.2, a.2.1, a.2.2}: four live leaves,
        and what they may collectively still spend is the ROOT budget, once."""
        harness, scheduler = _make(tmp_path)
        try:
            _split(harness, scheduler, "a")
            _split(harness, scheduler, "a.1")
            _split(harness, scheduler, "a.2")

            leaves = _live_cards(harness)
            assert set(leaves) == {"a.1.1", "a.1.2", "a.2.1", "a.2.2"}

            reserved = [float(sj.get(RESERVED_KEY) or 0.0) for sj in leaves.values()]
            assert all(usd > 0.0 for usd in reserved), (
                f"every admitted child must hold a positive reservation, got {reserved}"
            )
            assert sum(reserved) == pytest.approx(BUDGET), (
                f"depth-2 tree reservations {reserved} sum to {sum(reserved)} "
                f"against a ${BUDGET} root budget"
            )
        finally:
            harness.close()

    def test_the_ceiling_handed_to_a_leaf_is_its_slice_not_the_whole_run_budget(
        self, tmp_path: Path
    ) -> None:
        """The observable form of the defect: with four live leaves in the
        field, the ceiling on the SpawnRequest for any one of them must leave
        room for the other three."""
        harness, scheduler = _make(tmp_path)
        try:
            _split(harness, scheduler, "a")
            _split(harness, scheduler, "a.1")
            _split(harness, scheduler, "a.2")
            live = len(_live_cards(harness))
            assert live == 4

            ceiling = _spawn_ceiling(harness, scheduler, "a.1.1")

            assert ceiling is not None
            assert ceiling > 0.0, "a funded leaf must still be given a budget"
            assert ceiling * live <= BUDGET + 1e-9, (
                f"{live} live leaves each handed a ${ceiling} ceiling = "
                f"${ceiling * live} against a ${BUDGET} root budget"
            )
        finally:
            harness.close()

    def test_a_split_divides_its_parents_slice_and_the_parent_releases_it(
        self, tmp_path: Path
    ) -> None:
        """Release is coupled to terminalization: the split transaction closes
        the parent ``cancelled``, so its slice is not held by a dead card and is
        not double-counted against the children that replaced it."""
        harness, scheduler = _make(tmp_path)
        try:
            _split(harness, scheduler, "a", width=4)
            parent_slice = float(_live_cards(harness)["a.1"][RESERVED_KEY])
            assert parent_slice == pytest.approx(BUDGET / 4)

            _split(harness, scheduler, "a.1", width=2)

            assert str(_row_for_key(harness, "a.1")["status"]) == "cancelled"
            children = {k: v for k, v in _live_cards(harness).items() if k.startswith("a.1.")}
            assert set(children) == {"a.1.1", "a.1.2"}
            assert sum(float(sj[RESERVED_KEY]) for sj in children.values()) == pytest.approx(
                parent_slice
            ), "the children inherit their parent's slice exactly -- no more, no less"
        finally:
            harness.close()


# ---------------------------------------------------------------------------
# GUARD 5 now refuses on PROJECTED cost, not only on money already burned
# ---------------------------------------------------------------------------


class TestGuardFiveReservesProjectedCost:
    def test_admission_is_denied_when_the_parents_own_slice_is_exhausted(
        self, tmp_path: Path
    ) -> None:
        """The heart of the defect. The card holds a $2.50 reservation and has
        spent all $2.50; the RUN has $7.50 of its $10 left, so the accrued-only
        guard says yes and the card fans out four more workers against money
        that belongs to its siblings.

        The reservation stamp is applied directly here rather than by splitting
        a parent first: GUARD 2 (depth) refuses a dotted ``task_key`` before
        GUARD 5 is ever consulted, and unifying that depth rule across the
        timeout and worker-request paths is a separate change in flight. This
        exercises the budget guard on its own axis, with the exact stamp a split
        child carries.
        """
        harness, scheduler = _make(tmp_path)
        try:
            scheduler._merge_swarm_json(
                str(_row_for_key(harness, "a")["id"]), {RESERVED_KEY: BUDGET / 4}
            )
            _spend(harness, "a", BUDGET / 4)

            spend = harness.dal.budget_spend(harness.run_id)
            assert spend.accrued_cost_usd == pytest.approx(BUDGET / 4)
            assert spend.accrued_cost_usd < BUDGET, (
                "premise: the accrued-only guard has no reason to refuse here"
            )

            deny, specs = _guard(harness, scheduler, "a")

            assert deny == "budget"
            assert specs is None
        finally:
            harness.close()

    def test_a_partly_spent_slice_still_funds_a_split_of_what_is_left(
        self, tmp_path: Path
    ) -> None:
        """Spend against a reservation is not double counted: $1 burned out of a
        $2.50 slice leaves $1.50 to divide, not $2.50 and not nothing."""
        harness, scheduler = _make(tmp_path)
        try:
            scheduler._merge_swarm_json(
                str(_row_for_key(harness, "a")["id"]), {RESERVED_KEY: BUDGET / 4}
            )
            _spend(harness, "a", 1.0)

            deny, specs = _guard(harness, scheduler, "a", width=3)
            assert deny is None
            assert specs is not None and len(specs) == 3

            _split(harness, scheduler, "a", width=3)
            children = _live_cards(harness)
            assert set(children) == {"a.1", "a.2", "a.3"}
            for key, swarm_json in children.items():
                assert float(swarm_json[RESERVED_KEY]) == pytest.approx(1.5 / 3), key
        finally:
            harness.close()

    def test_a_reservation_never_outlives_the_money_that_backed_it(
        self, tmp_path: Path
    ) -> None:
        """A cap bounds an attempt rather than stopping it mid-flight, so a
        sibling can overshoot its own slice and blow the RUN cap while this card
        still holds an unspent promise. The promise must not be honoured against
        money that no longer exists -- this guard is never weaker than the
        accrued-only policy it replaces."""
        harness, scheduler = _make(tmp_path)
        try:
            scheduler._merge_swarm_json(
                str(_row_for_key(harness, "a")["id"]), {RESERVED_KEY: BUDGET / 2}
            )
            harness.dal.add_cost(harness.run_id, BUDGET + 2.0)  # a sibling overshot

            deny, specs = _guard(harness, scheduler, "a")
            assert deny == "budget"
            assert specs is None
        finally:
            harness.close()

    def test_the_accrued_only_denial_is_preserved_exactly(self, tmp_path: Path) -> None:
        """Backward compatibility: a card with NO reservation is entitled to the
        run's remaining headroom, so the guard reduces to the old
        ``accrued >= budget`` test and denies where it always denied."""
        harness, scheduler = _make(tmp_path, budget=1.0)
        try:
            assert _guard(harness, scheduler, "a")[0] is None  # $1 unspent: admitted
            harness.dal.add_cost(harness.run_id, 5.0)
            deny, specs = _guard(harness, scheduler, "a")
            assert deny == "budget"
            assert specs is None
        finally:
            harness.close()


# ---------------------------------------------------------------------------
# The guard must NOT over-reserve: legitimate work is still admitted and funded
# ---------------------------------------------------------------------------


class TestReservationDoesNotOverReserve:
    def test_a_split_that_fits_the_budget_is_still_admitted_and_funded(
        self, tmp_path: Path
    ) -> None:
        harness, scheduler = _make(tmp_path)
        try:
            deny, specs = _guard(harness, scheduler, "a")
            assert deny is None and specs is not None

            _split(harness, scheduler, "a", width=2)
            leaves = _live_cards(harness)
            assert set(leaves) == {"a.1", "a.2"}
            for key, swarm_json in leaves.items():
                assert float(swarm_json[RESERVED_KEY]) == pytest.approx(BUDGET / 2), key

            _split(harness, scheduler, "a.1", width=2)
            grandchildren = {k: v for k, v in _live_cards(harness).items() if k.startswith("a.1.")}
            assert set(grandchildren) == {"a.1.1", "a.1.2"}
            for key, swarm_json in grandchildren.items():
                assert float(swarm_json[RESERVED_KEY]) == pytest.approx(BUDGET / 4), key

            assert _spawn_ceiling(harness, scheduler, "a.2") == pytest.approx(BUDGET / 2), (
                "a.2's untouched half must still be spendable in full"
            )
        finally:
            harness.close()

    def test_one_cards_split_does_not_starve_an_unrelated_sibling_card(
        self, tmp_path: Path
    ) -> None:
        """The over-reservation trap. ``a`` splitting promises the whole
        remaining budget to ``a.1``/``a.2``, but those are PROMISES, not spend:
        the plan's other root card ``b`` must still be admitted for a split of
        its own and must still be handed the run's real headroom to spend."""
        harness, scheduler = _make(tmp_path, keys=("a", "b"))
        try:
            _split(harness, scheduler, "a", width=2)

            deny, specs = _guard(harness, scheduler, "b")
            assert deny is None, f"a sibling card's first split was refused: {deny}"
            assert specs is not None and len(specs) == 2

            assert _spawn_ceiling(harness, scheduler, "b") == pytest.approx(BUDGET), (
                "an unreserved card keeps exactly the ceiling it had before"
            )
        finally:
            harness.close()

    def test_an_uncapped_run_is_completely_unaffected(self, tmp_path: Path) -> None:
        """No ``budget_usd_max`` means no reservation and no new refusal."""
        harness, scheduler = _make(tmp_path, budget=None)
        try:
            _split(harness, scheduler, "a", width=4)
            _split(harness, scheduler, "a.1", width=4)
            leaves = _live_cards(harness)
            assert len(leaves) == 7
            for key, swarm_json in leaves.items():
                assert RESERVED_KEY not in swarm_json, key
            assert _guard(harness, scheduler, "a.2")[0] != "budget"
            assert _spawn_ceiling(harness, scheduler, "a.2") is None
        finally:
            harness.close()
