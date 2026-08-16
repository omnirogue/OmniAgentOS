"""Unit tests for LANE B additions: write_task_md and worker_context_block."""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.swarm.metacog_context import worker_context_block
from omniagentos.swarm.spawn import write_task_md
from omniagentos.taskcontract.models import AcceptanceCriterion, Budgets, RiskClass, TaskContract


def test_write_task_md_writes_once_and_never_overwrites(tmp_path: Path) -> None:
    task = {
        "id": "tsk_123",
        "title": "My Task Title",
        "description": "My Task Description",
        "swarm_json": json.dumps(
            {
                "depends_on": ["tsk_abc"],
                "owned_paths": ["src/lib.py"],
                "complexity": "complex",
                "risk_class": "deploy",
                "acceptance": "Ensure unit tests pass.",
                "verify_command": "pytest tests/lib",
                "category": "core",
            }
        ),
    }

    # 1. Write first time
    task_md_path = write_task_md(tmp_path, run=None, task=task, contract=None)
    assert task_md_path.exists()
    assert task_md_path.name == "TASK.md"

    content_first = task_md_path.read_text(encoding="utf-8")
    assert "My Task Title" in content_first
    assert "My Task Description" in content_first
    # Was `assert "complexity: deploy" or "Risk class: deploy" in content_first`,
    # which `or` binds looser than `in`, so it read as `assert "complexity: deploy"`
    # — a non-empty literal that passes for ANY file content, including an empty
    # one. Both operands were also wrong: TASK.md renders "- **Complexity**:
    # complex" and "- **Risk class**: deploy", so "complexity: deploy" never
    # appears. Assert the two lines the fixture actually sets, separately.
    assert "- **Risk class**: deploy" in content_first
    assert "- **Complexity**: complex" in content_first
    assert "Ensure unit tests pass." in content_first
    assert "pytest tests/lib" in content_first

    # 2. Modify task description and attempt to write again (should NOT overwrite)
    task_modified = {
        **task,
        "description": "DIFFERENT DESCRIPTION",
    }
    write_task_md(tmp_path, run=None, task=task_modified, contract=None)
    content_second = task_md_path.read_text(encoding="utf-8")
    assert content_second == content_first
    assert "DIFFERENT DESCRIPTION" not in content_second


def test_write_task_md_with_contract(tmp_path: Path) -> None:
    task = {
        "id": "tsk_456",
        "title": "Task with Contract",
        "description": "Original Description",
    }

    contract = TaskContract(
        objective="Contract Objective",
        acceptance_criteria=(
            AcceptanceCriterion(
                id="c1", condition="Acceptance from contract 1", evidence_required=True
            ),
        ),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R2,
        budgets=Budgets(max_tokens=5000, max_cost_usd=2.5),
    )

    task_md_path = write_task_md(tmp_path, run=None, task=task, contract=contract)
    assert task_md_path.exists()
    content = task_md_path.read_text(encoding="utf-8")

    assert "Acceptance from contract 1" in content
    assert "Max Tokens**: 5000" in content
    assert "Max Cost (USD)**: 2.5" in content


def test_worker_context_block_never_raises_on_empty_broken_store() -> None:
    # Under a clean test setup, without explicit database seed/memories,
    # compile_context will return a ContextPacket but with no actual memory/artifacts match.
    # Our optimized helper returns "" if there are no matching items.
    block = worker_context_block(
        task_title="Some Task",
        task_description="Some Description",
        project_id="nonexistent_project",
    )
    assert isinstance(block, str)
    # Since there are no matched artifacts/memories, it should be empty
    assert block == ""


def test_write_task_md_verify_fallback_to_contract(tmp_path: Path) -> None:
    task = {
        "id": "tsk_verify_fallback",
        "title": "Fallback Task",
        "description": "Fallback Description",
        "swarm_json": json.dumps(
            {
                "verify_command": "",
            }
        ),
    }
    contract = TaskContract(
        objective="Contract Objective",
        acceptance_criteria=(
            AcceptanceCriterion(
                id="verify_command",
                condition="verify_command exits 0: my_contract_test --verbose",
                evidence_required=True,
            ),
        ),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
    )
    sub_dir = tmp_path / "fallback"
    task_md_path = write_task_md(sub_dir, run=None, task=task, contract=contract)
    assert task_md_path.exists()
    content = task_md_path.read_text(encoding="utf-8")

    assert "my_contract_test --verbose" in content
    assert "`None`" not in content.split("## Verify command")[1]


def test_write_task_md_verify_spec_wins(tmp_path: Path) -> None:
    task = {
        "id": "tsk_spec_wins",
        "title": "Spec Wins Task",
        "description": "Spec Wins Description",
        "swarm_json": json.dumps(
            {
                "verify_command": "pytest tests/wins",
            }
        ),
    }
    contract = TaskContract(
        objective="Contract Objective",
        acceptance_criteria=(
            AcceptanceCriterion(
                id="verify_command",
                condition="verify_command exits 0: contract_loses",
                evidence_required=True,
            ),
        ),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
    )
    sub_dir = tmp_path / "spec_wins"
    task_md_path = write_task_md(sub_dir, run=None, task=task, contract=contract)
    assert task_md_path.exists()
    content = task_md_path.read_text(encoding="utf-8")

    # The spec's command wins in the "## Verify command" section. The contract's
    # criterion still appears in the acceptance checklist as prose -- long-standing
    # behavior, deliberately not asserted away here -- so scope the check to the
    # section under test rather than the whole document.
    verify_section = content.split("## Verify command", 1)[1]
    assert "pytest tests/wins" in verify_section
    assert "contract_loses" not in verify_section


def test_write_task_md_verify_neither(tmp_path: Path) -> None:
    task = {
        "id": "tsk_neither",
        "title": "Neither Task",
        "description": "Neither Description",
        "swarm_json": json.dumps(
            {
                "verify_command": "",
            }
        ),
    }
    contract = TaskContract(
        objective="Contract Objective",
        acceptance_criteria=(
            AcceptanceCriterion(id="deliver", condition="complete task", evidence_required=True),
        ),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
    )
    sub_dir = tmp_path / "neither"
    task_md_path = write_task_md(sub_dir, run=None, task=task, contract=contract)
    assert task_md_path.exists()
    content = task_md_path.read_text(encoding="utf-8")

    assert "`None`" in content.split("## Verify command")[1]


def test_write_task_md_extra_sections(tmp_path: Path) -> None:
    task = {
        "id": "tsk_extra_sections",
        "title": "Extra Sections Task",
        "description": "Extra Sections Description",
    }
    contract = TaskContract(
        objective="Contract Objective",
        acceptance_criteria=(
            AcceptanceCriterion(id="deliver", condition="complete task", evidence_required=True),
        ),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
        out_of_scope_paths=("omniagentos/critical_file.py", "tests/secrets/"),
        non_goals=("no refactoring", "no dependency updates"),
    )
    sub_dir = tmp_path / "extras"
    task_md_path = write_task_md(sub_dir, run=None, task=task, contract=contract)
    assert task_md_path.exists()
    content = task_md_path.read_text(encoding="utf-8")

    assert "## Out of scope paths" in content
    assert "- `omniagentos/critical_file.py`" in content
    assert "- `tests/secrets/`" in content
    assert "## Non-goals" in content
    assert "- no refactoring" in content
    assert "- no dependency updates" in content


def test_write_task_md_no_extra_sections_when_unset(tmp_path: Path) -> None:
    task = {
        "id": "tsk_no_extras",
        "title": "No Extras Task",
        "description": "No Extras Description",
    }
    contract = TaskContract(
        objective="Contract Objective",
        acceptance_criteria=(
            AcceptanceCriterion(id="deliver", condition="complete task", evidence_required=True),
        ),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
    )
    sub_dir = tmp_path / "no_extras"
    task_md_path = write_task_md(sub_dir, run=None, task=task, contract=contract)
    assert task_md_path.exists()
    content = task_md_path.read_text(encoding="utf-8")

    assert "## Out of scope paths" not in content
    assert "## Non-goals" not in content


def test_task_md_states_the_risk_class_the_platform_enforces(tmp_path: Path) -> None:
    """TASK.md must not tell a worker `none` for a class the platform denies.

    `write_task_md` renders the document line 621 of spawn.py calls "your work
    contract". Every other reader of the same stored ``swarm_json.risk_class``
    normalizes before comparing — ``spawn.py:1033`` (the path to
    ``_spawn_provider``), ``router.py:953`` (the claude-only risk pin) and
    ``contract_bridge._risk`` (the adopted TaskContract's RiskClass). This one
    did not, so an in-union value that merely differed in case or surrounding
    whitespace fell into the ``else "none"`` arm and the worker was told its
    destructive task was risk-free while the platform enforced R3 against it.

    Keyed on the CANONICAL union via ``get_args``, not a hand-written list: a
    new member of ``swarm.contracts.RiskClass`` is covered by construction. A
    literal list of four has the same failure mode as the comparison it tests.
    """
    from typing import get_args

    from omniagentos.swarm.contracts import RiskClass as SwarmRiskClass
    from omniagentos.swarm.provider_exec import DENIED_RISK_CLASSES

    union = get_args(SwarmRiskClass)
    assert union, "canonical swarm RiskClass union is empty — the test would be vacuous"

    def render(stored_value: str, slug: str) -> str:
        task = {
            "id": f"tsk_{slug}",
            "title": "Delete the production bucket",
            "description": "remove it",
            "swarm_json": json.dumps({"risk_class": stored_value}),
        }
        path = write_task_md(tmp_path / slug, run=None, task=task, contract=None)
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("- **Risk class**:")
        ]
        assert len(lines) == 1, f"expected one risk line, got {lines!r}"
        return lines[0].split(":", 1)[1].strip()

    for index, member in enumerate(union):
        for variant_index, stored in enumerate(
            (member, member.upper(), member.capitalize(), f" {member}", f"{member} ")
        ):
            slug = f"{index}_{variant_index}"
            # What every enforcing reader resolves this stored value to.
            enforced = str(stored).strip().lower()
            assert render(stored, slug) == enforced, (
                f"TASK.md stated {render(stored, slug)!r} for stored {stored!r}, but the "
                f"spawn/router/contract readers all enforce {enforced!r}"
                + (" — a DENIED class" if enforced in DENIED_RISK_CLASSES else "")
            )
