"""T3.8 — the three lane wirings on top of ``worktrees.lanes``.

The headline is :func:`test_longhaul_two_tasks_in_one_project_run_in_parallel`:
under ``enforce``, two longhaul tasks pointed at the SAME project used to
serialize on one realm-root lock, and with per-task worktrees they do not. The
control case in the same test — the flag off, the second task refused — is what
makes it a measurement rather than an assertion.

Everything else is the acceptance floor: with every flag at its shipped default
the three wirings are byte-for-byte what they were, proven on the declaration
column (exact bytes, not "parses the same"), on the store writes, and by making
the worktree machinery itself fail the test if it is ever reached.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul.store import WORKTREE_LANE as LONGHAUL_LANE
from omniagentos.longhaul.store import LonghaulStore, ScopeUnavailable
from omniagentos.runner.scope_wiring import (
    RunnerScope,
    claims_to_json,
    derive_claims,
    json_to_claims,
    json_to_worktree,
)
from omniagentos.scope import paths as scope_paths
from omniagentos.scope.model import DEFAULT_PURPOSE, ScopeClaim
from omniagentos.sessions.scope_wiring import (
    SessionScopeGate,
    decode_scope,
    decode_worktree,
    encode_scope,
)
from omniagentos.worktrees import lanes as lanes_module
from omniagentos.worktrees.lanes import LANE_SPECS, WORKTREE_MODE
from tests.worktrees.test_lanes import git, init_repo

RUN_ID = "run_wiring1"
TASK_A = "bt_alpha"
TASK_B = "bt_beta"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fresh realm registrations, fresh lane singletons, no host env."""
    monkeypatch.setattr(scope_paths, "_EXTRA_PRIVATE_BASES", [])
    scope_paths.clear_realm_cache()
    monkeypatch.setattr(lanes_module, "_LANES", {})
    for spec in LANE_SPECS.values():
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SCOPE_LOCKS", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(tmp_path / "var" / "runs"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "project")


class FakeRunStore:
    """The three ``Store`` methods ``RunnerScope`` uses off the lock path."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def add(self, run_id: str, **fields: Any) -> dict[str, Any]:
        row = {"id": run_id, "state": "running", "worker_id": "w1", "scope_json": None}
        row.update(fields)
        self.rows[run_id] = row
        return row

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.rows.get(run_id)
        return dict(row) if row else None

    def update_run(
        self, run_id: str, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool:
        self.writes.append((run_id, dict(fields)))
        row = self.rows.get(run_id)
        if row is None or (expect_worker is not None and row["worker_id"] != expect_worker):
            return False
        row.update(fields)
        return True

    def get_task(self, _task_id: str) -> dict[str, Any] | None:
        return None


def longhaul_store(tmp_path: Path) -> LonghaulStore:
    db = tmp_path / "longhaul.db"
    migrate(str(db))
    return LonghaulStore(str(db))


def board_task(store: LonghaulStore, task_id: str) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO board_tasks "
        "(id,title,description,status,created_at,updated_at,lane,longhaul_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (task_id, task_id, "work", "in_progress", now, now, "longhaul", "{}"),
    )
    store._connection.commit()


# ---------------------------------------------------------------------------
# (a) DARK: every flag at its default leaves the three lanes untouched
# ---------------------------------------------------------------------------


def test_dark_runner_declaration_bytes_are_unchanged() -> None:
    """``runs.scope_json`` must be the exact text it was before T3.8 existed."""
    claims = (ScopeClaim(realm="/realm/a", components=(), kind="root", purpose=DEFAULT_PURPOSE),)
    assert claims_to_json(claims) == (
        '{"claims":[{"kind":"root","path":".","purpose":"edit","realm":"/realm/a"}],"v":1}'
    )
    assert json_to_worktree(claims_to_json(claims)) is None


def test_dark_session_declaration_bytes_are_unchanged() -> None:
    """``sessions.scope_json`` stays a bare LIST while there is no worktree."""
    claims = (ScopeClaim(realm="/realm/a", components=("src",), kind="root"),)
    assert encode_scope(claims) == (
        '[{"kind":"root","path":"src","purpose":"edit","realm":"/realm/a"}]'
    )
    assert encode_scope(()) is None
    assert decode_worktree(encode_scope(claims)) is None


def test_dark_runner_working_dir_never_touches_git(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag off the seam is not built, git is not run, nothing is written."""
    monkeypatch.setattr(
        lanes_module.LaneWorktrees,
        "activate",
        lambda *a, **k: pytest.fail("worktree activated with the flag off"),
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("git run with the flag off"))
    store = FakeRunStore()
    run = store.add(RUN_ID)
    scope = RunnerScope(store, worker_id="w1", workspace_base=str(project))

    assert scope.working_dir_for(run, str(project)) == str(project)
    assert store.writes == []
    assert scope.worktree_for(RUN_ID) is None
    assert scope.integrate(RUN_ID) is None


def test_dark_session_working_dir_never_touches_git(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lanes_module.LaneWorktrees,
        "activate",
        lambda *a, **k: pytest.fail("worktree activated with the flag off"),
    )
    gate = SessionScopeGate(object(), mode_cache_s=0.0)
    assert gate.working_dir_for("ses_1", str(project)) == str(project)
    assert gate.worktree_for("ses_1") is None
    assert gate.integrate("ses_1") is None


def test_dark_longhaul_working_dir_writes_nothing(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lanes_module.LaneWorktrees,
        "activate",
        lambda *a, **k: pytest.fail("worktree activated with the flag off"),
    )
    store = longhaul_store(tmp_path)
    board_task(store, TASK_A)

    assert store.working_dir_for(TASK_A, str(project)) == str(project)
    assert store.get_longhaul_json(TASK_A) == {}
    assert store.worktree_for(TASK_A) is None
    assert store.integrate_worktree(TASK_A) is None


def test_dark_longhaul_attempt_rows_are_unchanged(project: Path, tmp_path: Path) -> None:
    """The attempt chain itself is untouched by the wiring."""
    store = longhaul_store(tmp_path)
    board_task(store, TASK_A)
    working_dir = store.working_dir_for(TASK_A, str(project))
    first = store.open_attempt(TASK_A, "cli-claude", "opus", working_dir=working_dir)
    assert store.close_attempt(first["id"], "completed") is True
    second = store.open_attempt(TASK_A, "cli-claude", "opus", working_dir=working_dir)

    assert [row["seq"] for row in store.list_attempts(TASK_A)] == [0, 1]
    assert second["seq"] == 1
    assert store.get_longhaul_json(TASK_A) == {}


# ---------------------------------------------------------------------------
# (b) runner: activation, the recorded mode, and the private claim
# ---------------------------------------------------------------------------


def test_runner_activation_records_the_mode_on_the_run_row(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    store = FakeRunStore()
    run = store.add(RUN_ID)
    scope = RunnerScope(store, worker_id="w1", workspace_base=str(project))

    working_dir = scope.working_dir_for(run, str(project))

    assert working_dir.endswith(f"worktrees/{RUN_ID}/main")
    record = json_to_worktree(store.rows[RUN_ID]["scope_json"])
    assert record is not None and record["mode"] == WORKTREE_MODE
    assert record["base_dir"] == str(project)
    # Replay: a second call re-reads the record and returns the same tree.
    assert scope.working_dir_for(store.get_run(RUN_ID) or {}, str(project)) == working_dir


def test_runner_claim_derives_to_the_private_worktree_realm(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole product: same claim algebra, uncontended realm.

    ``derive_claims`` is unchanged — it still claims the whole realm of the
    directory the run works in. What changed is which directory that is.
    """
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    store = FakeRunStore()
    scope = RunnerScope(store, worker_id="w1", workspace_base=str(project))
    first = scope.working_dir_for(store.add("run_one"), str(project))
    second = scope.working_dir_for(store.add("run_two"), str(project))

    claim_a = derive_claims({}, {"working_dir": first}, first)[0]
    claim_b = derive_claims({}, {"working_dir": second}, second)[0]
    project_realm = scope_paths.realm_of(str(project))

    assert claim_a.realm != claim_b.realm
    assert claim_a.realm != project_realm and claim_b.realm != project_realm
    assert claim_a.is_whole_realm and claim_b.is_whole_realm


def test_runner_take_preserves_the_recorded_worktree(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-serializing the declaration must not erase the registration.

    ``take`` rewrites ``scope_json`` on every claim; if it dropped the record the
    next process would re-resolve the mode from the ambient flag, which is the
    mid-flight flip m6 exists to prevent.
    """
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "shadow")
    store = FakeRunStore()
    run = store.add(RUN_ID)
    scope = RunnerScope(store, worker_id="w1", workspace_base=str(project))
    working_dir = scope.working_dir_for(run, str(project))
    claims = derive_claims({}, {"working_dir": working_dir}, working_dir)

    rewritten = claims_to_json(claims, worktree=json_to_worktree(store.rows[RUN_ID]["scope_json"]))

    assert json_to_worktree(rewritten) is not None
    assert json_to_claims(rewritten)[0].realm == claims[0].realm


def test_runner_terminal_run_merges_and_removes_its_worktree(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    store = FakeRunStore()
    run = store.add(RUN_ID)
    scope = RunnerScope(store, worker_id="w1", workspace_base=str(project))
    working_dir = scope.working_dir_for(run, str(project))
    Path(working_dir, "landed.txt").write_text("work\n", encoding="utf-8")
    git(working_dir, "add", "-A")
    git(working_dir, "commit", "-q", "-m", "work")

    store.rows[RUN_ID]["state"] = "completed"
    scope.release_if_terminal(RUN_ID)

    assert (project / "landed.txt").read_text(encoding="utf-8") == "work\n"
    assert not Path(working_dir).exists()
    assert scope.worktree_for(RUN_ID) is None


def test_runner_displaced_worker_does_not_merge(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adopter owns the workspace; a displaced worker must not mutate it."""
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    store = FakeRunStore()
    run = store.add(RUN_ID)
    scope = RunnerScope(store, worker_id="w1", workspace_base=str(project))
    working_dir = scope.working_dir_for(run, str(project))
    Path(working_dir, "landed.txt").write_text("work\n", encoding="utf-8")
    git(working_dir, "add", "-A")
    git(working_dir, "commit", "-q", "-m", "work")

    store.rows[RUN_ID]["worker_id"] = "w2"  # adopted away
    store.rows[RUN_ID]["state"] = "completed"
    scope.release_if_terminal(RUN_ID)

    assert not (project / "landed.txt").exists()
    assert Path(working_dir).exists()  # left intact for the adopter


# ---------------------------------------------------------------------------
# (c) sessions: the envelope, and the lane-owned exemption
# ---------------------------------------------------------------------------


def test_session_envelope_carries_claims_and_record_together(project: Path) -> None:
    claims = (ScopeClaim(realm=str(project), components=(), kind="root"),)
    record = {"mode": WORKTREE_MODE, "path": "/wt", "branch": "session/x/main"}

    payload = encode_scope(claims, worktree=record)

    assert json.loads(payload)["v"] == 2
    assert decode_scope(payload) == claims
    assert decode_worktree(payload) == record


def test_session_lane_owned_sessions_get_no_worktree_of_their_own(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``[longhaul:...]`` session runs inside its LANE's tree, not a second one.

    Longhaul's worktree is per BOARD TASK and outlives this attempt; nesting a
    session worktree inside it would split the chain's tree in two.
    """
    monkeypatch.setenv(LANE_SPECS["session"].env, "1")
    gate = SessionScopeGate(object(), mode_cache_s=0.0)

    assert gate.working_dir_for(
        "ses_1", str(project), title="[longhaul:tks_1] fix the thing"
    ) == str(project)
    assert gate.worktree_for("ses_1") is None


def test_session_activation_records_and_replays(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LANE_SPECS["session"].env, "1")
    written: list[Any] = []

    class Gate(SessionScopeGate):
        def _persist_scope(self, session_id: str, payload: str | None) -> None:
            written.append(payload)

    gate = Gate(object(), mode_cache_s=0.0)
    working_dir = gate.working_dir_for("ses_1", str(project))

    assert working_dir.endswith("var/sessions/worktrees/ses_1/main")
    assert len(written) == 1
    record = decode_worktree(written[0])
    assert record is not None and record["mode"] == WORKTREE_MODE
    # Replay through the recorded column, in a gate that never activated it.
    assert (
        Gate(object(), mode_cache_s=0.0).working_dir_for(
            "ses_1", str(project), recorded_scope_json=written[0]
        )
        == working_dir
    )


# ---------------------------------------------------------------------------
# (d) longhaul: the chain keeps its tree, and two tasks stop serializing
# ---------------------------------------------------------------------------


def test_longhaul_worktree_is_per_board_task_not_per_attempt(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE bug this mapping exists to avoid.

    Attempt 0 leaves uncommitted work; ``close_attempt`` fires; attempt 1 opens.
    The tree, and the partial work in it, must still be there — the successor is
    continuing the SAME piece of work, and a per-attempt worktree would hand it a
    pristine checkout and throw the rest away.
    """
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")
    store = longhaul_store(tmp_path)
    board_task(store, TASK_A)

    first_dir = store.working_dir_for(TASK_A, str(project))
    first = store.open_attempt(TASK_A, "cli-claude", "opus", working_dir=first_dir)
    Path(first_dir, "partial.txt").write_text("half done\n", encoding="utf-8")
    store.close_attempt(first["id"], "usage_limited")

    second_dir = store.working_dir_for(TASK_A, str(project))
    store.open_attempt(TASK_A, "cli-claude", "opus", working_dir=second_dir)

    assert second_dir == first_dir
    assert Path(second_dir, "partial.txt").read_text(encoding="utf-8") == "half done\n"
    assert Path(second_dir).exists()  # close_attempt must NOT remove the tree


def test_longhaul_two_tasks_in_one_project_run_in_parallel(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE HEADLINE. Same project, two tasks, ``enforce`` — both dispatch.

    The control is the second half: with the flag off, the same two tasks
    against the same project serialize exactly as they did before, because the
    second one is refused the project realm. That refusal is what per-task
    worktrees remove, and this is the measurement of it.
    """
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "enforce")
    store = longhaul_store(tmp_path)
    board_task(store, TASK_A)
    board_task(store, TASK_B)

    # Control: no worktrees -> the second task is refused the project realm.
    store.open_attempt(TASK_A, "cli-claude", "opus", working_dir=str(project))
    with pytest.raises(ScopeUnavailable):
        store.open_attempt(TASK_B, "cli-claude", "opus", working_dir=str(project))
    for row in store.list_attempts(TASK_A):
        store.close_attempt(row["id"], "superseded")

    # With per-task worktrees, both hold disjoint realms.
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")
    dir_a = store.working_dir_for(TASK_A, str(project))
    dir_b = store.working_dir_for(TASK_B, str(project))
    attempt_a = store.open_attempt(TASK_A, "cli-claude", "opus", working_dir=dir_a)
    attempt_b = store.open_attempt(TASK_B, "cli-claude", "opus", working_dir=dir_b)

    assert dir_a != dir_b
    assert attempt_a["id"] != attempt_b["id"]
    realms = {
        row["realm"]
        for row in store._connection.execute(
            "SELECT realm FROM resource_locks WHERE released_at IS NULL"
        ).fetchall()
    }
    assert len(realms) == 2
    assert scope_paths.realm_of(str(project)) not in realms


def test_longhaul_integration_merges_the_task_and_clears_the_record(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")
    store = longhaul_store(tmp_path)
    board_task(store, TASK_A)
    working_dir = store.working_dir_for(TASK_A, str(project))
    Path(working_dir, "shipped.txt").write_text("done\n", encoding="utf-8")
    git(working_dir, "add", "-A")
    git(working_dir, "commit", "-q", "-m", "done")

    result = store.integrate_worktree(TASK_A)

    assert result is not None and result.status == "merged"
    assert (project / "shipped.txt").read_text(encoding="utf-8") == "done\n"
    assert not Path(working_dir).exists()
    assert store.get_longhaul_json(TASK_A) == {}
    # Idempotent: a duplicate terminal delivery merges nothing.
    assert store.integrate_worktree(TASK_A) is None


def test_longhaul_record_survives_an_unrelated_longhaul_json_write(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column is shared state; the registration merges into it, never over it."""
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")
    store = longhaul_store(tmp_path)
    board_task(store, TASK_A)
    store.set_longhaul_json(TASK_A, {"acceptance": "tests pass", "park_reason": "scope"})

    store.working_dir_for(TASK_A, str(project))

    state = store.get_longhaul_json(TASK_A) or {}
    assert state["acceptance"] == "tests pass"
    assert state["park_reason"] == "scope"
    assert state["worktree"]["mode"] == WORKTREE_MODE
    assert LONGHAUL_LANE == "longhaul"
