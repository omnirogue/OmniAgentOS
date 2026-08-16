"""Fixtures for the orchestration acceptance suite.

Hermetic by construction:

* ``_block_outbound_network`` (autouse) makes any socket connect raise, so a
  test that silently reached a live model/registry fails loudly instead of
  passing slowly. ``git`` subprocesses are unaffected (they are local and never
  touch a Python socket), and loopback stays open because ``httpx``/ASGI test
  clients in sibling suites use it.
* Every store fixture is rooted in ``tmp_path``; nothing here can reach
  ``var/omniagentos.db`` (the root ``tests/conftest.py`` also pins
  ``OMNIAGENTOS_DB`` at session scope, this is the second belt).
"""

from __future__ import annotations

import socket
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import omniagentos.api.main  # noqa: F401,E402 -- side-effect import: breaks the intake<->api cycle
from omniagentos.collab.store import CollabStore
from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.store import SqliteReliabilityStore

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})


@pytest.fixture(autouse=True)
def _block_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast on any non-loopback socket connect."""

    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and host in _LOOPBACK:
            real_connect(self, address)  # type: ignore[arg-type]
            return
        raise AssertionError(
            f"acceptance tests are hermetic; outbound connect to {address!r} is not allowed"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def migrated_db_path(tmp_path: Path) -> str:
    """A migrated, empty control-plane database under ``tmp_path``."""

    path = tmp_path / "acceptance.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    return str(path)


@pytest.fixture
def reliability_store(migrated_db_path: str) -> Iterator[SqliteReliabilityStore]:
    """The org-hierarchy store (org_units + agents)."""

    store = SqliteReliabilityStore(migrated_db_path)
    yield store
    store._connection.close()


@pytest.fixture
def collab_store(migrated_db_path: str) -> CollabStore:
    """The collab registry / board store over the same migrated database."""

    return CollabStore(migrated_db_path)


@pytest.fixture
def vault_dir(tmp_path: Path) -> str:
    """An isolated vault root so org seeding never writes into the checkout."""

    path = tmp_path / "vault"
    path.mkdir()
    return str(path)


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """A real, committed git repository — the substrate a worktree needs."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(workspace), env=env, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "acceptance@example.invalid")
    git("config", "user.name", "Acceptance Suite")
    git("config", "commit.gpgsign", "false")
    (workspace / "README.md").write_text("acceptance workspace\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "seed")
    return workspace


# --- fixtures merged from the AT4 acceptance area (11, 13, 14, 15) ---


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Pin every filesystem root the lab/learning subsystems write to."""
    roots = {
        "ledger": tmp_path / "ledger",
        "vault": tmp_path / "vault",
        "workspace": tmp_path / "runs",
        "reflexion": tmp_path / "reflexion",
        "skills": tmp_path / "skills",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(roots["ledger"]))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(roots["vault"]))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(roots["workspace"]))
    return roots


@pytest.fixture
def migrated_db(tmp_path: Path) -> str:
    """A freshly migrated control-plane SQLite DB at (at least) migration 083."""
    from omniagentos.db.migrate import migrate

    db_path = str(tmp_path / "acceptance.db")
    version = migrate(db_path)
    assert version >= 83, f"acceptance suite needs migration 083+, got {version}"
    return db_path


@pytest.fixture
def offline_lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_roots: dict[str, Path]
) -> Iterator[tuple[Any, Any, str]]:
    """A real, fully offline lab loop: LabStore + ProtectedEvaluator + MockAdapter.

    Nothing here is a stand-in for the code under test -- the store, grader,
    evaluator, scorer and promotion gate are the production objects. Only the
    *model call* is faked: ``MockAdapter.run`` is patched to answer from the
    prompt text, so a challenger prompt containing ``WINNING`` is genuinely more
    accurate than the ``BASELINE`` champion and the numbers the gate sees are
    produced by the real grader.

    Yields ``(store, evaluator, suite_id)``.
    """
    import omniagentos.lab.executor as lab_executor
    from omniagentos.lab import surfaces
    from omniagentos.lab.contracts import EvalCase, EvalSplit, EvalSuite, MetricSpec
    from omniagentos.lab.eval.evaluator import ProtectedEvaluator
    from omniagentos.lab.eval.grader import ProtectedGrader
    from omniagentos.lab.store import LabStore
    from omniagentos.mock_adapter import MockAdapter

    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(lab_executor, "_repo_root", lambda: tmp_path)

    # The provenance-bearing subclass (omniagentos.lab.store), not the base
    # omniagentos.lab.db one: campaign's `_provenance_evidence` only finds
    # judge evidence on a store exposing `get_verdict_provenance`.
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite = EvalSuite(
        id="evs_at4",
        discipline="at4",
        metrics=[MetricSpec(name="accuracy", role="primary")],
        dataset_hash="at4-v1",
    )
    store.create_eval_suite(suite)
    for split in (EvalSplit.DEV, EvalSplit.HELD_OUT):
        for number in range(1, 5):
            case_id = f"evc_{split.value}_{number}"
            expected = {"value": f"answer-{number}"}
            store.add_eval_case(
                EvalCase(
                    id=case_id,
                    suite=suite.id,
                    split=split,
                    input={"number": number},
                    # held_out `expected` is deliberately withheld from the shared
                    # store (HD-001); only the protected grader holds it.
                    expected=expected if split == EvalSplit.DEV else None,
                    rubric="Return the exact answer.",
                )
            )
            grader.put_expected(case_id, expected)

    original_run = MockAdapter.run

    def prompt_sensitive_run(self: MockAdapter, agent_input: Any) -> Any:
        """The ONLY faked boundary: the model call."""
        executor = agent_input.metadata["executor"]
        prompt = str(executor["system_prompt"])
        number = int(executor["case_input"]["number"])
        correct_through = 3 if "WINNING" in prompt else (1 if "LOSING" in prompt else 2)
        reply = f"answer-{number}" if number <= correct_through else "incorrect"
        metadata = dict(agent_input.metadata)
        metadata["mock"] = {"reply": reply}
        return original_run(self, agent_input.model_copy(update={"metadata": metadata}))

    monkeypatch.setattr(MockAdapter, "run", prompt_sensitive_run)
    yield store, ProtectedEvaluator(store, grader), suite.id


def make_experiment(
    store: Any,
    suite_id: str,
    *,
    challenger_prompt: str,
    exp_id: str,
    replicates: int,
    hypothesis: str = "a better worker prompt raises accuracy",
) -> str:
    """Seed a champion (once) + a challenger and create one Experiment.

    ``replicates`` is the knob the "no promotion without repeated evidence"
    assertions turn; everything else is held constant between arms.
    """
    from omniagentos.lab import surfaces
    from omniagentos.lab.contracts import Budgets, Experiment, Surface, SurfaceKind

    champion_entry = store.get_champion("at4", SurfaceKind.PROMPT.value)
    if champion_entry is None:
        champion = surfaces.version_prompt(store, "at4", "worker", "BASELINE")
        surfaces.seed_champion(store, "at4", SurfaceKind.PROMPT, champion.id)
    else:
        champion = Surface.model_validate(store.get_surface(str(champion_entry["surface_id"])))
    challenger = surfaces.version_prompt(
        store,
        "at4",
        "worker",
        challenger_prompt,
        parent_version=champion.version,
    )
    experiment = Experiment(
        id=exp_id,
        hypothesis=hypothesis,
        discipline="at4",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id=champion.id,
        challenger_surface_id=challenger.id,
        eval_suite_id=suite_id,
        dataset_hash="at4-v1",
        primary_metric="accuracy",
        budgets=Budgets(replicates=replicates),
    )
    store.create_experiment(experiment)
    return experiment.id
