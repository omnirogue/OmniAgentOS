"""Provisioning wired into the intake dispatch path.

A provisioned dispatch scopes the run's working dir + connectors to EXACTLY the
provisioned project — and, being tools-capable, still parks behind the runner's
approval gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Import the api package first to break a pre-existing intake<->api import cycle
# that only bites when intake is imported first (see tests/provision/test_service).
import omniagentos.api.main  # noqa: F401,E402  (import-order guard)
from omniagentos.collab.store import CollabStore  # noqa: E402
from omniagentos.contracts import HarnessType
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import _INTAKE_TOOLS, dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.projects import ProjectStore


def _spec() -> RefinedSpec:
    return RefinedSpec(
        title="Reconcile payouts",
        description="Reconcile Stripe payouts against the ledger.",
        acceptance_criteria=["reconciled"],
    )


def _llm(connectors: list[str]) -> Any:
    def run(_prompt: str, _schema: dict[str, Any]) -> dict[str, Any]:
        return {"existing_project": None, "connectors": connectors, "reason": "stub"}

    return run


def test_dispatch_provision_scopes_run_to_provisioned_dir(
    tmp_path: Path,
    provision_var_dir: Path,
) -> None:
    db = str(tmp_path / "prov.db")
    collab = CollabStore(db)
    store = collab._store
    cfg = load_policy()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        provision=True,
        provision_llm=_llm(["stripe_acmeuni.read"]),
    )

    # Provisioning happened, implying tools mode.
    assert result["provisioned"] is True
    assert result["execute"] == "tools"
    project_id = result["project_id"]
    assert project_id and str(project_id).startswith("proj")

    # The run's working dir IS the provisioned project's scoped workspace, and
    # nothing else.
    expected = provision_var_dir / "projects" / str(project_id)
    assert Path(result["working_dir"]) == expected
    assert expected.is_dir()

    # The run is scoped to EXACTLY the workspace primitives + provisioned connector.
    assert result["allowed_connectors"] == ["stripe_acmeuni.read"]
    task = store.get_task(result["task_id"])
    assert task is not None
    assert json.loads(task["input_json"])["tools_allowed"] == [
        *_INTAKE_TOOLS,
        "stripe_acmeuni.read",
    ]

    # The run plan's agent step is scoped to the provisioned working dir.
    run = store.get_run(result["run_id"])
    assert run is not None
    plan = json.loads(run["plan_json"])
    agent_step = next(s for s in plan if s["kind"] == "agent")
    assert agent_step["params"]["working_dir"] == str(expected)
    assert agent_step["action_class"] == "consequential"  # parks for approval

    # The project row records the provisioned scope (dirs + APIs).
    project = ProjectStore(store).get_project(str(project_id))
    assert project is not None
    assert project["root_dirs"] == [str(expected)]
    assert "stripe_acmeuni.read" in project["allowed_tools"]


def test_dispatch_without_provision_unchanged(tmp_path: Path) -> None:
    db = str(tmp_path / "plain.db")
    collab = CollabStore(db)
    store = collab._store
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=HarnessType.MOCK.value)
    # Default path is untouched: readonly, no provisioning, no scoped connectors.
    assert result["execute"] == "readonly"
    assert result["provisioned"] is False
    assert result["allowed_connectors"] == []
    assert result["working_dir"] is None
