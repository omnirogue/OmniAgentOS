"""resolve_task: exact ref (any owner, gated), title-prefix (sender's own only)."""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.slack_updates import NOT_OWNED, AmbiguousMatch, resolve_task


class TestExactRefMatch:
    def test_owner_resolves_their_own_ref(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Ship it", ref="U3", owner_employee_id=employees["bob"])
        found = resolve_task(collab_store, "U3", employees["bob"])
        assert found is not None
        assert found["id"] == card.id

    def test_ref_match_is_case_insensitive(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Ship it", ref="U3", owner_employee_id=employees["bob"])
        found = resolve_task(collab_store, "u3", employees["bob"])
        assert found is not None
        assert found["id"] == card.id

    def test_u3_never_matches_u33(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        make_card(title="Card three", ref="U3", owner_employee_id=employees["bob"])
        make_card(title="Card thirty-three", ref="U33", owner_employee_id=employees["bob"])
        found = resolve_task(collab_store, "U3", employees["bob"])
        assert found is not None
        assert found["ref"] == "U3"

    def test_cross_owner_is_refused_for_a_non_owner_sender(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        make_card(title="Alice's card", ref="U9", owner_employee_id=employees["alice"])
        assert resolve_task(collab_store, "U9", employees["bob"]) == NOT_OWNED

    def test_emp_owner_overrides_ownership(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Alice's card", ref="U9", owner_employee_id=employees["alice"])
        found = resolve_task(collab_store, "U9", employees["owner"])
        assert found is not None
        assert found["id"] == card.id

    def test_an_unowned_card_returns_not_owned_for_non_owner(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        make_card(title="Agent card", ref="AGT-1")
        assert resolve_task(collab_store, "AGT-1", employees["bob"]) == NOT_OWNED

    def test_emp_owner_can_resolve_an_unowned_card(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Agent card", ref="AGT-1")
        found = resolve_task(collab_store, "AGT-1", employees["owner"])
        assert found is not None
        assert found["id"] == card.id

    def test_no_matching_ref_is_none(self, collab_store: CollabStore, employees: dict) -> None:
        assert resolve_task(collab_store, "U404", employees["bob"]) is None


class TestTitlePrefixMatch:
    def test_a_single_prefix_hit_among_the_senders_own_tasks(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Fix login bug", owner_employee_id=employees["bob"])
        make_card(title="Something else", owner_employee_id=employees["bob"])
        found = resolve_task(collab_store, "Fix login", employees["bob"])
        assert found is not None
        assert found["id"] == card.id

    def test_prefix_match_is_case_insensitive(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Fix Login Bug", owner_employee_id=employees["bob"])
        found = resolve_task(collab_store, "fix login", employees["bob"])
        assert found is not None
        assert found["id"] == card.id

    def test_two_hits_are_ambiguous(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        make_card(title="Fix login bug", owner_employee_id=employees["bob"])
        make_card(title="Fix login redirect", owner_employee_id=employees["bob"])
        result = resolve_task(collab_store, "Fix login", employees["bob"])
        assert isinstance(result, AmbiguousMatch)
        assert len(result.candidates) == 2

    def test_title_prefix_never_reaches_another_persons_cards(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        make_card(title="Fix login bug", owner_employee_id=employees["alice"])
        assert resolve_task(collab_store, "Fix login", employees["bob"]) is None

    def test_title_prefix_excludes_terminal_cards(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask], employees: dict
    ) -> None:
        card = make_card(title="Old finished work", owner_employee_id=employees["bob"])
        collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
        assert resolve_task(collab_store, "Old finished", employees["bob"]) is None

    def test_no_hit_is_none(self, collab_store: CollabStore, employees: dict) -> None:
        assert resolve_task(collab_store, "Nothing here", employees["bob"]) is None


class TestEmptyInput:
    def test_blank_ref_resolves_to_none(self, collab_store: CollabStore, employees: dict) -> None:
        assert resolve_task(collab_store, "", employees["bob"]) is None
        assert resolve_task(collab_store, None, employees["bob"]) is None
