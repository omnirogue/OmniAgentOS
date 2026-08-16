"""PATCH hardening (multi-company Work OS, 2026-08-13).

Three refusals, all raised as ``ValueError`` (the route maps them to 400):

* an unknown ``status`` used to write straight through and drop the card out
  of every queue bucket;
* an unknown ``priority`` used to write through and rank LAST — a silent
  demotion of the card its author meant to escalate;
* a card's OWNER re-sizing their own card re-prices their own points.
"""

from __future__ import annotations

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore


@pytest.fixture
def employees(store) -> dict[str, str]:
    goals_store = CompanyGoalsStore(store)
    for employee_id, name, role in (
        ("emp_owner", "the operator", "operator"),
        ("emp_alice", "Alice", "reviewer-merger"),
        ("emp_bob", "Bob", "candidate-author"),
    ):
        goals_store.ensure_employee(employee_id=employee_id, name=name, role=role)
    return {"owner": "emp_owner", "alice": "emp_alice", "bob": "emp_bob"}


def _card(collab_store: CollabStore, **fields) -> BoardTask:
    fields.setdefault("title", "A card")
    card = BoardTask(**fields)
    collab_store.create_board_task(card)
    return card


class TestStatusVocabulary:
    def test_unknown_status_is_refused(self, collab_store: CollabStore) -> None:
        card = _card(collab_store)
        with pytest.raises(ValueError, match="status must be one of"):
            collab_store.update_board_task(card.id, {"status": "in-progress"})
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["status"] == BoardTaskStatus.OPEN.value

    def test_every_legal_status_still_writes(self, collab_store: CollabStore) -> None:
        card = _card(collab_store)
        assert collab_store.update_board_task(card.id, {"status": "in_progress"})
        assert collab_store.update_board_task(card.id, {"status": "done"})

    def test_claimed_via_patch_stays_refused(self, collab_store: CollabStore) -> None:
        card = _card(collab_store)
        with pytest.raises(ValueError, match="CLAIMED"):
            collab_store.update_board_task(card.id, {"status": "claimed"})


class TestPriorityVocabulary:
    def test_unknown_priority_is_refused(self, collab_store: CollabStore) -> None:
        card = _card(collab_store)
        with pytest.raises(ValueError, match="priority must be one of"):
            collab_store.update_board_task(card.id, {"priority": "critical"})
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["priority"] == "normal"

    @pytest.mark.parametrize("priority", ["low", "normal", "high", "urgent"])
    def test_the_closed_vocabulary_writes(self, collab_store: CollabStore, priority: str) -> None:
        card = _card(collab_store)
        assert collab_store.update_board_task(card.id, {"priority": priority})
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["priority"] == priority


class TestOwnerSizeGuard:
    def test_owner_cannot_resize_their_own_card(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = _card(collab_store, owner_employee_id=employees["bob"], size="S")
        with pytest.raises(ValueError, match="owner_cannot_resize"):
            collab_store.update_board_task(card.id, {"size": "L"}, actor=employees["bob"])
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["size"] == "S"

    def test_a_different_principal_may_resize(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = _card(collab_store, owner_employee_id=employees["bob"], size="S")
        assert collab_store.update_board_task(card.id, {"size": "L"}, actor=employees["alice"])

    def test_the_operator_may_resize_their_own_card(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = _card(collab_store, owner_employee_id=employees["owner"], size="S")
        assert collab_store.update_board_task(card.id, {"size": "M"}, actor=employees["owner"])

    def test_the_system_path_is_untouched(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = _card(collab_store, owner_employee_id=employees["bob"], size="S")
        assert collab_store.update_board_task(card.id, {"size": "M"})  # actor='system'

    def test_an_ownerless_card_is_untouched(self, collab_store: CollabStore) -> None:
        card = _card(collab_store, size="S")
        assert collab_store.update_board_task(card.id, {"size": "M"}, actor="emp_bob")


class TestSourceBoundaryImmutable:
    """v4 review MAJOR: the Work/Tasks discriminator must not be PATCH-launderable.

    A Task relabeled as Work mints points for a zero-point card; the reverse
    retroactively erases a verified score. Both directions refuse, for every
    actor, operator included.
    """

    def test_a_task_cannot_be_laundered_into_work(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = BoardTask(title="Minor thing", source="task-adhoc")
        collab_store.create_board_task(card)

        with pytest.raises(ValueError, match="source_boundary_immutable"):
            collab_store.update_board_task(
                card.id, {"source": ""}, actor=employees["owner"]
            )

    def test_work_cannot_be_relabeled_a_task(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = BoardTask(title="Real work")
        collab_store.create_board_task(card)

        with pytest.raises(ValueError, match="source_boundary_immutable"):
            collab_store.update_board_task(
                card.id, {"source": "task-adhoc"}, actor=employees["alice"]
            )

    def test_a_neutral_source_change_still_passes(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        card = BoardTask(title="Imported", source="import-x")
        collab_store.create_board_task(card)

        collab_store.update_board_task(card.id, {"source": "import-y"}, actor=employees["owner"])
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["source"] == "import-y"
