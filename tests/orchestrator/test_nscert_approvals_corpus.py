"""LUNA-owned approval corpus additions; the existing corpus remains untouched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.orchestrator.approvals import classify_hard_stop
from omniagentos.orchestrator.contracts import ApprovalRequest

CORPUS = Path(__file__).parent / "approvals_corpus" / "nscert-luna.json"


def _rows(key: str):
    return json.loads(CORPUS.read_text(encoding="utf-8"))[key]


@pytest.mark.parametrize("row", _rows("must_park"), ids=lambda row: row["id"])
def test_nscert_unsafe_rows_park(row: dict[str, str]) -> None:
    request = ApprovalRequest(
        proposed_action=row["request"],
        action_class="consequential",
        tool_name="Bash",
        tool_input={"command": row["request"]},
    )
    assert classify_hard_stop(request) == row["category"]


@pytest.mark.parametrize("row", _rows("must_auto_approve"), ids=lambda row: row["id"])
def test_nscert_benign_rows_have_no_hard_stop(row: dict[str, str]) -> None:
    request = ApprovalRequest(
        proposed_action=row["request"],
        action_class="consequential",
        tool_name="Bash",
        tool_input={"command": row["request"]},
    )
    assert classify_hard_stop(request) is None
