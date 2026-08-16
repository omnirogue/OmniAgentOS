"""EDC P3: delegate (/task family), defer (queue | workqueue), outcome-verified
completion. Every §14 "P3 Verify" bullet is proven here.

The completion sweep reads ONLY strong, outcome-verified signals — a done card's
``verified_at``, a wq terminal pass, a declared probe — never a weak one (a done
card with no verification, a branch/commit/claim/open PR). The operator
self-verify exemption (F10) is exercised and its log asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.edc import actions
from omniagentos.edc.store import DecisionStore, available_actions_for, machine_spec
from omniagentos.edc.sweep import run_completion_sweep
from omniagentos.team import tasks as team_tasks
from omniagentos.team.store import TeamStore
from omniagentos.workqueue.store import WorkQueueStore
from tests.edc.conftest import make_decision

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class _Notifier:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_dm(self, slack_user_id: str, text: str, **_kwargs: Any) -> bool:
        self.posts.append(f"{slack_user_id}:{text}")
        return True


def _reverse() -> dict[str, str]:
    return {"emp_owner": "UOWNER", "emp_alice": "UALICE", "emp_bob": "UBOB"}


def _seed_company_goal(store: SqliteStore, slug: str = "globex") -> str:
    """A company + its 'General engineering —' goal so pool cards can ladder."""
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"co_{slug}", slug, slug.title(), "active", utc_now_iso()),
    )
    goal = CompanyGoalsStore(store).create_goal(
        goal_id=f"cgl_{slug}",
        org_company_id=f"co_{slug}",
        title="General engineering — keep the lights on",
        horizon="quarter",
    )
    return str(goal["id"])


def _delegatable(decisions: DecisionStore, *, ref: str = "p3-1", **over: object) -> dict[str, Any]:
    defaults: dict[str, object] = {
        "classification": "needs_owner",
        "recommended": {"kind": "reply", "human_line": "renew the AWS card before Friday"},
    }
    defaults.update(over)
    payload = make_decision(source_ref=ref, **defaults)
    return decisions.create_decision(payload)[0]


def _claim_delegate(
    decisions: DecisionStore, row: dict[str, Any], *, actor: str, assignee: str, notifier: _Notifier
) -> dict[str, Any]:
    claimed = decisions.resolve(
        row["id"],
        actor=actor,
        resolution="delegate",
        params={"execution": {"assignee": assignee}},
    )
    return actions.delegate(
        decisions,
        claimed,
        actor=actor,
        notifier=notifier,
        reverse_map=_reverse(),
    )


# --------------------------------------------------------------------------
# Delegate — canonical card + EXACTLY ONE DM (to Alice and to Bob)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("assignee", ["emp_alice", "emp_bob"])
def test_delegate_makes_canonical_card_and_exactly_one_dm(
    decisions: DecisionStore, employees: dict[str, str], assignee: str
) -> None:
    row = _delegatable(decisions, ref=f"p3-del-{assignee}")
    notifier = _Notifier()
    result = _claim_delegate(decisions, row, actor="emp_owner", assignee=assignee, notifier=notifier)

    # Landed dispatched (not done): the OUTCOME is proven later by the sweep.
    assert result["status"] == "done_unverified"
    ref = f"EDC-{row['number']}"
    assert result["board_task_ref"] == ref
    assert result["assignee_employee_id"] == assignee

    # The canonical board card: owned by the assignee, EDC ref, decision link,
    # and source='decision' so v4 scoring counts it as Work (F07), not a
    # zero-point ad-hoc Task.
    collab = actions._collab_on(decisions)
    card = collab.get_board_task(result["board_task_id"])
    assert card is not None
    assert card["owner_employee_id"] == assignee
    assert card["ref"] == ref
    assert card["source"] == "decision"
    assert card["source"] != team_tasks.TASK_ADHOC_SOURCE
    assert card["acceptance_criteria"] == "renew the AWS card before Friday"
    assert f"decision:{row['id']}" in card["description"]

    # EXACTLY ONE DM (the assign notice) — the create event carries no owner:
    # token, so no watcher DMs a second time.
    assert len(notifier.posts) == 1
    assert notifier.posts[0].startswith(_reverse()[assignee])
    assert ref in notifier.posts[0]


def test_delegate_never_self_assigns(decisions: DecisionStore, employees: dict[str, str]) -> None:
    row = _delegatable(decisions, ref="p3-self")
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="delegate",
        params={"execution": {"assignee": "emp_owner"}},
    )
    with pytest.raises(PermissionError, match="own owner"):
        actions.delegate(decisions, claimed, actor="emp_owner")
    # The permission refusal leaves the card uncreated.
    latest = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert latest is not None and not latest.get("board_task_id")


# --------------------------------------------------------------------------
# Completion — /task done + evidence + counter-sign → decision completes
# --------------------------------------------------------------------------


def _drive_card_done(collab: Any, team: TeamStore, card_id: str, *, owner: str) -> None:
    team.add_evidence(
        kind="note",
        ref="slack://done",
        task_id=card_id,
        actor=owner,
        title="completed",
        attribution="manual",
    )
    collab.update_board_task(card_id, {"status": "done"}, actor=owner)


def test_delegated_card_verified_completes_decision_and_dms_owner(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    row = _delegatable(decisions, ref="p3-complete")
    notifier = _Notifier()
    result = _claim_delegate(
        decisions, row, actor="emp_owner", assignee="emp_alice", notifier=notifier
    )
    collab = actions._collab_on(decisions)
    team = TeamStore(decisions._store)
    card_id = result["board_task_id"]

    _drive_card_done(collab, team, card_id, owner="emp_alice")
    # Counter-signed by someone OTHER than the assignee (owner cannot self-verify).
    verified = team.verify_task(card_id, "emp_owner")
    assert verified is not None and verified["verified_at"]

    sweep_notifier = _Notifier()
    stats = run_completion_sweep(
        decisions._store,
        ["emp_owner"],
        now=NOW,
        notifier=sweep_notifier,
        slack_reverse=_reverse(),
    )
    assert stats["verified"] == 1
    completed = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert completed is not None and completed["status"] == "done_verified"
    assert completed["verification"]["path"] == "board_card"
    assert completed["verification"]["verified_by"] == "emp_owner"
    # A verified-outcome DM to the owner.
    assert any("verified complete" in post for post in sweep_notifier.posts)
    # The audit trail records the outcome verification.
    events = [e["event"] for e in decisions.list_events(row["id"], owner_employee_id="emp_owner")]
    assert events[-1] == "verify_outcome"


def test_weak_signals_do_not_complete_the_decision(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    """A done card that is NOT verified — the weak signal — never completes.

    Stands in for every weak signal (branch/commit/Slack-claim/open PR): the
    sweep consumes only ``verified_at``, so a card that is merely 'done' leaves
    the decision ``done_unverified``.
    """
    row = _delegatable(decisions, ref="p3-weak")
    result = _claim_delegate(
        decisions, row, actor="emp_owner", assignee="emp_alice", notifier=_Notifier()
    )
    collab = actions._collab_on(decisions)
    team = TeamStore(decisions._store)
    _drive_card_done(collab, team, result["board_task_id"], owner="emp_alice")
    # NO verify_task — verified_at stays NULL.

    stats = run_completion_sweep(decisions._store, ["emp_owner"], now=NOW)
    assert stats["verified"] == 0
    still = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert still is not None and still["status"] == "done_unverified"


def test_assignee_self_verify_is_refused(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    row = _delegatable(decisions, ref="p3-selfverify")
    result = _claim_delegate(
        decisions, row, actor="emp_owner", assignee="emp_alice", notifier=_Notifier()
    )
    collab = actions._collab_on(decisions)
    team = TeamStore(decisions._store)
    _drive_card_done(collab, team, result["board_task_id"], owner="emp_alice")
    # The assignee (card owner) cannot verify their own work without mechanical
    # evidence — the whole point of counter-signing.
    with pytest.raises(ValueError, match="cannot verify their own"):
        team.verify_task(result["board_task_id"], "emp_alice")


# --------------------------------------------------------------------------
# Defer — shared queue (the operator) and machine (repo-shaped)
# --------------------------------------------------------------------------


def test_defer_to_queue_lands_an_ownerless_pool_card(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    _seed_company_goal(decisions._store, "globex")
    row = _delegatable(decisions, ref="p3-defer-q", company_slug="globex")
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="defer",
        params={"execution": {"defer_mode": "queue"}},
    )
    result = actions.defer(decisions, claimed, actor="emp_owner", mode="queue")
    assert result["status"] == "done_unverified"

    collab = actions._collab_on(decisions)
    card = collab.get_board_task(result["board_task_id"])
    assert card is not None
    # A pool-conformant card: ownerless, goal-laddered, acceptance-bearing.
    assert card["owner_employee_id"] is None
    assert card["goal_id"] == "cgl_globex"
    assert card["acceptance_criteria"]
    assert card["ref"] == f"EDC-{row['number']}"
    team = TeamStore(decisions._store)
    assert any(c.ref == card["ref"] for c in team.pool_cards(limit=50))


def test_defer_to_queue_is_operator_only(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    """A non-the operator owner may not add to the shared queue — the matrix says the operator."""
    _seed_company_goal(decisions._store, "globex")
    row = _delegatable(
        decisions, ref="p3-defer-notowner", owner_employee_id="emp_bob", company_slug="globex"
    )
    # available_actions offers a non-the operator owner Snooze/Delegate, NOT defer.
    assert "defer" not in available_actions_for(row)
    assert "delegate" in available_actions_for(row)


def _machine_recommended() -> dict[str, Any]:
    return {
        "kind": "execute",
        "human_line": "patch the failing test",
        "machine": {
            "repo_url": "https://github.com/Globex/OmniAgentOS",
            "repo_slug": "OmniAgentOS",
            "base_sha": "a" * 40,
            "branch": "edc/fix-123",
            "owned_paths": ["omniagentos/edc/"],
            "acceptance_cmd": "pytest tests/edc -q",
            "agent_profile": "sol-coder",
            "risk_class": "standard",
        },
    }


def test_defer_to_machine_lands_a_wq_unit_and_redefer_is_noop(
    decisions: DecisionStore, employees: dict[str, str], tmp_path: Any
) -> None:
    wq = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    row = _delegatable(decisions, ref="p3-defer-m", recommended=_machine_recommended())
    # The machine lane is offered because the recommendation is repo-shaped.
    assert "defer:machine" in available_actions_for(row)

    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="defer",
        params={"execution": {"defer_mode": "machine"}},
    )
    result = actions.defer(decisions, claimed, actor="emp_owner", mode="machine", wq_store=wq)
    unit_id = result["wq_unit_id"]
    assert unit_id and result["execution"]["deduped"] is False
    unit = wq.get_unit(unit_id)
    assert unit is not None and unit["idempotency_key"] == f"edc:{row['id']}"
    assert unit["submitted_by"] == "emp_owner"
    assert "edc" in unit["labels"]

    # Re-defer is a no-op on the idempotency key: the same envelope returns the
    # SAME unit with deduped=True and never a duplicate.
    again_id, deduped = wq.enqueue(
        actions._machine_submit(claimed, "emp_owner", _machine_recommended()["machine"])
    )
    assert again_id == unit_id and deduped is True


def test_defer_to_machine_completes_on_terminal_pass(
    decisions: DecisionStore, employees: dict[str, str], tmp_path: Any
) -> None:
    wq = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    row = _delegatable(decisions, ref="p3-defer-mc", recommended=_machine_recommended())
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="defer",
        params={"execution": {"defer_mode": "machine"}},
    )
    result = actions.defer(decisions, claimed, actor="emp_owner", mode="machine", wq_store=wq)

    # Drive the unit to a terminal accepted pass (the state record_result writes).
    wq._connection.execute(
        "UPDATE wq_units SET state = 'done', terminal_reason = 'accepted', "
        "finished_at = ?, result_sha = ? WHERE id = ?",
        (utc_now_iso(), "b" * 40, result["wq_unit_id"]),
    )
    wq._connection.commit()

    notifier = _Notifier()
    stats = run_completion_sweep(
        decisions._store,
        ["emp_owner"],
        now=NOW,
        wq_store=wq,
        notifier=notifier,
        slack_reverse=_reverse(),
    )
    assert stats["verified"] == 1
    done = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert done is not None and done["status"] == "done_verified"
    assert done["verification"]["path"] == "workqueue"


# --------------------------------------------------------------------------
# F10 — operator self-verify exemption, logged
# --------------------------------------------------------------------------


def test_operator_self_verify_exemption_is_logged(
    decisions: DecisionStore, employees: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A the operator-deferred pool card the operator himself claims + completes rests on the
    operator self-verify exemption — the sweep completes it AND logs the
    exemption explicitly, never a silent self-attestation (F10)."""
    _seed_company_goal(decisions._store, "globex")
    row = _delegatable(decisions, ref="p3-f10", company_slug="globex")
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="defer",
        params={"execution": {"defer_mode": "queue"}},
    )
    result = actions.defer(decisions, claimed, actor="emp_owner", mode="queue")
    collab = actions._collab_on(decisions)
    team = TeamStore(decisions._store)
    card_id = result["board_task_id"]

    # the operator claims the pool card himself, then completes + self-verifies (the
    # operator is the only identity permitted to verify their own card).
    assert team_tasks.assign_pool_card(collab, card_id, "emp_owner", "emp_owner")
    _drive_card_done(collab, team, card_id, owner="emp_owner")
    team.verify_task(card_id, "emp_owner")

    capsys.readouterr()  # drop prior output
    stats = run_completion_sweep(decisions._store, ["emp_owner"], now=NOW)
    assert stats["verified"] == 1
    logged = capsys.readouterr().out
    assert "operator self-verify exemption" in logged
    done = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert done is not None
    assert done["verification"]["operator_self_verify"] is True


# --------------------------------------------------------------------------
# Matrix + edit + execute-preview
# --------------------------------------------------------------------------


def test_available_actions_matrix_by_owner_and_shape(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    owner_row = make_decision(status="open", classification="needs_owner", owner_employee_id="emp_owner")
    assert "delegate" in available_actions_for(owner_row)
    assert "defer" in available_actions_for(owner_row)  # the operator → queue-add allowed
    assert "defer:machine" not in available_actions_for(owner_row)  # no repo shape

    bob_row = make_decision(
        status="open", classification="needs_owner", owner_employee_id="emp_bob"
    )
    assert "delegate" in available_actions_for(bob_row)
    assert "defer" not in available_actions_for(bob_row)  # non-the operator, no machine

    machine_row = make_decision(
        status="open",
        classification="needs_owner",
        owner_employee_id="emp_bob",
        recommended=_machine_recommended(),
    )
    # Repo-shaped work opens the machine lane even for a non-the operator owner.
    assert "defer" in available_actions_for(machine_row)
    assert "defer:machine" in available_actions_for(machine_row)

    maybe_row = make_decision(status="open", classification="maybe")
    assert available_actions_for(maybe_row) == ["edit", "dismiss", "note"]


def test_owner_edits_the_recommended_action(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    row = _delegatable(decisions, ref="p3-edit")
    new_reco = {"kind": "reply", "human_line": "escalate to finance instead"}
    edited = decisions.resolve(
        row["id"], actor="emp_owner", resolution="edit", params={"recommended": new_reco}
    )
    assert edited["recommended"]["human_line"] == "escalate to finance instead"
    # Editing the recommendation (no draft) keeps the item open/editable.
    assert edited["status"] == "open"


class _FakeExec:
    consequential = True

    def __init__(self) -> None:
        self.order: list[str] = []

    def preview(self, decision: dict[str, Any]) -> dict[str, Any]:
        self.order.append("preview")
        return {"kind": "execute", "shown": True}

    def execute(
        self, decision: dict[str, Any], *, store: DecisionStore, actor: str
    ) -> dict[str, Any]:
        # The dry-run preview must have been produced BEFORE the effect runs.
        assert self.order == ["preview"]
        self.order.append("execute")
        return {"provider_message_id": "cap-1"}

    def verify(self, result: dict[str, Any]) -> dict[str, Any]:
        return {"verified": False}


def test_execute_path_shows_dry_run_preview_before_executing(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    row = _delegatable(decisions, ref="p3-exec", status="awaiting_approval")
    claimed = decisions.resolve(
        row["id"], actor="emp_owner", resolution="approve", params={"execution": {}}
    )
    fake = _FakeExec()
    result = actions.run_executor(
        decisions, claimed, actor="emp_owner", kind="send_email", executor=fake
    )
    assert fake.order == ["preview", "execute"]
    assert result["status"] == "done_unverified"
    assert result["execution"]["shown"] is True


# --------------------------------------------------------------------------
# C1 — delegate / defer are idempotent: a retry NEVER creates a second card
# --------------------------------------------------------------------------


def _ref_card_count(decisions: DecisionStore, ref: str) -> int:
    return int(
        decisions._store._connection.execute(
            "SELECT COUNT(*) AS n FROM board_tasks WHERE ref = ?", (ref,)
        ).fetchone()["n"]
    )


def test_delegate_reentry_creates_exactly_one_card(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    """A retry that lands after the card write but before the CAS re-drives the
    SAME linkage — it must not create (nor ref-conflict on) a second card."""
    row = _delegatable(decisions, ref="p3-c1-del")
    ref = f"EDC-{row['number']}"
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="delegate",
        params={"execution": {"assignee": "emp_alice"}},
    )
    # First execute: creates the linked card, sets board_task_id (status stays
    # in_progress — run_executor is what performs the terminal CAS).
    first = actions.execute_delegate(claimed, store=decisions, actor="emp_owner")
    assert first["board_task_id"]
    current = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert current is not None and current["status"] == "in_progress"
    assert current["board_task_id"] == first["board_task_id"]

    # Retry with the reloaded row (board_task_id already set): the guard re-drives
    # the same linkage without touching the board — no exception, no duplicate.
    second = actions.execute_delegate(current, store=decisions, actor="emp_owner")
    assert second["board_task_id"] == first["board_task_id"]
    assert second.get("reentrant") is True
    assert _ref_card_count(decisions, ref) == 1


def test_defer_queue_reentry_creates_exactly_one_pool_card(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    _seed_company_goal(decisions._store, "globex")
    row = _delegatable(decisions, ref="p3-c1-defq", company_slug="globex")
    ref = f"EDC-{row['number']}"
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="defer",
        params={"execution": {"defer_mode": "queue"}},
    )
    executor = actions._DeferExecutor()
    first = executor.execute(claimed, store=decisions, actor="emp_owner")
    assert first["board_task_id"]
    current = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert current is not None and current["board_task_id"] == first["board_task_id"]

    second = executor.execute(current, store=decisions, actor="emp_owner")
    assert second["board_task_id"] == first["board_task_id"]
    assert second.get("reentrant") is True
    assert _ref_card_count(decisions, ref) == 1


# --------------------------------------------------------------------------
# F1/E1 — the one-shot DM flag stamps only on delivery (never before)
# --------------------------------------------------------------------------


def _force_done_unverified(
    decisions: DecisionStore, row: dict[str, Any], *, verification: dict[str, Any] | None = None
) -> None:
    fields: dict[str, Any] = {"status": "done_unverified"}
    if verification is not None:
        fields["verification"] = verification
    decisions.update_decision(row["id"], owner_employee_id="emp_owner", fields=fields)


def test_unverified_dm_flag_stamps_only_on_delivery(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    """notifier=None (a real prod state with no SLACK_BOT_TOKEN) must NOT freeze
    the one-shot flag — a later healthy tick has to still deliver the notice."""
    row = _delegatable(decisions, ref="p3-f1")
    _force_done_unverified(decisions, row)  # no card, no unit, no probe → case (a)

    # Tick 1: no notifier. The DM cannot be sent, so the flag stays UNSET and a
    # *_failed counter records the drop.
    down = run_completion_sweep(decisions._store, ["emp_owner"], now=NOW, notifier=None)
    assert down["unverified_dm"] == 0
    assert down["unverified_dm_failed"] == 1
    mid = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert mid is not None and "unverified_dm_at" not in (mid.get("execution") or {})

    # Tick 2: healthy notifier — the notice is delivered and only NOW stamped.
    notifier = _Notifier()
    up = run_completion_sweep(
        decisions._store, ["emp_owner"], now=NOW, notifier=notifier, slack_reverse=_reverse()
    )
    assert up["unverified_dm"] == 1
    assert any("outcome not verified" in post for post in notifier.posts)
    after = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert after is not None and (after.get("execution") or {}).get("unverified_dm_at")

    # Tick 3: healthy again — the flag now holds, so no duplicate DM.
    repeat = run_completion_sweep(
        decisions._store, ["emp_owner"], now=NOW, notifier=_Notifier(), slack_reverse=_reverse()
    )
    assert repeat["unverified_dm"] == 0


# --------------------------------------------------------------------------
# E2 — a declared-but-PENDING probe must not fire the "no feasible probe" DM
# --------------------------------------------------------------------------


def test_pending_probe_sends_no_false_dm(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    row = _delegatable(decisions, ref="p3-e2")
    _force_done_unverified(
        decisions,
        row,
        verification={
            "capability": "gmail_owner.get_message",
            "method": "GET",
            "path": "/gmail/v1/users/me/messages/x",
            "expect": {"labelIds": ["SENT"]},
        },
    )

    def pending_probe(_decision: dict[str, Any]) -> dict[str, Any]:
        return {"verified": False}  # feasible probe, simply not passed yet

    notifier = _Notifier()
    stats = run_completion_sweep(
        decisions._store,
        ["emp_owner"],
        now=NOW,
        notifier=notifier,
        slack_reverse=_reverse(),
        probe_runner=pending_probe,
    )
    # No completion, and crucially NO "no feasible probe" DM (that would be false).
    assert stats["verified"] == 0
    assert stats["unverified_dm"] == 0
    assert notifier.posts == []
    still = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert still is not None and still["status"] == "done_unverified"
    assert "unverified_dm_at" not in (still.get("execution") or {})

    # A later PASS of the same probe completes it (case (c)).
    def passing_probe(_decision: dict[str, Any]) -> dict[str, Any]:
        return {"verified": True, "provider_status": "SENT"}

    done_stats = run_completion_sweep(
        decisions._store, ["emp_owner"], now=NOW, probe_runner=passing_probe
    )
    assert done_stats["verified"] == 1
    done = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert done is not None and done["status"] == "done_verified"
    assert done["verification"]["path"] == "probe"


# --------------------------------------------------------------------------
# F3 — a malformed base_sha is rejected at the EDC layer (no machine lane)
# --------------------------------------------------------------------------


def test_machine_spec_rejects_malformed_base_sha() -> None:
    good = _machine_recommended()
    assert machine_spec(good) is not None

    for bad in ("z" * 40, "a" * 39, "a" * 41, "A" * 40, "not-a-sha", ""):
        spec = {**good["machine"], "base_sha": bad}
        assert machine_spec({**good, "machine": spec}) is None


def test_defer_machine_not_offered_for_malformed_base_sha(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    bad = _machine_recommended()
    bad["machine"]["base_sha"] = "a" * 39
    row = make_decision(status="open", classification="needs_owner", recommended=bad)
    actions_offered = available_actions_for(row)
    assert "defer:machine" not in actions_offered
