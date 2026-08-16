"""Comprehensive CollabStore tests: registry, board CAS, channels, messaging, workflow."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest

from omniagentos.collab.contracts import (
    Agent,
    AgentStatus,
    BoardTask,
    BoardTaskStatus,
    Channel,
    ChannelKind,
    Message,
    MessageKind,
)
from omniagentos.collab.store import CollabStore
from omniagentos.collab.vault import render_conversation_log
from tests.lab.collab.conftest import FakeCollabStore

# ---------------------------------------------------------------------------
# a) Agent Registry
# ---------------------------------------------------------------------------

#: ``agents`` is the identity table capability grants reference, so since
#: migration 108 it also holds broker grant holders (``loop:<instance>``).
#: These helpers separate the collab roster under test from those machine rows.
_MACHINE_HOLDER_PREFIXES = ("loop:", "lane:", "job:", "human:")


def _registered(rows: list[dict[str, Any]]) -> set[str]:
    return {
        row["name"]
        for row in rows
        if not str(row["id"]).startswith(_MACHINE_HOLDER_PREFIXES)
    }


def _registered_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not str(row["id"]).startswith(_MACHINE_HOLDER_PREFIXES)]


def test_register_agent_insert_and_upsert(collab_store: CollabStore) -> None:
    a1 = Agent(name="coder", lineage="claude", expertise=["python", "coding"])
    collab_store.register_agent(a1)
    agents = _registered_rows(collab_store.list_agents())
    assert len(agents) == 1
    assert agents[0]["name"] == "coder"
    assert agents[0]["expertise"] == ["python", "coding"]
    assert agents[0]["id"] == a1.id

    a2 = Agent(name="coder", lineage="cli-codex", expertise=["rust"], model="gpt-5")
    collab_store.register_agent(a2)
    agents = _registered_rows(collab_store.list_agents())
    assert len(agents) == 1
    assert agents[0]["id"] == a1.id  # same id after upsert by name
    assert a2.id == a1.id
    assert agents[0]["lineage"] == "cli-codex"
    assert agents[0]["expertise"] == ["rust"]
    assert agents[0]["model"] == "gpt-5"


def test_list_agents_returns_all(collab_store: CollabStore) -> None:
    collab_store.register_agent(Agent(name="a", lineage="mock"))
    collab_store.register_agent(Agent(name="b", lineage="claude"))
    rows = collab_store.list_agents()
    assert _registered(rows) == {"a", "b"}
    # The machine grant holders migration 108 seeds are deliberately NOT hidden
    # here: /api/access lists collab agents so an operator can see and manage
    # exactly these grants, and a store that filtered them would hide the loop
    # holders from the only surface that administers their capabilities.
    assert {"loop:render_probe", "loop:flowers_collection"} <= {
        row["name"] for row in rows
    }


def test_set_agent_status(collab_store: CollabStore) -> None:
    agent = Agent(name="worker", status=AgentStatus.IDLE)
    collab_store.register_agent(agent)
    collab_store.set_agent_status(agent.id, AgentStatus.BUSY.value)
    row = collab_store.get_agent(agent.id)
    assert row is not None
    assert row["status"] == "busy"


# ---------------------------------------------------------------------------
# b) Board Tasks & Expertise Matching
# ---------------------------------------------------------------------------


def test_create_and_list_board_tasks(collab_store: CollabStore) -> None:
    t1 = BoardTask(title="Fix bug", required_expertise=["python"], priority="high")
    t2 = BoardTask(title="Train model", required_expertise=["ml"], status=BoardTaskStatus.CLAIMED)
    collab_store.create_board_task(t1)
    collab_store.create_board_task(t2)

    all_tasks = collab_store.list_board_tasks()
    assert {t["id"] for t in all_tasks} == {t1.id, t2.id}

    open_only = collab_store.list_board_tasks(status="open")
    assert [t["id"] for t in open_only] == [t1.id]

    pythonish = collab_store.list_board_tasks(expertise=["python"])
    ids = {t["id"] for t in pythonish}
    assert t1.id in ids
    # empty required_expertise can_claim with any expertise — t2 requires ml only
    assert t2.id not in ids


def test_open_tasks_for_expertise_overlap(collab_store: CollabStore) -> None:
    py_ml = BoardTask(title="Py+ML", required_expertise=["python", "ml"])
    ml_only = BoardTask(title="ML only", required_expertise=["ml"])
    anyone = BoardTask(title="Anyone", required_expertise=[])
    claimed = BoardTask(
        title="Claimed py",
        required_expertise=["python"],
        status=BoardTaskStatus.CLAIMED,
    )
    collab_store.create_board_task(py_ml)
    collab_store.create_board_task(ml_only)
    collab_store.create_board_task(anyone)
    collab_store.create_board_task(claimed)

    open_for_python = collab_store.open_tasks_for(["python"])
    ids = {t["id"] for t in open_for_python}
    assert py_ml.id in ids  # overlap on python
    assert anyone.id in ids  # empty required → all agents can claim
    assert ml_only.id not in ids  # no overlap
    assert claimed.id not in ids  # not OPEN


def test_open_tasks_for_empty_requirement(collab_store: CollabStore) -> None:
    free = BoardTask(title="Free-for-all", required_expertise=[])
    collab_store.create_board_task(free)
    assert any(t["id"] == free.id for t in collab_store.open_tasks_for([]))
    assert any(t["id"] == free.id for t in collab_store.open_tasks_for(["anything"]))


# ---------------------------------------------------------------------------
# c) Compare-and-Swap (CAS) Claim Race
# ---------------------------------------------------------------------------


def test_claim_race_exactly_one_winner(collab_store: CollabStore) -> None:
    a1 = Agent(name="racer-1", expertise=["coding"])
    a2 = Agent(name="racer-2", expertise=["coding"])
    collab_store.register_agent(a1)
    collab_store.register_agent(a2)
    task = BoardTask(title="Hot potato", required_expertise=["coding"])
    collab_store.create_board_task(task)

    barrier = Barrier(2)
    results: list[bool] = []

    def race(agent_id: str) -> bool:
        barrier.wait()
        return collab_store.claim_task(task.id, agent_id, 0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(race, a1.id), pool.submit(race, a2.id)]
        results = [f.result() for f in futs]

    assert results.count(True) == 1
    assert results.count(False) == 1

    won = collab_store.get_board_task(task.id)
    assert won is not None
    assert won["status"] == BoardTaskStatus.CLAIMED.value
    assert won["claimed_by"] in {a1.id, a2.id}
    assert won["claim_version"] == 1


def test_reclaim_with_wrong_version_fails(collab_store: CollabStore) -> None:
    agent = Agent(name="solo")
    collab_store.register_agent(agent)
    task = BoardTask(title="Once")
    collab_store.create_board_task(task)

    assert collab_store.claim_task(task.id, agent.id, 0) is True
    assert collab_store.claim_task(task.id, agent.id, 0) is False  # wrong version / not OPEN


def test_claim_non_open_fails(collab_store: CollabStore) -> None:
    agent = Agent(name="late")
    collab_store.register_agent(agent)
    task = BoardTask(title="Done already", status=BoardTaskStatus.DONE)
    collab_store.create_board_task(task)
    assert collab_store.claim_task(task.id, agent.id, 0) is False


def test_update_board_task(collab_store: CollabStore) -> None:
    task = BoardTask(title="WIP")
    collab_store.create_board_task(task)
    collab_store.update_board_task(task.id, {"status": "in_progress", "result_ref": "run_abc"})
    row = collab_store.get_board_task(task.id)
    assert row is not None
    assert row["status"] == "in_progress"
    assert row["result_ref"] == "run_abc"


# ---------------------------------------------------------------------------
# d) Channels & Messaging
# ---------------------------------------------------------------------------


def test_direct_channel_member_pair_dedupe(collab_store: CollabStore) -> None:
    a1 = Agent(name="alice")
    a2 = Agent(name="bob")
    collab_store.register_agent(a1)
    collab_store.register_agent(a2)

    c1 = Channel(
        name="dm",
        kind=ChannelKind.DIRECT,
        members=[a1.id, a2.id],
    )
    c2 = Channel(
        name="dm-again",
        kind=ChannelKind.DIRECT,
        members=[a2.id, a1.id],  # reverse order
    )
    collab_store.create_channel(c1)
    collab_store.create_channel(c2)
    assert c1.id == c2.id
    channels = collab_store.list_channels()
    directs = [c for c in channels if c["kind"] == "direct"]
    assert len(directs) == 1
    assert set(directs[0]["members"]) == {a1.id, a2.id}


def test_add_member_and_list_channels(collab_store: CollabStore) -> None:
    a1 = Agent(name="m1")
    a2 = Agent(name="m2")
    a3 = Agent(name="m3")
    for a in (a1, a2, a3):
        collab_store.register_agent(a)

    group = Channel(name="team", kind=ChannelKind.GROUP, members=[a1.id, a2.id])
    collab_store.create_channel(group)
    collab_store.add_member(group.id, a3.id)
    collab_store.add_member(group.id, a3.id)  # no-op

    for_a3 = collab_store.list_channels(agent_id=a3.id)
    assert len(for_a3) == 1
    assert set(for_a3[0]["members"]) == {a1.id, a2.id, a3.id}

    for_unknown = collab_store.list_channels(agent_id="agt_nobody")
    assert for_unknown == []


def test_post_list_search_messages(collab_store: CollabStore) -> None:
    a1 = Agent(name="talker")
    collab_store.register_agent(a1)
    ch = Channel(name="chat", kind=ChannelKind.GROUP, members=[a1.id])
    collab_store.create_channel(ch)

    m1 = Message(channel_id=ch.id, from_agent=a1.id, body="hello world", ts="2026-01-01T00:00:01Z")
    m2 = Message(channel_id=ch.id, from_agent=a1.id, body="second note", ts="2026-01-01T00:00:02Z")
    m3 = Message(channel_id=ch.id, from_agent=a1.id, body="hello again", ts="2026-01-01T00:00:03Z")
    collab_store.post_message(m1)
    collab_store.post_message(m2)
    collab_store.post_message(m3)

    listed = collab_store.list_messages(ch.id)
    assert [m["id"] for m in listed] == [m1.id, m2.id, m3.id]

    limited = collab_store.list_messages(ch.id, limit=2)
    assert len(limited) == 2
    assert limited[0]["id"] == m1.id

    hits = collab_store.search_messages("hello")
    assert {h["id"] for h in hits} == {m1.id, m3.id}
    limited_hits = collab_store.search_messages("hello", limit=1)
    assert len(limited_hits) == 1


# ---------------------------------------------------------------------------
# e) Message Types & Persistence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        MessageKind.MESSAGE,
        MessageKind.HANDOFF,
        MessageKind.STATUS,
        MessageKind.CLAIM,
        MessageKind.NOTE,
    ],
)
def test_message_kinds_persisted(collab_store: CollabStore, kind: MessageKind) -> None:
    ch = Channel(name="kinds", kind=ChannelKind.GROUP, members=[])
    collab_store.create_channel(ch)
    msg = Message(
        channel_id=ch.id,
        from_agent="system",
        kind=kind,
        body=f"kind={kind.value}",
        ref="btk_linked_task",
    )
    collab_store.post_message(msg)
    rows = collab_store.list_messages(ch.id)
    assert len(rows) == 1
    assert rows[0]["kind"] == kind.value
    assert rows[0]["ref"] == "btk_linked_task"
    assert rows[0]["body"] == f"kind={kind.value}"


# ---------------------------------------------------------------------------
# f) Integration: Full Workflow
# ---------------------------------------------------------------------------


def test_full_workflow_race_message_search(collab_store: CollabStore) -> None:
    agent_a = Agent(name="AgentA", expertise=["coding"])
    agent_b = Agent(name="AgentB", expertise=["coding"])
    collab_store.register_agent(agent_a)
    collab_store.register_agent(agent_b)

    task = BoardTask(
        title="Implement feature",
        required_expertise=["coding"],
        description="ship it",
    )
    collab_store.create_board_task(task)

    open_for_b = collab_store.open_tasks_for(["coding"])
    assert any(t["id"] == task.id for t in open_for_b)

    barrier = Barrier(2)

    def race(agent_id: str) -> bool:
        barrier.wait()
        return collab_store.claim_task(task.id, agent_id, 0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1 = pool.submit(race, agent_a.id)
        r2 = pool.submit(race, agent_b.id)
        results = [r1.result(), r2.result()]

    assert results.count(True) == 1
    assert results.count(False) == 1

    claimed = collab_store.get_board_task(task.id)
    assert claimed is not None
    winner_id = claimed["claimed_by"]
    loser_id = agent_b.id if winner_id == agent_a.id else agent_a.id
    assert winner_id in {agent_a.id, agent_b.id}

    dm = Channel(
        name="claim-dm",
        kind=ChannelKind.DIRECT,
        members=[winner_id, loser_id],
    )
    collab_store.create_channel(dm)
    msg = Message(
        channel_id=dm.id,
        from_agent=winner_id,
        to_agent=loser_id,
        kind=MessageKind.CLAIM,
        body=f"I claimed task {task.id}",
        ref=task.id,
    )
    collab_store.post_message(msg)

    for agent_id in (winner_id, loser_id):
        msgs = collab_store.list_messages(dm.id)
        assert len(msgs) == 1
        assert msgs[0]["body"] == f"I claimed task {task.id}"
        assert agent_id  # both can list

    found = collab_store.search_messages(f"claimed task {task.id}")
    assert len(found) == 1
    assert found[0]["id"] == msg.id


def test_group_channel_three_agents_searchable(collab_store: CollabStore) -> None:
    agents = [Agent(name=f"g{i}") for i in range(3)]
    for a in agents:
        collab_store.register_agent(a)
    group = Channel(
        name="triad",
        kind=ChannelKind.GROUP,
        members=[a.id for a in agents],
    )
    collab_store.create_channel(group)
    collab_store.post_message(
        Message(
            channel_id=group.id,
            from_agent=agents[0].id,
            body="status update from the triad",
            kind=MessageKind.STATUS,
        )
    )
    for a in agents:
        channels = collab_store.list_channels(agent_id=a.id)
        assert any(c["id"] == group.id for c in channels)
    hits = collab_store.search_messages("triad")
    assert len(hits) == 1
    assert hits[0]["channel_id"] == group.id


# ---------------------------------------------------------------------------
# Vault rendering
# ---------------------------------------------------------------------------


def test_render_conversation_log(collab_store: CollabStore) -> None:
    a1 = Agent(name="vault-a")
    a2 = Agent(name="vault-b")
    collab_store.register_agent(a1)
    collab_store.register_agent(a2)
    ch = Channel(
        name="standup",
        kind=ChannelKind.GROUP,
        members=[a1.id, a2.id],
        topic="daily",
    )
    collab_store.create_channel(ch)
    collab_store.post_message(Message(channel_id=ch.id, from_agent=a1.id, body="shipped board CAS"))
    collab_store.post_message(
        Message(
            channel_id=ch.id,
            from_agent=a2.id,
            kind=MessageKind.STATUS,
            body="reviewing",
        )
    )
    channel = collab_store.get_channel(ch.id)
    assert channel is not None
    messages = collab_store.list_messages(ch.id)
    title, body = render_conversation_log(channel, messages)
    assert "standup" in title
    assert "Members:" in body
    assert a1.id in body
    assert "shipped board CAS" in body
    assert "[status]" in body
    assert "reviewing" in body


# ---------------------------------------------------------------------------
# g) Security & Repair: Claim CAS Guard + Search Wildcard Escaping
# ---------------------------------------------------------------------------


def test_patch_cannot_steal_claim(collab_store: CollabStore) -> None:
    """PATCH /board/{id} cannot bypass the claim CAS by setting claimed_by directly."""
    a1 = Agent(name="claimer1", expertise=["coding"])
    a2 = Agent(name="claimer2", expertise=["coding"])
    collab_store.register_agent(a1)
    collab_store.register_agent(a2)

    task = BoardTask(title="Guarded", required_expertise=["coding"])
    collab_store.create_board_task(task)

    # a1 claims it via CAS (legitimate path)
    assert collab_store.claim_task(task.id, a1.id, 0) is True
    claimed = collab_store.get_board_task(task.id)
    assert claimed["claimed_by"] == a1.id
    assert claimed["status"] == BoardTaskStatus.CLAIMED.value

    # a2 attempts to steal the claim via PATCH (the attack)
    # This should fail because claimed_by is NOT in _BOARD_UPDATE_COLUMNS
    with pytest.raises(ValueError, match="unknown columns"):
        collab_store.update_board_task(task.id, {"claimed_by": a2.id})

    # Verify the claim is still held by a1
    still_claimed = collab_store.get_board_task(task.id)
    assert still_claimed["claimed_by"] == a1.id


def test_patch_rejects_status_claimed_transition(collab_store: CollabStore) -> None:
    """PATCH /board/{id} rejects transitions to CLAIMED status (must use claim CAS)."""
    a1 = Agent(name="agent-claim-guard")
    collab_store.register_agent(a1)

    task = BoardTask(title="Locked", required_expertise=[])
    collab_store.create_board_task(task)
    assert task.status == BoardTaskStatus.OPEN

    # Attempt to transition to CLAIMED via PATCH (should fail)
    with pytest.raises(
        ValueError,
        match="status transition to CLAIMED is not allowed via PATCH",
    ):
        collab_store.update_board_task(task.id, {"status": BoardTaskStatus.CLAIMED.value})

    # Verify status is still OPEN
    still_open = collab_store.get_board_task(task.id)
    assert still_open["status"] == BoardTaskStatus.OPEN.value
    assert still_open["claimed_by"] is None

    # The legitimate path via claim_task CAS works
    assert collab_store.claim_task(task.id, a1.id, 0) is True
    now_claimed = collab_store.get_board_task(task.id)
    assert now_claimed["status"] == BoardTaskStatus.CLAIMED.value
    assert now_claimed["claimed_by"] == a1.id

    # After claiming, legitimate PATCH transitions work (e.g., CLAIMED -> IN_PROGRESS)
    collab_store.update_board_task(task.id, {"status": "in_progress"})
    progressing = collab_store.get_board_task(task.id)
    assert progressing["status"] == "in_progress"
    assert progressing["claimed_by"] == a1.id  # preserved


def test_search_messages_escapes_like_wildcards(collab_store: CollabStore) -> None:
    """search_messages correctly escapes % and _ so they match literally."""
    ch = Channel(name="wildcard-test", kind=ChannelKind.GROUP, members=[])
    collab_store.create_channel(ch)

    # Post messages containing literal LIKE metacharacters
    m1 = Message(
        channel_id=ch.id,
        from_agent="system",
        body="Sale: 100% off everything!",
        ts="2026-01-01T00:00:01Z",
    )
    m2 = Message(
        channel_id=ch.id,
        from_agent="system",
        body="Match a_b pattern literally",
        ts="2026-01-01T00:00:02Z",
    )
    m3 = Message(
        channel_id=ch.id,
        from_agent="system",
        body="Normal message with text",
        ts="2026-01-01T00:00:03Z",
    )
    collab_store.post_message(m1)
    collab_store.post_message(m2)
    collab_store.post_message(m3)

    # Search for literal "100%" — should match m1 only, not everything
    percent_hits = collab_store.search_messages("100%")
    assert len(percent_hits) == 1
    assert percent_hits[0]["id"] == m1.id
    assert "100% off" in percent_hits[0]["body"]

    # Search for literal "a_b" — should match m2 only, not patterns like "axb"
    underscore_hits = collab_store.search_messages("a_b")
    assert len(underscore_hits) == 1
    assert underscore_hits[0]["id"] == m2.id
    assert "a_b pattern" in underscore_hits[0]["body"]

    # Search for a normal word still works
    normal_hits = collab_store.search_messages("message")
    assert len(normal_hits) == 1  # m3 has "message"
    ids = {h["id"] for h in normal_hits}
    assert m3.id in ids
    assert m1.id not in ids
    assert m2.id not in ids

    # Search for backslash (escaped escape char)
    m4 = Message(
        channel_id=ch.id,
        from_agent="system",
        body="path: C:\\Users\\test",
        ts="2026-01-01T00:00:04Z",
    )
    collab_store.post_message(m4)
    backslash_hits = collab_store.search_messages("C:\\Users")
    assert len(backslash_hits) == 1
    assert backslash_hits[0]["id"] == m4.id


# ---------------------------------------------------------------------------
# h) FakeCollabStore board-ordering parity with the real store
# ---------------------------------------------------------------------------


def test_fake_store_board_paging_matches_real_ordering_on_tied_timestamps(
    collab_store: CollabStore, fake_collab_store: FakeCollabStore
) -> None:
    """The double must page IDENTICALLY to SQL's ORDER BY created_at DESC, id DESC.

    ``utc_now_iso`` has one-second resolution, so cards created in the same
    second tie on ``created_at`` and only ``id DESC`` decides the page. A
    created_at-only sort in the double looks right until ``limit`` is used, then
    it silently hands route tests a different first card than production.
    """
    tied_at = "2026-07-31T00:00:00Z"
    cards = [
        BoardTask(
            id=f"btk_tie_{suffix}", title=f"Tied {suffix}", created_at=tied_at, updated_at=tied_at
        )
        for suffix in ("aaa", "mmm", "zzz")
    ]
    for card in cards:
        collab_store.create_board_task(card)
        fake_collab_store.create_board_task(card)

    for limit in (1, 2, 3):
        real_page = [t["id"] for t in collab_store.list_board_tasks(limit=limit)]
        fake_page = [t["id"] for t in fake_collab_store.list_board_tasks(limit=limit)]
        assert fake_page == real_page, f"limit={limit}"
    # And the tiebreak is the documented one: highest id first.
    assert collab_store.list_board_tasks(limit=1)[0]["id"] == "btk_tie_zzz"
