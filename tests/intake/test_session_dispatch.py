"""Wire 1 -- Fable -> spawn monitored sessions.

``execute="session"`` skips the runner queue entirely and launches a live,
monitored Claude Code session via the Session Bridge
(``SessionSupervisor.spawn``, injected here as a mock so no real ``claude``
process is ever started). ``readonly``/``tools`` stay on the untouched
runner-queue lane (covered by tests/intake/test_exec.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake import service
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.fable import FABLE_MODEL
from omniagentos.intake.planner import ProjectPlan, RouteDecision, provision_plan
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.projects import ProjectStore
from omniagentos.sessions.dal import SessionsDal
from tests.support.db_template import make_store


def _assert_no_secret_grants(granted: list[str]) -> None:
    """Standing roots must never include credential paths or product SOURCE trees."""
    import os

    forbidden = [
        os.path.realpath(os.path.expanduser("~/.ssh")),
        os.path.realpath(os.path.expanduser("~/.aws")),
        os.path.realpath(os.path.expanduser("~/OmniAgentOS/omniagentos")),
        os.path.realpath(os.path.expanduser("~/OmniAgentOS/omniagentos")),
    ]
    real = [os.path.realpath(g) for g in granted]
    for secret in forbidden:
        assert secret not in real, f"secret path granted: {secret}"
        assert not any(r.startswith(secret + os.sep) for r in real)


@dataclass
class _MockSpawner:
    """Stand-in for ``omniagentos.sessions.supervisor.SessionSupervisor``.

    Records every spawn() call and returns a fresh deterministic session id --
    never touches a real ``claude`` process or the sessions DB.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def spawn(
        self,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None = None,
        title: str | None = None,
        extra_write_roots: list[str] | None = None,
        orchestrator_owned: bool = False,
        orchestrator_run_id: str | None = None,
        granted_roots: list[str] | None = None,
    ) -> str:
        self._seq += 1
        session_id = f"ses_mock{self._seq:04d}"
        self.calls.append(
            {
                "project_dir": project_dir,
                "model": model,
                "prompt": prompt,
                "budget_usd_max": budget_usd_max,
                "title": title,
                "extra_write_roots": extra_write_roots,
                "orchestrator_owned": orchestrator_owned,
                "orchestrator_run_id": orchestrator_run_id,
                "granted_roots": granted_roots,
            }
        )
        # Simulate the real supervisor: ownership is marked at spawn time (pre-launch).
        dal = getattr(self, "dal", None)
        if orchestrator_owned and dal is not None:
            dal.mark_orchestrator_session(session_id, orchestrator_run_id)
        return session_id


@dataclass
class _FailingSpawner:
    dal: SessionsDal

    def spawn(self, *_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("claude executable unavailable")


def _spec(**overrides: Any) -> RefinedSpec:
    base: dict[str, Any] = {
        "title": "Write a hello file",
        "description": "Create hello.txt with the word hi.",
        "acceptance_criteria": ["hello.txt exists"],
    }
    base.update(overrides)
    return RefinedSpec(**base)


def _stores(tmp_path: Path, name: str = "session.db") -> tuple[Any, CollabStore, Any]:
    collab = make_store(CollabStore, tmp_path / name)
    return collab._store, collab, load_policy()


def test_session_dispatch_spawns_monitored_session_and_skips_run(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        execute="session",
        session_spawner=spawner,
    )

    # A session dispatch never creates a queued run.
    assert result["execute"] == "session"
    assert result["run_id"] is None
    assert result["session_id"] == "ses_mock0001"
    assert store.list_runs({}) == []

    # SessionSupervisor.spawn was called exactly once with the task's prompt/title.
    assert len(spawner.calls) == 1
    call = spawner.calls[0]
    assert call["model"] == "haiku"
    assert call["title"] == "Write a hello file"
    assert "Write a hello file" in call["prompt"]
    assert "hello.txt exists" in call["prompt"]
    assert call["budget_usd_max"] is None

    # The task is still created and tracks the session mode -- trackable from the
    # control-plane task list.
    task = store.get_task(result["task_id"])
    assert task is not None
    import json

    assert json.loads(task["input_json"])["execute"] == "session"

    # The board card records the session id (result_ref) so it is trackable from
    # the board too; the dashboard's Sessions area already lists it separately.
    card = collab.get_board_task(result["board_task"]["id"])
    assert card is not None
    assert card["result_ref"] == "ses_mock0001"
    assert card["run_id"] is None


def test_fast_lane_session_grants_target_and_runs_hands_off(tmp_path: Path) -> None:
    """The superfast lane: a ``fast=True`` session dispatch grants the target dir as
    an extra sandbox write root, injects it + a TodoWrite nudge into the prompt, marks
    the session orchestrator-owned (hands-off: auto-approve safe / escalate
    money+delete), and reuses the pre-created board card instead of duplicating it."""
    store, collab, cfg = _stores(tmp_path)
    dal = SessionsDal(str(tmp_path / "session.db"))
    spawner = _MockSpawner()
    spawner.dal = dal  # type: ignore[attr-defined]

    target = tmp_path / "Desktop"
    target.mkdir()

    # The instant placeholder card the quick front door would have created.
    card = service.create_queued_goal_card(collab, "make a folder called tiger", "orch_fast1")

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(title="make a folder called tiger", description="make a folder called tiger"),
        execute="session",
        fast=True,
        target_write_root=str(target),
        board_task_id=str(card["id"]),
        session_spawner=spawner,
    )

    call = spawner.calls[0]
    # The vetted target is granted as an extra write root + injected into the prompt.
    assert call["extra_write_roots"] == [str(target)]
    assert str(target) in call["prompt"]
    assert "TodoWrite" in call["prompt"]

    # Hands-off: spawn is asked to mark the session orchestrator-owned (pre-launch, so
    # its first write auto-approves; only money/delete escalate).
    assert call["orchestrator_owned"] is True
    assert dal.is_orchestrator_session(result["session_id"]) is True

    # The pre-created card was REUSED, not duplicated.
    assert result["board_task"]["id"] == str(card["id"])
    assert len(collab.list_board_tasks()) == 1
    dal.close()


def test_session_dispatch_records_dial_speed_on_the_task(tmp_path: Path) -> None:
    """D10 (F2): a caller-supplied dial speed lands on the task's input metadata
    (tasks.input_json) so the fastlane's spawned session carries it durably; a
    legacy call (no speed) keeps the input payload byte-identical."""
    import json

    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    with_speed = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="session",
        session_spawner=spawner,
        speed="fast",
    )
    task = store.get_task(with_speed["task_id"])
    assert task is not None
    assert json.loads(task["input_json"])["speed"] == "fast"

    without_speed = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(title="No dial"),
        execute="session",
        session_spawner=spawner,
    )
    legacy_task = store.get_task(without_speed["task_id"])
    assert legacy_task is not None
    assert "speed" not in json.loads(legacy_task["input_json"])


def test_session_dispatch_uses_project_dir_and_budget(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    project_root = tmp_path / "repo"
    project_root.mkdir()
    project = ProjectStore(store).create_project(
        {"name": "demo", "root_dirs": [str(project_root)], "budget_usd": 12.5}
    )

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        project_id=str(project["id"]),
        execute="session",
        session_spawner=spawner,
    )

    assert result["working_dir"] == str(project_root)
    call = spawner.calls[0]
    assert call["project_dir"] == str(project_root)
    # Session budget defaults to the project's own declared budget_usd cap.
    assert call["budget_usd_max"] == 12.5


def test_session_dispatch_threads_full_granted_scope(tmp_path: Path) -> None:
    """P3: a session honors its project's FULL granted scope. dispatch resolves the
    project's other root_dirs + allowed_dirs (server-side, minus working_dir) and
    passes them to spawn() as granted_roots -- frozen for the sandbox + hook."""
    import os

    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    primary = tmp_path / "repo"
    second = tmp_path / "repo2"
    allowed = tmp_path / "shared"
    for directory in (primary, second, allowed):
        directory.mkdir()

    project = ProjectStore(store).create_project(
        {
            "name": "multiroot",
            "root_dirs": [str(primary), str(second)],
            "allowed_dirs": [str(allowed)],
        }
    )

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        project_id=str(project["id"]),
        execute="session",
        session_spawner=spawner,
    )

    # working_dir is root_dirs[0]; granted_roots is the REST of the scope, realpathed
    # and excluding working_dir, so the session may write in every granted dir.
    # AUTO-APPROVE Phase 1 also merges standing roots (Desktop, var/, …).
    assert result["working_dir"] == str(primary)
    granted = spawner.calls[0]["granted_roots"] or []
    real = [os.path.realpath(g) for g in granted]
    assert os.path.realpath(str(second)) in real
    assert os.path.realpath(str(allowed)) in real
    _assert_no_secret_grants(real)


def test_granted_roots_drops_symlink_retarget_out_of_scope(tmp_path: Path) -> None:
    """HARDEN (P3 dispatch symlink-retarget): a grant recorded as an in-scope path but
    SYMLINK-RETARGETED after validation re-resolves to its new destination at dispatch.
    A retarget that now points OUTSIDE every approved root and every managed base
    (here, ``/etc``) is dropped, even though it is not itself a secret dir.

    Standing roots may still appear (AUTO-APPROVE Phase 1); the invariant this
    test guards is that the retarget destination is NEVER granted.
    """
    import os

    store, _collab, _cfg = _stores(tmp_path)
    primary = tmp_path / "repo"
    primary.mkdir()
    retarget = tmp_path / "escape"
    os.symlink("/etc", retarget)  # a grant later pointed out of scope

    project = ProjectStore(store).create_project(
        {"name": "retargeted", "root_dirs": [str(primary)], "allowed_dirs": [str(retarget)]}
    )

    granted = service._resolve_session_granted_roots(store, str(project["id"]), str(primary))
    real = [os.path.realpath(g) for g in granted]
    assert os.path.realpath("/etc") not in real
    assert str(retarget) not in granted
    _assert_no_secret_grants(real)


def test_granted_roots_drops_symlink_retarget_into_secret_dir(tmp_path: Path) -> None:
    """HARDEN: a retarget INTO a credential store is dropped by the secret gate (which
    realpaths first), never handed to a session -- the deny invariant holds.

    Standing roots may appear; ~/.ssh itself must never be among them.
    """
    import os

    store, _collab, _cfg = _stores(tmp_path)
    primary = tmp_path / "repo"
    primary.mkdir()
    retarget = tmp_path / "escape_secret"
    os.symlink(os.path.expanduser("~/.ssh"), retarget)

    project = ProjectStore(store).create_project(
        {"name": "retargeted2", "root_dirs": [str(primary)], "allowed_dirs": [str(retarget)]}
    )

    granted = service._resolve_session_granted_roots(store, str(project["id"]), str(primary))
    real = [os.path.realpath(g) for g in granted]
    assert os.path.realpath(os.path.expanduser("~/.ssh")) not in real
    assert not any(r.startswith(os.path.realpath(os.path.expanduser("~/.ssh"))) for r in real)
    _assert_no_secret_grants(real)


def test_granted_roots_keeps_managed_var_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HARDEN: a server-managed workspace under ``<var>`` (e.g. an orchestration
    project's ``var/projects/<id>/workspace``) is NOT within the user-approved grant
    roots, but it IS a legitimate managed base -- so it is kept, not dropped.
    Standing roots may also appear after AUTO-APPROVE Phase 1.
    """
    import os

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    store, _collab, _cfg = _stores(tmp_path)
    primary = tmp_path / "repo"
    managed = tmp_path / "var" / "projects" / "projX" / "workspace"
    primary.mkdir()
    managed.mkdir(parents=True)

    project = ProjectStore(store).create_project(
        {"name": "managed", "root_dirs": [str(primary)], "allowed_dirs": [str(managed)]}
    )

    granted = service._resolve_session_granted_roots(store, str(project["id"]), str(primary))
    real = [os.path.realpath(g) for g in granted]
    assert os.path.realpath(str(managed)) in real
    _assert_no_secret_grants(real)


def test_session_dispatch_no_project_grants_still_gets_standing_roots(tmp_path: Path) -> None:
    """AUTO-APPROVE Phase 1: a single-root project still receives standing roots
    (Desktop, agent var/, …). It must never receive secret paths.
    """
    import os

    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    project_root = tmp_path / "solo"
    project_root.mkdir()
    project = ProjectStore(store).create_project({"name": "solo", "root_dirs": [str(project_root)]})

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        project_id=str(project["id"]),
        execute="session",
        session_spawner=spawner,
    )

    granted = spawner.calls[0]["granted_roots"] or []
    assert isinstance(granted, list)
    real = [os.path.realpath(g) for g in granted]
    _assert_no_secret_grants(real)
    # Standing set is non-empty in production config (Desktop / var / …).
    assert any("Desktop" in r or r.endswith("/var") or "jambot" in r for r in real)


def test_session_dispatch_creates_declared_project_dir(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()
    project_root = tmp_path / "not-yet-created" / "repo"
    project = ProjectStore(store).create_project(
        {"name": "future repo", "root_dirs": [str(project_root)]}
    )

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=str(project["id"]),
        execute="session",
        session_spawner=spawner,
    )

    assert project_root.is_dir()
    assert spawner.calls[0]["project_dir"] == str(project_root)


def test_session_dispatch_records_spawn_failure_and_blocks_card(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    sessions_dal = SessionsDal(tmp_path / "failed-sessions.db")
    spawner = _FailingSpawner(sessions_dal)
    try:
        with pytest.raises(RuntimeError, match="claude executable unavailable"):
            dispatch_spec(
                store,
                collab,
                cfg,
                _spec(),
                execute="session",
                session_spawner=spawner,
            )

        cards = collab.list_board_tasks()
        assert len(cards) == 1
        assert cards[0]["status"] == "blocked"
        failed_id = str(cards[0]["result_ref"])
        failed = sessions_dal.get_session(failed_id)
        assert failed is not None
        assert failed["state"] == "failed"
        assert failed["error"] == "claude executable unavailable"
        assert store.list_tasks({})
    finally:
        sessions_dal.close()


def test_session_dispatch_explicit_budget_overrides_project_budget(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()
    project = ProjectStore(store).create_project({"name": "demo2", "budget_usd": 12.5})

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        project_id=str(project["id"]),
        execute="session",
        session_spawner=spawner,
        budget_usd_max=3.0,
    )

    assert spawner.calls[0]["budget_usd_max"] == 3.0


def test_session_dispatch_no_project_uses_scratch_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        execute="session",
        session_spawner=spawner,
    )

    task_id = result["task_id"]
    expected = tmp_path / "var" / "intake-workspace" / task_id
    assert Path(result["working_dir"]) == expected
    assert expected.is_dir()  # never an unscoped/home dir


@pytest.mark.parametrize(
    ("band", "expected_model"),
    [
        ("simple", "haiku"),
        ("standard", "sonnet"),
        ("complex", FABLE_MODEL),
    ],
)
def test_session_dispatch_default_model_tiers_by_complexity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    band: str,
    expected_model: str,
) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()
    monkeypatch.setattr(service, "_estimate_session_complexity", lambda _goal: band)

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        execute="session",
        session_spawner=spawner,
    )

    assert spawner.calls[0]["model"] == expected_model


@pytest.mark.parametrize(
    ("band", "env_name"),
    [
        ("simple", "OMNIAGENTOS_SESSION_MODEL_SIMPLE"),
        ("standard", "OMNIAGENTOS_SESSION_MODEL_STANDARD"),
        ("complex", "OMNIAGENTOS_SESSION_MODEL_COMPLEX"),
    ],
)
def test_session_dispatch_model_tier_env_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    band: str,
    env_name: str,
) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()
    monkeypatch.setattr(service, "_estimate_session_complexity", lambda _goal: band)
    monkeypatch.setenv(env_name, f"custom-{band}")

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        execute="session",
        session_spawner=spawner,
    )

    assert spawner.calls[0]["model"] == f"custom-{band}"


def test_session_dispatch_explicit_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()
    monkeypatch.setattr(service, "_estimate_session_complexity", lambda _goal: "complex")
    monkeypatch.setenv("OMNIAGENTOS_SESSION_MODEL_COMPLEX", "env-complex")

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        execute="session",
        session_spawner=spawner,
        model="claude-opus-4.8",
    )

    assert spawner.calls[0]["model"] == "claude-opus-4.8"


def test_session_dispatch_unknown_model_tier_biases_to_sonnet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()
    monkeypatch.setattr(service, "_estimate_session_complexity", lambda _goal: "uncertain")

    with caplog.at_level("INFO", logger=service.__name__):
        dispatch_spec(
            store,
            collab,
            cfg,
            _spec(),
            harness=HarnessType.MOCK.value,
            execute="session",
            session_spawner=spawner,
        )

    assert spawner.calls[0]["model"] == "sonnet"
    tier_logs = [
        record
        for record in caplog.records
        if record.name == service.__name__
        and record.getMessage().startswith("session model tier selected:")
    ]
    assert len(tier_logs) == 1
    assert "band=uncertain model=sonnet" in tier_logs[0].getMessage()


def test_readonly_and_tools_modes_still_create_a_run_not_a_session(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store, collab, cfg, _spec(), harness=HarnessType.MOCK.value, session_spawner=spawner
    )

    assert result["execute"] == "readonly"
    assert result["session_id"] is None
    assert result["run_id"] is not None
    assert spawner.calls == []  # the spawner is never touched outside session mode


def test_multi_task_plan_dispatches_one_monitored_session_per_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole Fable plan in "sessions" mode spawns ONE monitored session PER
    task -- provision_plan (planner.py, unmodified) already loops over every
    task calling dispatch_spec(execute=execute); this only has to prove that
    loop now fans out into N SessionSupervisor.spawn() calls instead of N
    queued runs."""
    store, collab, cfg = _stores(tmp_path, "plan.db")
    spawner = _MockSpawner()
    # provision_plan calls dispatch_spec directly (it is not mine to change and
    # does not thread a session_spawner through) -- so the default spawner
    # factory is monkeypatched instead, exactly like curator.py tests monkeypatch
    # a module-level collaborator.
    monkeypatch.setattr(service, "_default_session_spawner", lambda: spawner)

    from tests.intake.test_planner import _StubHierarchyDAL

    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(
        project_name="Reports",
        tasks=[
            {"title": "Weekly report", "acceptance_criteria": ["emailed"]},  # type: ignore[list-item]
            {"title": "Monthly report"},  # type: ignore[list-item]
            {"title": "Quarterly report"},  # type: ignore[list-item]
        ],
    )
    route = RouteDecision(decision="new", project_name="Reports")

    result = provision_plan(
        store,
        collab,
        cfg,
        plan,
        route,
        hierarchy,
        harness=HarnessType.MOCK.value,
        execute="session",
    )

    assert result.task_count == 3
    assert len(spawner.calls) == 3
    titles = {call["title"] for call in spawner.calls}
    assert titles == {"Weekly report", "Monthly report", "Quarterly report"}
    # Every dispatched task carries a distinct session id and no run.
    session_ids = {row["session_id"] for row in result.dispatched}
    assert len(session_ids) == 3
    assert all(row["run_id"] is None for row in result.dispatched)
    assert store.list_runs({}) == []
