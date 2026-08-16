"""Regressions for the three AUTO-mode loosenings found in review at 6d66877.

Each test encodes a bypass that was live and demonstrable, so a future
'operator decision' commit cannot silently reopen it.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy import evaluate_action, load_policy
from omniagentos.policy.shell import classify_shell


@pytest.fixture
def workspace() -> str:
    return tempfile.mkdtemp()


@pytest.mark.parametrize(
    "target",
    [
        "/Users/youruser/OmniAgentOS/worktrees/main",  # sibling PRODUCT worktree
        "/Users/youruser/OmniAgentOS/var/projects/alpha",
        "/Users/youruser/OmniAgentOS/var/runs/run_abc",
        "/Users/youruser/Desktop/worktrees/keepme",  # unrelated user dir
    ],
)
def test_recursive_delete_outside_project_never_auto_runs(workspace: str, target: str) -> None:
    assert classify_shell(f"rm -rf {target}", project_dir=workspace) is ActionClass.IRREVERSIBLE


def test_scratch_delete_inside_project_still_auto_runs(workspace: str) -> None:
    """The ergonomic case the marker list exists for must keep working."""
    scratch = os.path.join(workspace, "var", "runs", "run_abc")
    os.makedirs(scratch, exist_ok=True)
    assert (
        classify_shell(f"rm -rf {scratch}", project_dir=workspace)
        is ActionClass.INTERNAL_REVERSIBLE
    )


@pytest.mark.parametrize(
    "payload",
    [
        "import os; os.system('id')",
        "import os as o; o.system('id')",  # the alias bypass
        "import os as o; o.remove('/Users/youruser/OmniAgentOS/omniagentos/api/main.py')",
        "getattr(__import__('os'),'system')('id')",  # attribute-indirection
        "exec(__import__('base64').b64decode('aWQ='))",  # encoded payload
    ],
)
def test_inline_interpreter_payloads_are_always_irreversible(workspace: str, payload: str) -> None:
    cmd = f'python3 -c "{payload}"'
    assert classify_shell(cmd, project_dir=workspace) is ActionClass.IRREVERSIBLE


def test_clustered_inline_flag_is_caught(workspace: str) -> None:
    assert (
        classify_shell('python3 -Sc "import os"', project_dir=workspace) is ActionClass.IRREVERSIBLE
    )


def test_script_outside_project_is_irreversible(workspace: str) -> None:
    assert classify_shell("python3 /etc/evil.py", project_dir=workspace) is ActionClass.IRREVERSIBLE


def test_in_scope_script_still_auto_runs(workspace: str) -> None:
    """Positive path: a normal script inside the workspace is not over-tightened."""
    script = os.path.join(workspace, "ok.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("print(1)\n")
    assert (
        classify_shell(f"python3 {script}", project_dir=workspace)
        is ActionClass.INTERNAL_REVERSIBLE
    )


def test_consequential_auto_runs_under_auto_mode() -> None:
    """Grok product stance: full-auto = irreversible + finance only.

    CONSEQUENTIAL auto-executes under AUTO. Money/HARD_HUMAN still refuse via
    the broker with a *store-loaded* grant (never a fabricated grant_row).
    Grep anchor: ``AUTO mode gate: consequential``.
    """
    decision = evaluate_action(ActionClass.CONSEQUENTIAL, load_policy())
    assert decision.requires_approval is False
    assert decision.always_human is False
    assert "AUTO mode gate: consequential" in decision.reason


def test_irreversible_hard_stop_is_never_relaxed() -> None:
    """Pin the floor itself. A sibling fork disabled this class for 84 minutes."""
    decision = evaluate_action(ActionClass.IRREVERSIBLE, load_policy())
    assert decision.requires_approval is True
    assert decision.always_human is True
