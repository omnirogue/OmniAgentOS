"""Coverage for the two agent-output redaction SOURCE SEAMS.

Per-writer scrubbing of agent output was proven structurally incomplete over
five review rounds: ``AgentResult`` has six independent construction sites (the
adapters), and the session-capture path never builds an ``AgentResult`` at all,
so every round found one more writer that had been missed.

This file pins the two seams that make the guarantee provable at the source:

1. **AgentResult seam** -- the runner redacts the result the moment it comes
   back from an adapter, so ``runs.output_text``/``output_json``/``error``,
   ``steps.result_json``, the manifest, the vault note and every reviewer prompt
   downstream inherit redacted text from ONE place.
2. **Raw-transcript seam** -- ``sessions/supervisor.py`` and
   ``swarm/provider_exec.py`` redact live CLI stdout at the point it is
   harvested into ``sessions.output_text``/``sessions.error``. That path never
   constructs an ``AgentResult``, so seam 1 cannot cover it.
   ``wrapper/durable.py`` is the third writer of the same class (imported runs
   land in the DB, the ledger JSONL and the vault) and is seamed the same way.

The last section is a REGISTRY test: it reads the source of each seamed module
and fails if a NEW writer of agent output appears that does not route through a
seam. That is what makes the class closed rather than merely patched.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import omniagentos.ledger as ledger_mod
import omniagentos.runner.core as runner_core
import omniagentos.sessions.supervisor as supervisor_mod
import omniagentos.swarm.provider_exec as provider_exec_mod
import omniagentos.vault as vault_mod
import omniagentos.wrapper.durable as durable_mod
from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    AgentResult,
    AgentUsage,
    HarnessProfile,
    HarnessType,
    ResultStatus,
    RunManifest,
    RunState,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.store import SqliteStore
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner
from omniagentos.toolplane.scrub import REDACTED, scrub_agent_result, scrub_run_manifest
from tests.runner.test_state_machine import FinalizationSpy, agent_step, dependencies
from tests.support.db_template import migrated_db

# A credential shape scrub_text already knows; the point of this file is WHERE
# the scrubber runs, not which patterns it carries.
SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"
CLEAN = "ordinary agent output: 12 files changed, all tests green"


def _tree(module: Any) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, str]:
    """Map every node to the name of the nearest enclosing function."""
    owner: dict[ast.AST, str] = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            # ast.walk is breadth-first from `func`, so an inner function
            # visited later overwrites the outer attribution -- which is the
            # attribution we want.
            owner[node] = func.name
    return owner


# ---------------------------------------------------------------------------
# Seam 1 -- the AgentResult receipt boundary
# ---------------------------------------------------------------------------


def test_scrub_agent_result_redacts_every_agent_authored_field() -> None:
    result = AgentResult(
        status=ResultStatus.OK,
        output_text=f"done. key={SECRET}",
        output_json={"key": SECRET, "nested": [{"k": SECRET}]},
        error=f"failed with {SECRET}",
        usage=AgentUsage(wall_ms=1),
    )

    scrubbed = scrub_agent_result(result)

    assert SECRET not in scrubbed.output_text
    assert REDACTED in scrubbed.output_text
    assert SECRET not in json.dumps(scrubbed.output_json)
    assert scrubbed.error is not None and SECRET not in scrubbed.error
    # The original object is not mutated: the seam returns a copy.
    assert SECRET in result.output_text


def test_scrub_agent_result_leaves_clean_output_byte_identical() -> None:
    result = AgentResult(
        status=ResultStatus.OK,
        output_text=CLEAN,
        output_json={"summary": CLEAN},
        usage=AgentUsage(wall_ms=1),
    )

    scrubbed = scrub_agent_result(result)

    assert scrubbed.output_text == CLEAN
    assert scrubbed.output_json == {"summary": CLEAN}
    # Nothing to redact means no copy at all -- the seam is free on the hot path.
    assert scrubbed is result


def test_scrub_agent_result_tolerates_none() -> None:
    assert scrub_agent_result(None) is None


class _SecretAdapter(MockAdapter):
    """An adapter whose output carries a credential, like a real CLI's stdout."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[AgentInput] = []
        self.fail = fail

    def run(self, input: AgentInput) -> AgentResult:
        self.calls.append(input)
        if self.fail:
            return AgentResult(
                status=ResultStatus.ERROR,
                error=f"provider rejected the call: {SECRET}",
                usage=AgentUsage(wall_ms=1),
            )
        return AgentResult(
            status=ResultStatus.OK,
            output_text=f"{CLEAN}\nexport API_KEY={SECRET}",
            output_json={"summary": CLEAN, "token": SECRET},
            usage=AgentUsage(wall_ms=1),
        )


def _seeded_run(store: SqliteStore) -> str:
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "redaction seam",
            "input_json": json.dumps({"prompt": "test", "tools_allowed": []}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "harness": HarnessType.MOCK.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps([agent_step("one")]),
            "budget_json": json.dumps({}),
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    return run_id


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(migrated_db(SqliteStore, tmp_path / "redaction.db"))


def _real_finalization(base: Any) -> Any:
    """The runner's dependencies with the REAL ledger and vault writers.

    The falsifier names the ledger JSONL explicitly, and a spy that never writes
    a file cannot falsify it -- so this test drives the durable writers.
    """
    return replace(
        base,
        append_manifest=ledger_mod.append_manifest,
        render_run_note=vault_mod.render_run_note,
        write_note=vault_mod.write_note,
    )


def test_runner_persists_no_secret_on_any_success_surface(
    store: SqliteStore, tmp_path: Path
) -> None:
    """runs.output_text/output_json, steps.result_json, the ledger and the vault."""
    run_id = _seeded_run(store)
    ledger_dir = tmp_path / "ledger"
    vault_dir = tmp_path / "vault"
    Runner(
        store,
        "w1",
        dependencies=_real_finalization(dependencies(_SecretAdapter(), FinalizationSpy())),
        ledger_dir=str(ledger_dir),
        vault_dir=str(vault_dir),
        workspace_base=str(tmp_path / "ws"),
    ).tick()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == RunState.COMPLETED.value
    assert SECRET not in str(run["output_text"])
    assert REDACTED in str(run["output_text"])
    # The legitimate part of the output survives untouched.
    assert CLEAN in str(run["output_text"])
    assert SECRET not in str(run["output_json"])

    steps = store.get_steps(run_id)
    assert steps, "the agent step must have been persisted"
    assert SECRET not in str(steps[0]["result_json"])

    written = [path for path in (*ledger_dir.rglob("*"), *vault_dir.rglob("*")) if path.is_file()]
    assert written, "finalization must have written the ledger line and the vault note"
    for path in written:
        assert SECRET not in path.read_text(encoding="utf-8"), f"secret reached {path}"


def test_runner_persists_no_secret_on_the_failure_surface(
    store: SqliteStore, tmp_path: Path
) -> None:
    """runs.error via the indirect StepFailure path, and steps.error."""
    run_id = _seeded_run(store)
    Runner(
        store,
        "w1",
        dependencies=dependencies(_SecretAdapter(fail=True), FinalizationSpy()),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "ws"),
    ).tick()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == RunState.FAILED.value
    assert SECRET not in str(run["error"])
    assert REDACTED in str(run["error"])
    assert all(SECRET not in str(step["error"]) for step in store.get_steps(run_id))


# ---------------------------------------------------------------------------
# Seam 2 -- the raw-transcript capture boundary
# ---------------------------------------------------------------------------


class _FakeDal:
    def __init__(self) -> None:
        self.output: list[tuple[str, str | None]] = []
        self.error: list[tuple[str, str | None]] = []

    def set_session_output(self, session_id: str, text: str | None) -> bool:
        self.output.append((session_id, text))
        return True

    def set_session_error(self, session_id: str, text: str | None) -> bool:
        self.error.append((session_id, text))
        return True

    def record_session_error(self, session_id: str, text: str) -> None:
        self.error.append((session_id, text))


def test_provider_exec_persist_helpers_redact() -> None:
    dal = _FakeDal()
    holder = SimpleNamespace(dal=dal)

    provider_exec_mod.ProviderSessionRunner._persist_output(holder, "ses-1", f"{CLEAN}\n{SECRET}")
    provider_exec_mod.ProviderSessionRunner._persist_error(holder, "ses-1", f"boom {SECRET}")

    assert dal.output and SECRET not in str(dal.output[0][1])
    assert CLEAN in str(dal.output[0][1])
    assert dal.error and SECRET not in str(dal.error[0][1])
    # None must stay None -- clearing an error is not a redaction.
    provider_exec_mod.ProviderSessionRunner._persist_output(holder, "ses-1", None)
    assert dal.output[-1][1] is None


def test_supervisor_persist_helpers_redact() -> None:
    dal = _FakeDal()

    supervisor_mod._persist_session_output(dal, "ses-1", f"{CLEAN} {SECRET}")
    supervisor_mod._persist_session_error(dal, "ses-1", f"crashed: {SECRET}")

    assert dal.output and SECRET not in str(dal.output[0][1])
    assert CLEAN in str(dal.output[0][1])
    assert dal.error and SECRET not in str(dal.error[0][1])


def test_supervisor_spawn_failure_audit_event_is_redacted() -> None:
    """The spawn-failure AUDIT EVENT is a second surface, seamed separately."""
    dal = _FakeDal()
    supervisor_mod._record_session_error(dal, "ses-1", f"spawn blew up: {SECRET}")
    assert dal.error and SECRET not in str(dal.error[0][1])


def test_supervisor_auth_failure_detail_is_redacted_at_capture() -> None:
    """The auth detail is raw CLI text and reaches sessions.error AND accounts."""
    event = {
        "type": "system",
        "subtype": "api_retry",
        "error_status": 401,
        "error": f"invalid api key {SECRET}",
    }
    detail = supervisor_mod._auth_failure_detail(event)
    assert detail is not None and SECRET not in detail


def test_wrapper_durable_finalize_redacts_db_ledger_and_vault(tmp_path: Path) -> None:
    manifest = RunManifest(
        run_id=new_id("run"),
        task_id=new_id("tsk"),
        harness=HarnessProfile(harness=HarnessType.MOCK),
        state=RunState.COMPLETED,
        finished_at=utc_now_iso(),
        extra={"error": f"boom {SECRET}", "raw_stdout": f"{CLEAN} {SECRET}"},
    )
    ledger_dir = tmp_path / "ledger"
    vault_dir = tmp_path / "vault"

    manifest_path, note_path = durable_mod.finalize_manifest(
        manifest,
        task_title="import",
        output_text=f"{CLEAN}\n{SECRET}",
        ledger_dir=str(ledger_dir),
        vault_dir=str(vault_dir),
        db_path=str(tmp_path / "durable.db"),
    )

    ledger_text = Path(manifest_path).read_text(encoding="utf-8")
    assert SECRET not in ledger_text
    assert REDACTED in ledger_text
    assert note_path is not None and SECRET not in Path(note_path).read_text(encoding="utf-8")

    store = SqliteStore(str(tmp_path / "durable.db"))
    row = store.get_run(manifest.run_id)
    assert row is not None
    assert SECRET not in str(row["output_text"])
    assert CLEAN in str(row["output_text"])
    assert SECRET not in str(row["output_json"])
    assert SECRET not in str(row["error"])
    # The caller's manifest object is not mutated by the seam.
    assert SECRET in str(manifest.extra["error"])


def test_scrub_run_manifest_leaves_a_clean_manifest_identical() -> None:
    manifest = RunManifest(
        run_id="run-1",
        task_id="tsk-1",
        harness=HarnessProfile(harness=HarnessType.MOCK),
        state=RunState.COMPLETED,
        extra={"summary": CLEAN},
    )
    assert scrub_run_manifest(manifest) is manifest


# ---------------------------------------------------------------------------
# The registry -- a NEW writer that skips a seam fails here
# ---------------------------------------------------------------------------

#: DAL methods that persist agent-authored text on a session row. Any call to
#: one of these (by attribute or by ``getattr`` name) must live inside a seam.
SESSION_WRITERS = frozenset({"set_session_output", "set_session_error", "record_session_error"})

#: The functions allowed to call them, per module.
SESSION_SEAMS: dict[str, frozenset[str]] = {
    "omniagentos/sessions/supervisor.py": frozenset(
        {"_persist_session_output", "_persist_session_error", "_record_session_error"}
    ),
    "omniagentos/swarm/provider_exec.py": frozenset({"_persist_output", "_persist_error"}),
}


@pytest.mark.parametrize(
    "module",
    [supervisor_mod, provider_exec_mod],
    ids=["supervisor", "provider_exec"],
)
def test_every_session_output_writer_routes_through_a_seam(module: Any) -> None:
    relpath = next(key for key in SESSION_SEAMS if Path(module.__file__).as_posix().endswith(key))
    seams = SESSION_SEAMS[relpath]
    tree = _tree(module)
    owner = _enclosing_functions(tree)

    offenders: list[str] = []
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.Attribute) and node.attr in SESSION_WRITERS:
            name = node.attr
        elif isinstance(node, ast.Constant) and node.value in SESSION_WRITERS:
            name = str(node.value)
        if name is None:
            continue
        enclosing = owner.get(node, "<module>")
        if enclosing not in seams:
            offenders.append(f"{name} called from {enclosing}")

    assert not offenders, (
        f"{relpath}: agent output reaches a session row outside the redaction seam "
        f"({sorted(set(offenders))}). Route the write through {sorted(seams)} so it "
        "inherits redaction, or extend the seam registry deliberately."
    )


def test_every_adapter_result_is_scrubbed_at_the_runner_boundary() -> None:
    """Seam 1 is structural: no ``adapter.run(...)`` may escape unscrubbed."""
    tree = _tree(runner_core)
    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "scrub_agent_result"
        ):
            wrapped.update(id(arg) for arg in node.args)

    invocations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and "adapter" in node.func.value.id
    ]
    assert invocations, "the runner must still invoke an adapter"
    unwrapped = [node.lineno for node in invocations if id(node) not in wrapped]
    assert not unwrapped, (
        "runner/core.py: adapter results at lines "
        f"{unwrapped} are persisted without passing through scrub_agent_result()"
    )


def test_durable_finalization_scrubs_before_it_writes() -> None:
    tree = _tree(durable_mod)
    finalize = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "finalize_manifest"
    )
    called = {
        node.func.id
        for node in ast.walk(finalize)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"scrub_run_manifest", "scrub_optional_text"} <= called, (
        "wrapper/durable.py: finalize_manifest writes the DB, the ledger and the vault; "
        "it must redact the manifest and the output text before any of the three"
    )


def test_seam_helpers_are_the_shared_scrubber() -> None:
    """No second scrubber: the seams reuse toolplane/scrub.py (proposal constraint)."""
    from omniagentos.toolplane import scrub as scrub_mod

    assert scrub_agent_result.__module__ == scrub_mod.__name__
    assert scrub_run_manifest.__module__ == scrub_mod.__name__
    for module in (supervisor_mod, provider_exec_mod, durable_mod, runner_core):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "omniagentos.toolplane.scrub" in source, (
            f"{module.__name__} must reuse the shared scrubber, not define its own"
        )


def test_action_class_import_is_used() -> None:
    # Keeps the seeded-run helper honest about the plan shape it builds.
    assert agent_step("x")["action_class"] == ActionClass.SANDBOXED_CREATION.value
