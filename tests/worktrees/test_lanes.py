"""T3.8 — the per-unit worktree mechanism (``omniagentos/worktrees/lanes.py``).

Five properties carry this work package; each has a test whose failure means it
must not ship.

``test_flag_*``
    Every lane ships DARK. Off by default, env overrides the config in BOTH
    directions, and an unparseable value never turns anything on.

``test_realm_*``
    THE POINT OF THE FEATURE. A per-unit worktree is only a no-contention claim
    if its path resolves to its OWN realm. Registering the lane's worktrees base
    as a private base at depth 2 is what makes that true, and the "without"
    half of the test shows what the omission costs: every runner worktree
    collapsing onto one realm, and every session/longhaul worktree collapsing
    into the enclosing repo.

``test_mode_recorded_*``
    The m6 discipline. A recorded mode wins over the ambient flag in both
    directions, so a daemon restarted with a different flag cannot flip a live
    unit mid-flight — a unit whose locks were sized for the other mode.

``test_chain_*``
    Longhaul's reason for existing: the unit is the BOARD TASK, so a successor
    attempt inherits the tree — including uncommitted work — instead of getting
    a pristine checkout that silently discards it.

``test_merge_*``
    The sha is captured INSIDE the commit lock and it is that sha that is
    merged, never the branch ref.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import new_id
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.scope import paths as scope_paths
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.worktrees.git import MergeOutcome
from omniagentos.worktrees.lanes import (
    LANE_SPECS,
    SAME_DIR_MODE,
    WORKTREE_MODE,
    LaneWorktree,
    LaneWorktrees,
    lane_spec,
    lane_var_root,
    lane_worktrees_enabled,
    recorded_mode,
    worktree_from_record,
)

RUN_ID = "run_t38a"
OTHER_ID = "run_t38b"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q", str(path)), check=True, capture_output=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture(autouse=True)
def isolated_realms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Realm resolution is process-global; every test starts from a clean slate.

    ``register_private_base`` appends to a module list and ``realm_of`` memoizes
    ``git rev-parse``, so without this a test would inherit its neighbours'
    registrations and the "unregistered" half of the realm proof could pass for
    the wrong reason.
    """
    monkeypatch.setattr(scope_paths, "_EXTRA_PRIVATE_BASES", [])
    scope_paths.clear_realm_cache()


@pytest.fixture(autouse=True)
def flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No host env leaks into a test that states the flag it means."""
    for spec in LANE_SPECS.values():
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SCOPE_LOCKS", raising=False)


def lane(tmp_path: Path, name: str = "runner", **kwargs: Any) -> LaneWorktrees:
    return LaneWorktrees(
        lane_spec(name), var_root=tmp_path / "var" / LANE_SPECS[name].var_subdir, **kwargs
    )


# ---------------------------------------------------------------------------
# (a) flags: dark by default, env wins both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["runner", "session", "longhaul"])
def test_flag_defaults_off(name: str) -> None:
    """The entire acceptance argument rests on this."""
    assert lane_worktrees_enabled(name) is False


@pytest.mark.parametrize("name", ["runner", "session", "longhaul"])
def test_flag_env_enables(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LANE_SPECS[name].env, "1")
    assert lane_worktrees_enabled(name) is True
    # And only that lane.
    others = [other for other in LANE_SPECS if other != name]
    assert [lane_worktrees_enabled(other) for other in others] == [False, False]


def test_flag_env_force_disables_over_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-directional override is not a kill switch."""
    config = tmp_path / "parallelism.yaml"
    config.write_text("worktrees:\n  runner: true\n", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(config))
    assert lane_worktrees_enabled("runner") is True
    monkeypatch.setenv(LANE_SPECS["runner"].env, "off")
    assert lane_worktrees_enabled("runner") is False


def test_flag_unparseable_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must never silently start rewriting where a lane runs."""
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "yess")
    assert lane_worktrees_enabled("longhaul") is False


def test_flag_missing_config_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(tmp_path / "nope.yaml"))
    assert lane_worktrees_enabled("session") is False


def test_constructing_a_lane_binding_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ships dark: construction must not read config, resolve a var root or shell out."""
    monkeypatch.setattr(
        scope_paths, "realm_of", lambda _p: pytest.fail("realm resolved at construction")
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("git run at construction"))
    LaneWorktrees(lane_spec("runner"))


# ---------------------------------------------------------------------------
# (b) layout: the three lanes land where T3.8 says they do
# ---------------------------------------------------------------------------


def test_layout_matches_the_lane_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    var = tmp_path / "var"
    runner = LaneWorktrees(lane_spec("runner"))
    session = LaneWorktrees(lane_spec("session"))
    longhaul = LaneWorktrees(lane_spec("longhaul"))

    assert runner.path_for("run_1") == str(var / "runs" / "worktrees" / "run_1" / "main")
    assert session.path_for("ses_1") == str(var / "sessions" / "worktrees" / "ses_1" / "main")
    assert longhaul.path_for("bt_1") == str(var / "longhaul" / "worktrees" / "bt_1" / "main")
    assert runner.branch_for("run_1").startswith("run/run_1")
    assert session.branch_for("ses_1").startswith("session/ses_1")
    assert longhaul.branch_for("bt_1").startswith("longhaul/bt_1")
    assert lane_var_root(lane_spec("longhaul")) == var / "longhaul"


# ---------------------------------------------------------------------------
# (c) THE REALM PROOF: a worktree is only private if the base is registered
# ---------------------------------------------------------------------------


def test_realm_without_registration_collapses_runner_worktrees_onto_one_realm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this feature inverts into if the registration is skipped.

    ``var/runs`` is ALREADY a depth-1 private base, so every path below
    ``var/runs/worktrees`` keys to the single realm ``var/runs/worktrees`` —
    every run in the fleet colliding on one lock, which is strictly worse than
    the per-project serialization T3.8 exists to remove.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(tmp_path / "var" / "runs"))
    binding = LaneWorktrees(lane_spec("runner"))
    a = scope_paths.realm_of(binding.path_for(RUN_ID))
    b = scope_paths.realm_of(binding.path_for(OTHER_ID))
    assert a == b  # one realm for both runs -- total serialization


def test_realm_without_registration_folds_a_session_worktree_into_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other, worse half: no private base above it at all.

    An unregistered ``var/sessions/worktrees/...`` inside a checkout resolves to
    the checkout's git toplevel — the session would claim (and collide with) the
    whole repository.
    """
    repo = init_repo(tmp_path / "project")
    var = repo / "var"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var))
    binding = LaneWorktrees(lane_spec("session"))
    assert scope_paths.realm_of(binding.path_for("ses_1")) == scope_paths.realm_of(str(repo))


@pytest.mark.parametrize("name", ["runner", "session", "longhaul"])
def test_realm_registration_gives_every_unit_its_own_realm(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the base registered, two units of one lane cannot conflict.

    This is the whole product of T3.8: the same realm-ROOT claim each lane
    already takes becomes a claim on a directory nobody else can name.
    """
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(repo / "var"))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(repo / "var" / "runs"))
    binding = LaneWorktrees(lane_spec(name))
    binding.register_realm_base()

    first = scope_paths.realm_of(binding.path_for("unit_a"))
    second = scope_paths.realm_of(binding.path_for("unit_b"))
    project = scope_paths.realm_of(str(repo))
    assert first is not None and second is not None
    assert first != second  # no contention between two units of the lane
    assert first != project and second != project  # nor with the shared project


def test_realm_registration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    binding = LaneWorktrees(lane_spec("runner"))
    assert binding.register_realm_base() == binding.register_realm_base()
    assert len(scope_paths._EXTRA_PRIVATE_BASES) == 1


# ---------------------------------------------------------------------------
# (d) mode is RECORDED, not ambient (m6)
# ---------------------------------------------------------------------------


def test_mode_activation_records_the_resolved_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    binding = lane(tmp_path)

    activation = binding.activate(RUN_ID, str(repo))

    assert activation.mode is True
    assert activation.working_dir == binding.path_for(RUN_ID)
    assert activation.record is not None
    assert activation.record["mode"] == WORKTREE_MODE
    assert recorded_mode(activation.record) is True
    assert os.path.isdir(activation.working_dir)
    assert git(activation.working_dir, "rev-parse", "--abbrev-ref", "HEAD") == (
        binding.branch_for(RUN_ID)
    )


def test_mode_recorded_worktree_survives_the_flag_being_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon restarted with the flag off must not strand a live unit's tree.

    The unit's locks were taken against the WORKTREE realm; running it in the
    shared project while holding those locks is two writers arrived at through
    the lock table rather than in spite of it.
    """
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    binding = lane(tmp_path)
    first = binding.activate(RUN_ID, str(repo))

    monkeypatch.setenv(LANE_SPECS["runner"].env, "0")
    again = lane(tmp_path).activate(RUN_ID, str(repo), record=first.record)

    assert again.mode is True
    assert again.working_dir == first.working_dir
    assert again.registered is True


def test_mode_recorded_same_dir_never_flips_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: a unit that started shared stays shared."""
    repo = init_repo(tmp_path / "project")
    binding = lane(tmp_path)
    first = binding.activate(RUN_ID, str(repo))  # flag off
    assert first.mode is False
    assert first.record == {"mode": SAME_DIR_MODE}

    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    again = lane(tmp_path).activate(RUN_ID, str(repo), record=first.record)

    assert again.mode is False
    assert again.working_dir == str(repo)
    assert again.record is None  # a recorded mode is never overwritten
    assert not (tmp_path / "var" / "runs" / "worktrees").exists()


def test_mode_probe_failure_falls_back_to_the_shared_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-git working dir is not a reason to refuse the work."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")

    activation = lane(tmp_path, "longhaul").activate("bt_1", str(plain))

    assert activation.mode is False
    assert activation.working_dir == str(plain)
    assert activation.record == {"mode": SAME_DIR_MODE}


def test_mode_a_git_failure_is_not_recorded_as_a_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient create failure must not permanently pin the unit to same-dir."""
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    binding = lane(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, "git worktree add")

    monkeypatch.setattr(binding._worktrees(), "create", boom)
    activation = binding.activate(RUN_ID, str(repo))

    assert activation.mode is False
    assert activation.working_dir == str(repo)
    assert activation.record is None


def test_recorded_mode_reads_garbage_as_unrecorded() -> None:
    for value in (None, {}, [], "worktree", {"mode": "banana"}, {"mode": None}):
        assert recorded_mode(value) is None
    assert worktree_from_record({"mode": WORKTREE_MODE}) is None  # no path/branch


# ---------------------------------------------------------------------------
# (e) the attempt CHAIN: a successor inherits the tree, uncommitted work and all
# ---------------------------------------------------------------------------


def test_chain_successor_attempt_inherits_uncommitted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LONGHAUL'S WHOLE REASON FOR BEING PER-BOARD-TASK.

    Attempt N leaves partial, uncommitted work in the tree. Attempt N+1 must
    find it there. A per-attempt worktree — or any activation that reset the
    tree — would delete it, silently, on every link of the chain.
    """
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")
    binding = lane(tmp_path, "longhaul")

    first = binding.activate("bt_1", str(repo))
    assert first.worktree is not None
    partial = Path(first.worktree.path) / "partial.txt"
    partial.write_text("half the work\n", encoding="utf-8")

    second = binding.activate("bt_1", str(repo), record=first.record)

    assert second.worktree is not None
    assert second.worktree.path == first.worktree.path
    assert second.worktree.reused is True
    assert partial.read_text(encoding="utf-8") == "half the work\n"


def test_chain_salvaged_work_relays_forward_across_a_removed_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when the tree is removed, the branch tip carries the work forward."""
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["longhaul"].env, "1")
    binding = lane(tmp_path, "longhaul")

    first = binding.activate("bt_1", str(repo))
    assert first.worktree is not None
    Path(first.worktree.path, "partial.txt").write_text("salvage me\n", encoding="utf-8")
    outcome = binding.finish(first.worktree)
    assert outcome is not None and outcome.status == "removed"
    assert outcome.salvage_sha  # the dirty tree was committed to the branch

    second = binding.activate("bt_1", str(repo), record=first.record)

    assert second.worktree is not None
    assert Path(second.worktree.path, "partial.txt").exists()
    assert git(second.worktree.path, "rev-parse", "HEAD") == outcome.salvage_sha


# ---------------------------------------------------------------------------
# (f) merge: the sha is captured INSIDE the commit lock
# ---------------------------------------------------------------------------


class OrderProbe:
    """A seam that reports what the durable commit lock looked like when called."""

    def __init__(self, store: SqliteStore, realm: str) -> None:
        self._store = store
        self._realm = realm
        self.calls: list[tuple[str, Any]] = []

    def _commit_locks(self) -> int:
        rows = self._store._connection.execute(
            "SELECT COUNT(*) FROM resource_locks "
            "WHERE realm = ? AND purpose = 'commit' AND released_at IS NULL",
            (self._realm,),
        ).fetchone()
        return int(rows[0])

    # -- the two seam methods integrate() uses -----------------------------
    def head_sha(self, _path: str) -> str:
        self.calls.append(("head_sha", self._commit_locks()))
        return "cafebabe"

    def merge_branch(
        self, _wd: str, branch: str, _message: str, *, sha: str | None = None
    ) -> MergeOutcome:
        self.calls.append(("merge_branch", (sha, self._commit_locks())))
        assert branch  # named for the message, but never merged AS the ref
        return MergeOutcome(status="merged", sha="merged1")

    def remove(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("integrate must not remove")


@pytest.fixture
def locks(tmp_path: Path) -> tuple[SqliteStore, PathLockStore]:
    db = tmp_path / "scope.db"
    migrate(str(db))
    store = SqliteStore(str(db))
    return store, PathLockStore(store)


def test_merge_captures_the_sha_inside_the_commit_lock(
    tmp_path: Path, locks: tuple[SqliteStore, PathLockStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capturing outside the lock leaves a window in which the ref moves.

    Both calls must see the realm's commit lock HELD, and the sha the merge is
    given must be exactly the one the capture returned — the merge target is a
    commit, never a branch ref another writer could have advanced.
    """
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "enforce")
    repo = init_repo(tmp_path / "project")
    realm = scope_paths.realm_of(str(repo))
    assert realm is not None
    store, path_locks = locks
    probe = OrderProbe(store, realm)
    binding = lane(tmp_path, worktrees=probe)
    worktree = LaneWorktree(
        lane="runner",
        unit_id=RUN_ID,
        path=str(tmp_path / "wt"),
        branch="run/x/main",
        base_sha="base",
        base_dir=str(repo),
        realm="wt-realm",
    )

    result = binding.integrate(
        worktree,
        locks=path_locks,
        holder=LockHolder(kind="run", id=RUN_ID, lane="runner"),
    )

    assert result.status == "merged"
    assert [name for name, _ in probe.calls] == ["head_sha", "merge_branch"]
    assert probe.calls[0][1] == 1, "the sha was captured OUTSIDE the commit lock"
    assert probe.calls[1][1] == ("cafebabe", 1)
    # And the lock is given back afterwards.
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM resource_locks WHERE purpose = 'commit' AND released_at IS NULL"
        ).fetchone()[0]
        == 0
    )


def test_merge_is_deferred_when_another_holder_owns_the_commit_lock(
    tmp_path: Path, locks: tuple[SqliteStore, PathLockStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy realm defers the merge; it never merges without the lock.

    Deferred means the WORKTREE SURVIVES — the caller keeps it and retries, and
    ``integrate`` reports ``skipped`` rather than pretending to have landed.
    """
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "enforce")
    repo = init_repo(tmp_path / "project")
    realm = scope_paths.realm_of(str(repo))
    assert realm is not None
    store, path_locks = locks
    probe = OrderProbe(store, realm)
    binding = lane(tmp_path, worktrees=probe, merge_wait_s=0.0, merge_poll_s=0.0)
    worktree = LaneWorktree(
        lane="runner",
        unit_id=RUN_ID,
        path=str(tmp_path / "wt"),
        branch="run/x/main",
        base_sha="base",
        base_dir=str(repo),
        realm="wt-realm",
    )

    from contextlib import ExitStack

    with ExitStack() as stack:
        stack.enter_context(
            path_locks.commit_lock(
                realm, LockHolder(kind="coordinator", id=new_id("swr"), lane="swarm")
            )
        )
        result = binding.integrate(
            worktree,
            locks=path_locks,
            holder=LockHolder(kind="run", id=RUN_ID, lane="runner"),
        )

    assert result.status == "skipped"
    assert probe.calls == []  # nothing was read and nothing was merged


def test_merge_without_scope_locks_still_merges_by_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locking off degrades to the in-process lock; the sha discipline stands."""
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    binding = lane(tmp_path)
    activation = binding.activate(RUN_ID, str(repo))
    assert activation.worktree is not None
    Path(activation.worktree.path, "feature.txt").write_text("done\n", encoding="utf-8")
    git(activation.worktree.path, "add", "-A")
    git(activation.worktree.path, "commit", "-q", "-m", "feature")
    tip = git(activation.worktree.path, "rev-parse", "HEAD")

    result = binding.integrate(activation.worktree)

    assert result.status == "merged"
    assert result.sha and result.sha != tip  # a --no-ff merge commit
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
    assert tip in git(repo, "log", "--format=%H")


def test_merge_conflict_keeps_the_branch_and_leaves_main_pristine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict is routed, not lost: the branch stays alive and main is clean."""
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["session"].env, "1")
    binding = lane(tmp_path, "session")
    activation = binding.activate("ses_1", str(repo))
    assert activation.worktree is not None

    Path(activation.worktree.path, "README.md").write_text("theirs\n", encoding="utf-8")
    git(activation.worktree.path, "add", "-A")
    git(activation.worktree.path, "commit", "-q", "-m", "theirs")
    (repo / "README.md").write_text("ours\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ours")

    result = binding.integrate(activation.worktree)

    assert result.status == "conflict"
    assert result.conflict_files == ("README.md",)
    assert result.branch == activation.worktree.branch
    assert git(repo, "status", "--porcelain") == ""  # merge --abort already ran
    assert (repo / "README.md").read_text(encoding="utf-8") == "ours\n"
    assert result.branch in git(repo, "branch", "--list", result.branch)


def test_merge_skips_when_the_worktree_has_no_readable_head(tmp_path: Path) -> None:
    """No sha, no merge. Merging the ref instead is the hazard, not the fallback."""
    repo = init_repo(tmp_path / "project")
    binding = lane(tmp_path)
    result = binding.integrate(
        LaneWorktree(
            lane="runner",
            unit_id=RUN_ID,
            path=str(tmp_path / "gone"),
            branch="run/x/main",
            base_sha="",
            base_dir=str(repo),
            realm="",
        )
    )
    assert result.status == "skipped"
    assert result.detail == "no_head_sha"


def test_record_round_trips_through_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record is persisted as JSON by all three lanes; it must survive that."""
    repo = init_repo(tmp_path / "project")
    monkeypatch.setenv(LANE_SPECS["runner"].env, "1")
    activation = lane(tmp_path).activate(RUN_ID, str(repo))
    assert activation.record is not None

    revived = worktree_from_record(json.loads(json.dumps(activation.record)))

    assert revived is not None
    assert activation.worktree is not None
    assert revived.path == activation.worktree.path
    assert revived.branch == activation.worktree.branch
    assert revived.base_dir == str(repo)


@pytest.mark.no_worktree_interlock_lift
def test_interlock_holds_without_this_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRODUCTION behaviour: the lane flags cannot be turned on, by any means.

    Opts out of conftest's interlock lift, then tries the two ways a real host
    could enable a lane -- the env override and the config file -- and asserts
    both are refused. This is the test that keeps `_EXECUTOR_WIRING_COMPLETE`
    honest: if someone flips it to True before the executor cwd actually follows
    the worktree, this fails and names the remaining work.
    """
    from omniagentos.worktrees import lanes as _lanes

    assert _lanes._EXECUTOR_WIRING_COMPLETE is False, (
        "executor wiring is not complete; remaining work: "
        + "; ".join(_lanes.EXECUTOR_WIRING_REMAINING)
    )
    for lane in ("runner", "session", "longhaul"):
        monkeypatch.setenv(f"OMNIAGENTOS_WORKTREES_{lane.upper()}", "1")
        assert _lanes.lane_worktrees_enabled(lane) is False
