"""AT-02 — Work contract injection.

Every spawned agent is handed its contract through three real artifacts, and
all three are asserted here on their actual rendered text rather than on a mock:

* ``swarm.scheduler.build_worker_brief`` — the prompt string the scheduler
  hands the spawner. It must carry the task, the scope (owned paths), the
  acceptance criteria, the verification gate, and the stop conditions.
* ``swarm.spawn.write_task_md`` — the durable ``var/swarm/<run>/<task>/TASK.md``
  the worker is told to read first (AGENTS.md "Documents Contract").
* ``swarm.contract_bridge.build_task_contract_from_swarm`` +
  ``taskcontract.models.TaskContract`` — the validated, hashed, persisted
  transition contract.

The load-bearing question — *may an agent work without a valid contract?* — is
asked twice: once at the ``TaskContract`` boundary (which really does refuse),
and once at the swarm spawn boundary (which really does not; that is recorded
as a strict xfail, not papered over).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm.contract_bridge import build_task_contract_from_swarm
from omniagentos.swarm.scheduler import build_worker_brief
from omniagentos.swarm.spawn import write_task_md
from omniagentos.taskcontract.models import TaskContract, TaskContractError

RUN = {"id": "run_at2", "working_dir": "/tmp/at2-workspace"}

TASK = {
    "id": "tsk_at2",
    "title": "Build the CSV importer",
    "description": "Parse the vendor CSV into normalized rows.",
}

SWARM_JSON: dict[str, Any] = {
    "task_key": "importer",
    "plan_version": 7,
    "plan_hash": "0123456789abcdef0123",
    "acceptance": "Malformed rows are rejected with a row number.",
    "verify_command": "uv run pytest -q tests/importer",
    "owned_paths": ["src/importer/", "tests/importer/"],
    "complexity": "standard",
    "risk_class": "none",
}


def _brief(**overrides: Any) -> str:
    swarm_json = {**SWARM_JSON, **overrides.pop("swarm_json", {})}
    return build_worker_brief(
        overrides.pop("run", RUN),
        overrides.pop("task", TASK),
        swarm_json,
        overrides.pop("neighbors", {}),
        overrides.pop("subtasks_request_path", None),
    )


# ---------------------------------------------------------------------------
# The brief handed to the adapter
# ---------------------------------------------------------------------------


class TestWorkerBriefCarriesTheContract:
    def test_brief_states_the_task(self) -> None:
        brief = _brief()
        assert "Build the CSV importer" in brief
        assert "Parse the vendor CSV into normalized rows." in brief

    def test_brief_states_the_acceptance_criteria(self) -> None:
        brief = _brief()
        assert "Acceptance: Malformed rows are rejected with a row number." in brief

    def test_brief_states_the_verification_gate(self) -> None:
        brief = _brief()
        assert "Verify: uv run pytest -q tests/importer" in brief

    def test_brief_enumerates_the_allowed_files_as_an_exclusive_scope(self) -> None:
        brief = _brief()
        # Both the paths AND the exclusivity wording matter: a list without
        # "the ONLY files you may create or modify" is a hint, not a scope.
        assert "## Owned paths (the ONLY files you may create or modify)" in brief
        assert "- src/importer/" in brief
        assert "- tests/importer/" in brief

    def test_brief_states_the_stop_conditions(self) -> None:
        brief = _brief()
        assert "## Hard rules" in brief
        assert "Never edit PLAN.md." in brief
        assert "Out-of-scope changes are reverted automatically" in brief
        # Drill-proven failure mode: a chat-only "done" with no file changes.
        assert "A chat-only answer with no file changes is a FAILED attempt." in brief

    def test_shared_directory_worker_is_forbidden_every_git_mutation(self) -> None:
        brief = _brief()
        assert "NEVER run `git add`, `git commit`, or any other git mutation" in brief
        assert "coordinator-owned" in brief

    def test_private_worktree_worker_is_permitted_its_own_commits_only(self) -> None:
        brief = _brief(swarm_json={"worktree_branch": "swarm/run_at2/importer"})
        assert "swarm/run_at2/importer" in brief
        assert "commit your own work freely" in brief
        assert "NEVER push, pull, merge, rebase, switch branches" in brief
        # The shared-directory blanket ban must NOT also be present, or the
        # worker receives two contradictory contracts and freezes.
        assert "NEVER run `git add`, `git commit`, or any other git mutation" not in brief

    def test_brief_binds_the_worker_to_a_plan_version_and_hash(self) -> None:
        # Without this a worker trusts a stale PLAN.md from a superseded plan.
        brief = _brief()
        assert "Plan version 7, hash 0123456789ab" in brief
        assert "trust it only if its hash matches" in brief

    def test_a_worker_with_no_owned_paths_is_told_it_owns_nothing(self) -> None:
        brief = _brief(swarm_json={"owned_paths": []})
        assert "- (none — produce analysis/output only)" in brief

    def test_prior_attempt_feedback_is_delivered_as_a_correction(self) -> None:
        brief = _brief(swarm_json={"feedback": [{"text": "review denied: no tests added"}]})
        assert "## Prior attempt feedback (address this)" in brief
        assert "review denied: no tests added" in brief


# ---------------------------------------------------------------------------
# TASK.md — the durable contract on disk
# ---------------------------------------------------------------------------


class TestTaskMdIsTheDurableContract:
    def _contract(self) -> TaskContract:
        return build_task_contract_from_swarm(
            task=TASK, swarm_json=SWARM_JSON, task_id=str(TASK["id"])
        )

    def test_task_md_renders_every_contract_field(self, tmp_path: Path) -> None:
        contract = self._contract()
        path = write_task_md(
            tmp_path,
            run=RUN,
            task={**TASK, "swarm_json": SWARM_JSON},
            contract=contract,
        )
        text = path.read_text(encoding="utf-8")

        assert path.name == "TASK.md"
        assert text.startswith("# Build the CSV importer")
        assert "## Description\nParse the vendor CSV into normalized rows." in text
        assert "## Owned paths\n- `src/importer/`\n- `tests/importer/`" in text
        assert "- [ ] Malformed rows are rejected with a row number." in text
        assert "## Verify command\n`uv run pytest -q tests/importer`" in text
        assert "This is your work contract." in text

    def test_task_md_records_out_of_scope_paths_and_non_goals(self, tmp_path: Path) -> None:
        swarm_json = {
            **SWARM_JSON,
            "out_of_scope_paths": ["src/legacy/"],
            "non_goals": ["Do not rewrite the exporter."],
        }
        contract = build_task_contract_from_swarm(
            task=TASK, swarm_json=swarm_json, task_id=str(TASK["id"])
        )
        path = write_task_md(
            tmp_path, run=RUN, task={**TASK, "swarm_json": swarm_json}, contract=contract
        )
        text = path.read_text(encoding="utf-8")

        assert "## Out of scope paths\n- `src/legacy/`" in text
        assert "## Non-goals\n- Do not rewrite the exporter." in text

    def test_an_existing_task_md_is_never_overwritten_on_a_retry(self, tmp_path: Path) -> None:
        # A successor attempt must inherit the ORIGINAL contract; silently
        # rewriting it lets a retry move its own goalposts.
        first = write_task_md(tmp_path, run=RUN, task={**TASK, "swarm_json": SWARM_JSON})
        original = first.read_text(encoding="utf-8")

        write_task_md(
            tmp_path,
            run=RUN,
            task={
                "id": TASK["id"],
                "title": "Something else entirely",
                "description": "different",
                "swarm_json": {**SWARM_JSON, "acceptance": "anything goes"},
            },
        )
        assert first.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# No agent may work without a valid contract
# ---------------------------------------------------------------------------


class TestNoWorkWithoutAValidContract:
    def _valid_payload(self) -> dict[str, Any]:
        return {
            "objective": "Build the CSV importer",
            "acceptance_criteria": [{"id": "acceptance", "condition": "rows validate"}],
            "read_set": ["src/importer/"],
            "write_set": ["src/importer/"],
            "risk_class": "R1",
        }

    def test_a_valid_payload_round_trips(self) -> None:
        contract = TaskContract.from_dict(self._valid_payload())
        assert contract.objective == "Build the CSV importer"
        assert len(contract.acceptance_criteria) == 1
        assert contract.contract_hash()

    def test_a_contract_with_no_objective_is_refused(self) -> None:
        payload = self._valid_payload()
        payload["objective"] = "   "
        with pytest.raises(TaskContractError, match="objective must be non-empty"):
            TaskContract.from_dict(payload)

    def test_a_contract_with_no_acceptance_criteria_is_refused(self) -> None:
        payload = self._valid_payload()
        payload["acceptance_criteria"] = []
        with pytest.raises(TaskContractError, match="at least one acceptance criterion"):
            TaskContract.from_dict(payload)

    def test_a_contract_with_a_blank_criterion_condition_is_refused(self) -> None:
        payload = self._valid_payload()
        payload["acceptance_criteria"] = [{"id": "acceptance", "condition": ""}]
        with pytest.raises(TaskContractError, match="condition must be non-empty"):
            TaskContract.from_dict(payload)

    def test_a_contract_with_ambiguous_duplicate_criteria_ids_is_refused(self) -> None:
        payload = self._valid_payload()
        payload["acceptance_criteria"] = [
            {"id": "acceptance", "condition": "a"},
            {"id": "acceptance", "condition": "b"},
        ]
        with pytest.raises(TaskContractError, match="ids must be unique"):
            TaskContract.from_dict(payload)

    def test_a_gate_with_no_command_is_refused(self) -> None:
        payload = self._valid_payload()
        payload["gates"] = [{"command": "  "}]
        with pytest.raises(TaskContractError, match="gate command must be non-empty"):
            TaskContract.from_dict(payload)

    def test_an_empty_contract_document_is_refused(self) -> None:
        with pytest.raises(TaskContractError):
            TaskContract.from_json("{}")

    def test_the_contract_hash_changes_when_the_scope_changes(self) -> None:
        # The hash is what a later transition is checked against; if it ignored
        # scope, a worker could widen write_set mid-flight undetected.
        narrow = TaskContract.from_dict(self._valid_payload())
        widened_payload = self._valid_payload()
        widened_payload["write_set"] = ["src/importer/", "src/"]
        widened = TaskContract.from_dict(widened_payload)
        assert narrow.contract_hash() != widened.contract_hash()

    def test_the_bridge_builds_a_contract_that_binds_scope_and_gate(self) -> None:
        contract = build_task_contract_from_swarm(
            task=TASK, swarm_json=SWARM_JSON, task_id=str(TASK["id"])
        )
        assert contract.write_set == ("src/importer/", "tests/importer/")
        conditions = {c.id: c.condition for c in contract.acceptance_criteria}
        assert conditions["acceptance"] == SWARM_JSON["acceptance"]
        assert (
            conditions["verify_command"]
            == f"verify_command exits 0: {SWARM_JSON['verify_command']}"
        )
        assert all(c.evidence_required for c in contract.acceptance_criteria)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: contract_bridge.build_task_contract_from_swarm MANUFACTURES a contract "
            "for a task that has none — objective falls back to f'swarm task {task_id}' "
            "and a placeholder 'deliver' criterion is synthesized — so a worker with an "
            "empty contract launches with a vacuous one instead of being refused. There "
            "is no spawn-time guard that a swarm task carries real acceptance criteria "
            "and a real verify command. See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_a_swarm_task_with_an_empty_contract_cannot_be_turned_into_one(self) -> None:
        with pytest.raises(TaskContractError):
            build_task_contract_from_swarm(
                task={"id": "tsk_empty", "title": "", "description": ""},
                swarm_json={"acceptance": "", "verify_command": "", "owned_paths": []},
                task_id="tsk_empty",
            )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: build_worker_brief renders 'Acceptance: (none recorded)' and "
            "'Verify: (none)' and hands the worker the prompt anyway. Nothing between "
            "the planner and the adapter refuses to launch an agent whose contract has "
            "no acceptance criteria and no verification gate. "
            "See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_a_worker_brief_is_not_produced_without_acceptance_or_verification(self) -> None:
        brief = build_worker_brief(
            RUN,
            {"id": "tsk_empty", "title": "Untitled", "description": ""},
            {"task_key": "empty", "owned_paths": []},
            {},
            None,
        )
        assert "(none recorded)" not in brief and "Verify: (none)" not in brief, (
            "an agent was briefed with no acceptance criteria and no verify command"
        )
