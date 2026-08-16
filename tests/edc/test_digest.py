"""EDC morning-digest rendering (synthesis §11, P1) — owner-scoped, non-URGENT."""

from __future__ import annotations

from omniagentos.edc.digest import render_owner_section
from tests.edc.conftest import make_decision


def test_empty_section_is_none(decisions, employees) -> None:
    assert render_owner_section(decisions, "emp_owner") is None


def test_section_lists_needs_owner_with_recommended_action(decisions, employees) -> None:
    decisions.create_decision(
        make_decision(
            source_ref="m1",
            title="Update your payment method",
            classification="needs_owner",
            recommended={"kind": "update_payment", "human_line": "Update the card on AWS"},
            deadline_at="2026-08-20T17:00:00Z",
        )
    )
    section = render_owner_section(decisions, "emp_owner")
    assert section is not None
    assert "Update the card on AWS" in section
    assert "⏰ 2026-08-20T17:00:00Z" in section


def test_section_summarizes_maybe_items(decisions, employees) -> None:
    for i in range(3):
        decisions.create_decision(
            make_decision(source_ref=f"mb{i}", title=f"Ambiguous {i}", classification="maybe")
        )
    section = render_owner_section(decisions, "emp_owner")
    assert section is not None
    assert "3 to review" in section


def test_section_is_owner_scoped(decisions, employees) -> None:
    decisions.create_decision(
        make_decision(owner_employee_id="emp_bob", source_ref="s1", classification="needs_owner")
    )
    assert render_owner_section(decisions, "emp_owner") is None
    assert render_owner_section(decisions, "emp_bob") is not None
