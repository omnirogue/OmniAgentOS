"""Parent/child cards are exactly ONE level deep, and a parent waits for them.

Depth ≤ 1 is not a style choice. It is what makes "is every subtask finished?"
a single indexed query instead of a recursive walk, and it is why no cycle
longer than a self-reference is even expressible. Each refusal below closes one
way the depth could grow: from the child's end (filing under a child), from the
parent's end (a card with children becoming one), and the degenerate case.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.store import TeamStore


@pytest.fixture
def owned(make_card: Callable[..., BoardTask], employees: dict[str, str]) -> BoardTask:
    """A HUMAN card: owner + acceptance criteria, so the done-gate is armed."""
    return make_card(
        title="Ship the team board",
        owner_employee_id=employees["bob"],
        acceptance_criteria="the queue renders",
    )


class TestSettingAParent:
    def test_a_card_can_become_a_subtask(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        parent = make_card(title="Parent")
        child = make_card(title="Child")
        assert collab_store.update_board_task(child.id, {"parent_task_id": parent.id}) is True
        row = collab_store.get_board_task(child.id)
        assert row is not None
        assert row["parent_task_id"] == parent.id

    def test_a_card_cannot_parent_itself(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Ouroboros")
        with pytest.raises(ValueError, match="cannot be the task itself"):
            collab_store.update_board_task(card.id, {"parent_task_id": card.id})
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["parent_task_id"] is None

    def test_a_grandchild_is_refused(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        parent = make_card(title="Parent")
        child = make_card(title="Child")
        grandchild = make_card(title="Grandchild")
        collab_store.update_board_task(child.id, {"parent_task_id": parent.id})
        with pytest.raises(ValueError, match="one level deep"):
            collab_store.update_board_task(grandchild.id, {"parent_task_id": child.id})

    def test_a_card_with_subtasks_cannot_become_one(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        """The same violation seen from the other end: adopting a parent would
        make grandchildren of every card it already holds."""
        top = make_card(title="Top")
        middle = make_card(title="Middle")
        bottom = make_card(title="Bottom")
        collab_store.update_board_task(bottom.id, {"parent_task_id": middle.id})
        with pytest.raises(ValueError, match="has subtasks"):
            collab_store.update_board_task(middle.id, {"parent_task_id": top.id})

    def test_an_unknown_parent_is_refused_by_name(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Orphan")
        with pytest.raises(ValueError, match="parent task not found"):
            collab_store.update_board_task(card.id, {"parent_task_id": "btk_nope"})

    def test_clearing_the_parent_is_always_allowed(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        parent = make_card(title="Parent")
        child = make_card(title="Child")
        collab_store.update_board_task(child.id, {"parent_task_id": parent.id})
        assert collab_store.update_board_task(child.id, {"parent_task_id": None}) is True
        row = collab_store.get_board_task(child.id)
        assert row is not None
        assert row["parent_task_id"] is None


class TestTheCreatePathIsHeldToTheSameRule:
    """A rule the update path enforces and the create path does not is not a
    rule — it is a detour. Creating the card already parented must refuse
    exactly what patching it into that shape refuses."""

    def test_creating_a_grandchild_is_refused(
        self, make_card: Callable[..., BoardTask], collab_store: CollabStore
    ) -> None:
        parent = make_card(title="Parent")
        child = make_card(title="Child", parent_task_id=parent.id)
        with pytest.raises(ValueError, match="one level deep"):
            make_card(title="Grandchild", parent_task_id=child.id)
        assert len(collab_store.list_board_tasks()) == 2

    def test_creating_under_an_unknown_parent_is_refused(
        self, make_card: Callable[..., BoardTask]
    ) -> None:
        with pytest.raises(ValueError, match="parent task not found"):
            make_card(title="Orphan", parent_task_id="btk_nope")

    def test_an_owned_card_cannot_be_born_done(
        self, make_card: Callable[..., BoardTask], employees: dict[str, str]
    ) -> None:
        """A brand-new card has no evidence by construction, so a create that
        claims completion is the one shape the done-gate can never grant."""
        with pytest.raises(ValueError, match="without evidence"):
            make_card(
                title="Already finished, honest",
                status=BoardTaskStatus.DONE,
                owner_employee_id=employees["bob"],
                acceptance_criteria="it works",
            )


class TestAParentWaitsForItsSubtasks:
    def test_done_is_refused_while_a_subtask_is_open(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        owned: BoardTask,
    ) -> None:
        subtask = make_card(title="Subtask", parent_task_id=owned.id)
        team_store.add_evidence(kind="commit", ref="deadbeef", repo="omnios", task_id=owned.id)
        with pytest.raises(ValueError, match=f"subtask {subtask.id} is open"):
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.DONE.value})
        row = collab_store.get_board_task(owned.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.OPEN.value

    def test_done_passes_once_every_subtask_is_terminal_and_evidence_exists(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        owned: BoardTask,
    ) -> None:
        finished = make_card(title="Finished", parent_task_id=owned.id)
        abandoned = make_card(title="Abandoned", parent_task_id=owned.id)
        collab_store.update_board_task(finished.id, {"status": BoardTaskStatus.DONE.value})
        collab_store.update_board_task(abandoned.id, {"status": BoardTaskStatus.CANCELLED.value})
        team_store.add_evidence(kind="pr", ref="41", repo="omnios", task_id=owned.id)
        assert (
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.DONE.value}) is True
        )
        row = collab_store.get_board_task(owned.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.DONE.value

    def test_owned_parent_without_acceptance_still_waits_for_open_subtask(
        self,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        parent = make_card(
            title="Owned parent without acceptance",
            owner_employee_id=employees["bob"],
        )
        child = make_card(title="Still open", parent_task_id=parent.id)
        with pytest.raises(ValueError, match=f"subtask {child.id} is open"):
            collab_store.update_board_task(parent.id, {"status": BoardTaskStatus.DONE.value})
        assert collab_store.get_board_task(parent.id)["status"] == BoardTaskStatus.OPEN.value

    def test_an_agent_parent_is_not_gated_by_its_subtasks(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        """No owner means no human contract: the swarm's own parent/child cards
        finish exactly as they did before migration 123."""
        parent = make_card(title="Swarm root")
        make_card(title="Swarm member", parent_task_id=parent.id)
        assert (
            collab_store.update_board_task(parent.id, {"status": BoardTaskStatus.DONE.value})
            is True
        )
