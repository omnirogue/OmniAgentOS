"""Scope telemetry: the ship-dark proof, the derivation, and the gate arithmetic.

Three tests carry more weight than the rest.

``test_all_recorders_are_inert_with_the_shipped_defaults``
    The SHIP-DARK proof. Every recorder is called with a store double that raises
    ``AssertionError`` on ANY attribute access — so "wrote no rows" is proved as
    "made no store call at all", which is the only version of the claim that also
    covers a config read that reaches the database. Also asserts the git seams are
    never touched: the dark path must not pay for a ``git diff`` subprocess whose
    result it then discards.

``test_precision_is_the_hang_rate_not_a_string_prefix``
    Coverage uses the SAME component-wise containment as
    :mod:`omniagentos.scope.conflict`. If this drifts to string prefixes the gate
    stops measuring enforcement: ``.github`` would "cover" ``github/x``, precision
    would read high, and ENFORCE would then hang on a path the report called
    declared.

``test_thin_or_short_evidence_is_insufficient_never_pass``
    A gate that answers ``pass`` on three clean units is worse than no gate,
    because it launders an absence of measurement into a green light. Proves the
    unit-count and 72-hour window conditions both return ``insufficient_data``
    rather than ``pass``, and that only a real precision shortfall returns
    ``fail``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import DeclaredScope, Events, ObservedChange
from omniagentos.db.store import SqliteStore
from omniagentos.scope import config as scope_config
from omniagentos.scope import observe
from omniagentos.scope.conflict import ScopeConflict
from omniagentos.scope.locks import HeldLock, LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.scope.observe import (
    ACTION_CONFLICT_SHADOW,
    ACTION_DECLARED_VS_ACTUAL,
    MIN_GATE_UNITS,
    PATH_SAMPLE_LIMIT,
    PRECISION_GATE,
    SCOPE_OBSERVE_ENV,
    SCOPE_TARGET_TYPE,
    SOAK_WINDOW_HOURS,
    declared_paths_from_scope,
    declared_vs_actual,
    derive_actual,
    observe_acquire,
    record_declared_vs_actual,
    record_shadow_conflict,
    record_terminal_observation,
    resolve_held,
    scope_gate_report,
    scope_observe_enabled,
    session_reported_paths,
)
from tests.support.db_template import make_store

REALM = "/realm/one"


# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient env or repo config may reach a test.

    ``OMNIAGENTOS_PARALLELISM_CONFIG`` points at a path that does not exist, which
    ``parallelism_config`` degrades to ``{}`` — so every test starts from the
    hard-coded defaults regardless of what ``configs/parallelism.yaml`` says on
    the developer's checkout.
    """
    monkeypatch.delenv(SCOPE_OBSERVE_ENV, raising=False)
    monkeypatch.delenv(scope_config.SCOPE_LOCKS_ENV, raising=False)
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(tmp_path / "absent.yaml"))


@pytest.fixture
def observing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry ON, locks in shadow — the rollout ramp's configuration."""
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "shadow")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    created = make_store(SqliteStore, tmp_path / "observe.db")
    yield created
    created.close()


class ExplodingStore:
    """Any attribute access is a failure. The ship-dark assertion.

    Stronger than a call counter: it also catches a recorder that reads
    ``store.something`` to decide whether to record.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dark path touched the store: {name}")


class ExplodingGit:
    """Any diff call is a failure — the dark path must not shell out to git."""

    def changed_paths_since(self, path: str, base_sha: str) -> list[str]:
        raise AssertionError("dark path ran changed_paths_since")

    def changed_paths(self, working_dir: str) -> list[str]:
        raise AssertionError("dark path ran changed_paths")


class ProtocolOnlyStore:
    """Exactly the frozen ``Store`` event slice — no ``_connection``, no ``_lock``.

    Forces :func:`scope_gate_report` down its portable ``get_events_after`` path so
    both readers are covered.
    """

    def __init__(self, inner: SqliteStore) -> None:
        self._inner = inner

    def insert_event(self, **kwargs: Any) -> int:
        return self._inner.insert_event(**kwargs)

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self._inner.get_events_after(after_id, types, limit)


def claim(path: str, *, kind: str = "file", lock_id: str = "") -> ScopeClaim:
    return ScopeClaim.for_path(REALM, path, kind=kind, lock_id=lock_id)  # type: ignore[arg-type]


def conflict(
    candidate: str = "src/a.py", held: str = "src/a.py", *, lock_id: str = "lk-1"
) -> ScopeConflict:
    return ScopeConflict(
        candidate=claim(candidate),
        held=claim(held, lock_id=lock_id),
        reason="same_file",
    )


def scope_events(store: SqliteStore, action: str | None = None) -> list[dict[str, Any]]:
    rows = store.get_events_after(0, [Events.AUDIT], 500)
    return [
        row
        for row in rows
        if row["target_type"] == SCOPE_TARGET_TYPE and (action is None or row["action"] == action)
    ]


def payload_of(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(row["payload_json"])


# ---------------------------------------------------------------------------
# SHIP DARK
# ---------------------------------------------------------------------------


def test_all_recorders_are_inert_with_the_shipped_defaults() -> None:
    """THE SHIP-DARK PROOF: zero store calls, zero git calls, on every entry point."""
    assert scope_config.scope_locks_mode() == "off"
    assert scope_observe_enabled() is False

    dark_store = ExplodingStore()
    dark_git = ExplodingGit()

    assert record_shadow_conflict(dark_store, conflict(), candidate_lane="swarm") is None
    assert (
        record_declared_vs_actual(
            dark_store,
            declared_vs_actual(["src"], ["src/a.py"]),
            lane="swarm",
        )
        is None
    )
    assert (
        record_terminal_observation(
            dark_store,
            lane="swarm",
            declared=["src"],
            worktree_path="/tmp/wt",
            base_sha="deadbeef",
            worktrees=dark_git,
            working_dir="/tmp/wd",
            git=dark_git,
            session={"files_json": '["src/a.py"]'},
        )
        is None
    )


def test_inert_recorders_write_no_events_to_a_real_store(store: SqliteStore) -> None:
    """Byte-for-byte: the events table is unchanged after every recorder runs."""
    before = store.latest_event_id()
    record_shadow_conflict(store, conflict(), candidate_lane="swarm")
    record_terminal_observation(store, lane="swarm", declared=["src"], session={"files_json": "[]"})
    observe_acquire(
        store,
        _acquire_result_with_conflict(),
        candidate_lane="runner",
    )
    assert store.latest_event_id() == before
    assert scope_events(store) == []


def _acquire_result_with_conflict() -> Any:
    from omniagentos.scope.locks import AcquireResult

    return AcquireResult(status="granted", mode="shadow", conflict=conflict(), shadowed=True)


def test_observe_follows_the_locks_and_the_env_overrides_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default follows ``scope_locks_mode``; the env var is a real kill switch."""
    assert scope_observe_enabled() is False

    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "shadow")
    assert scope_observe_enabled() is True, "a shadow soak must collect gate data"

    monkeypatch.setenv(SCOPE_OBSERVE_ENV, "off")
    assert scope_observe_enabled() is False, "the kill switch must beat shadow mode"

    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "off")
    monkeypatch.setenv(SCOPE_OBSERVE_ENV, "1")
    assert scope_observe_enabled() is True, "measuring declarations must not require locks"

    monkeypatch.setenv(SCOPE_OBSERVE_ENV, "enfroce")  # typo
    assert scope_observe_enabled() is False, "an unparseable value must not turn it on"


def test_config_file_enables_observation_without_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "parallelism.yaml"
    config.write_text("scope_observe: true\n", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(config))
    assert scope_observe_enabled() is True
    monkeypatch.setenv(SCOPE_OBSERVE_ENV, "no")
    assert scope_observe_enabled() is False


# ---------------------------------------------------------------------------
# The lifted derivation
# ---------------------------------------------------------------------------


class FakeWorktrees:
    def __init__(self, paths: list[str] | Exception) -> None:
        self._paths = paths
        self.calls: list[tuple[str, str]] = []

    def changed_paths_since(self, path: str, base_sha: str) -> list[str]:
        self.calls.append((path, base_sha))
        if isinstance(self._paths, Exception):
            raise self._paths
        return list(self._paths)


class FakeGit:
    def __init__(self, paths: list[str] | Exception) -> None:
        self._paths = paths
        self.calls: list[str] = []

    def changed_paths(self, working_dir: str) -> list[str]:
        self.calls.append(working_dir)
        if isinstance(self._paths, Exception):
            raise self._paths
        return list(self._paths)


def test_worktree_mode_uses_the_cumulative_branch_delta() -> None:
    """Worktree mode reads ``changed_paths_since``, NOT the agent's own report.

    The scheduler's reasoning, lifted: workers commit freely inside their worktree,
    so ``files_json`` under-reports. Under-reporting inflates precision, which is
    the direction that would falsely OPEN the gate.
    """
    worktrees = FakeWorktrees(["src/a.py", "src/b.py"])
    observed = derive_actual(
        worktree_path="/wt/task-1",
        base_sha="base-sha",
        worktrees=worktrees,
        session={"files_json": '["src/a.py"]'},
        working_dir="/repo",
        git=FakeGit(["ignored.py"]),
    )
    assert observed.source == "git-worktree"
    assert observed.paths == ["src/a.py", "src/b.py"]
    assert observed.base_ref == "base-sha"
    assert worktrees.calls == [("/wt/task-1", "base-sha")]


def test_non_worktree_mode_prefers_the_session_report_then_falls_back() -> None:
    git = FakeGit(["src/fallback.py"])
    reported = derive_actual(
        session={"files_json": '["src/said.py"]'}, working_dir="/repo", git=git
    )
    assert (reported.source, reported.paths) == ("agent-report", ["src/said.py"])
    assert git.calls == [], "the report answered; git must not be consulted"

    fell_back = derive_actual(session={}, working_dir="/repo", git=git)
    assert (fell_back.source, fell_back.paths) == ("git-index", ["src/fallback.py"])
    assert git.calls == ["/repo"]


def test_an_empty_agent_report_is_not_the_same_as_no_report() -> None:
    """``[]`` means "touched nothing"; ``None`` means "unknown"."""
    assert session_reported_paths({"files_json": "[]"}) == []
    assert session_reported_paths({}) is None
    assert session_reported_paths({"files_json": "not json"}) is None
    assert session_reported_paths(None) is None

    git = FakeGit(["src/x.py"])
    empty = derive_actual(session={"files_json": "[]"}, working_dir="/repo", git=git)
    assert (empty.source, empty.paths) == ("agent-report", [])
    assert git.calls == [], "an explicit empty report must not fall through to git"


def test_a_failed_derivation_is_unobserved_never_clean() -> None:
    """The one deliberate divergence from the scheduler.

    The scheduler substitutes ``[]`` on a git error because its next move is a
    revert. For the gate, ``[]`` would be a silent perfect score, so a failed
    derivation is ``unobserved`` and never counts.
    """
    broken = derive_actual(
        worktree_path="/wt/x", base_sha="b", worktrees=FakeWorktrees(RuntimeError("boom"))
    )
    assert broken.source == "unobserved"
    assert broken.paths == []
    assert declared_vs_actual(["src"], broken).counted is False

    broken_workdir = derive_actual(working_dir="/repo", git=FakeGit(OSError("no git")))
    assert broken_workdir.source == "unobserved"

    assert derive_actual().source == "unobserved", "no seams at all is unobserved"


# ---------------------------------------------------------------------------
# declared vs actual
# ---------------------------------------------------------------------------


def test_precision_is_the_hang_rate_not_a_string_prefix() -> None:
    """Coverage is the ENFORCEMENT algebra: component-wise containment.

    A string-prefix rule would let ``.github`` cover ``github/x`` — precision would
    read clean and ENFORCE would then hang on a path this report called declared.
    """
    measured = declared_vs_actual(
        [".github", "src/a"],
        ["src/a/deep/b.py", "github/x.yml", "README.md"],
    )
    assert measured.covered == ("src/a/deep/b.py",)
    assert measured.missing == ("github/x.yml", "README.md")
    assert measured.precision == pytest.approx(1 / 3)
    assert measured.clean is False
    assert measured.counted is True


def test_the_whole_realm_declaration_covers_everything() -> None:
    """``"."`` normalizes to the whole realm, which is what the lock store grants."""
    measured = declared_vs_actual(["."], ["a/b.py", "c.py"])
    assert measured.missing == ()
    assert measured.precision == 1.0
    assert measured.clean is True


def test_nothing_touched_is_excluded_from_the_gate_not_credited() -> None:
    """A fleet of no-op units must never be able to open the gate."""
    measured = declared_vs_actual(["src"], [])
    assert measured.precision == 1.0
    assert measured.counted is False


def test_malformed_paths_are_dropped_from_both_sides() -> None:
    """An escaping declaration covers nothing; an escaping actual is unattributable."""
    measured = declared_vs_actual(["../outside", "/abs/path", "src"], ["src/a.py", "../escape.py"])
    assert measured.actual == ("src/a.py",)
    assert measured.missing == ()
    assert measured.precision == 1.0

    widened = declared_vs_actual(["../outside"], ["src/a.py"])
    assert widened.missing == ("src/a.py",), "a malformed declaration must not cover anything"


def test_paths_are_normalized_before_comparison() -> None:
    measured = declared_vs_actual(["./src//a/"], ["src/a/../a/b.py", "src/a/b.py"])
    assert measured.actual == ("src/a/b.py",), "duplicates collapse after normalization"
    assert measured.missing == ()


def test_declared_scope_flattens_every_path_field() -> None:
    """A delete is a write; enforcement arbitrates it like any other."""
    scope = DeclaredScope(
        files_to_modify=["a.py"],
        files_to_create=["b.py"],
        files_to_delete=["c.py"],
        create_roots=["gen/"],
        must_modify=["a.py"],
    )
    assert declared_paths_from_scope(scope) == ["a.py", "b.py", "c.py", "gen/"]


def test_observed_change_input_is_accepted_directly() -> None:
    observed = ObservedChange(source="git-worktree", paths=["src/a.py"])
    measured = declared_vs_actual(["src"], observed)
    assert measured.source == "git-worktree"
    assert measured.counted is True


# ---------------------------------------------------------------------------
# Stream (a): shadow conflicts
# ---------------------------------------------------------------------------


def test_shadow_conflict_payload_carries_the_seven_required_fields(
    store: SqliteStore, observing: None
) -> None:
    event_id = record_shadow_conflict(
        store,
        conflict(candidate="src/a.py", held="src", lock_id="lk-9"),
        candidate_lane="runner",
        held_lane="swarm",
        held_holder="swarm_task:t-1",
        blocked_s=12.5,
        run_id="run-1",
        unit_id="unit-1",
    )
    assert event_id is not None
    rows = scope_events(store, ACTION_CONFLICT_SHADOW)
    assert len(rows) == 1
    payload = payload_of(rows[0])
    for key in (
        "realm",
        "candidate_path",
        "held_path",
        "reason",
        "candidate_lane",
        "held_lane",
        "held_holder",
    ):
        assert key in payload, key
    assert payload["realm"] == REALM
    assert payload["candidate_path"] == "src/a.py"
    assert payload["held_path"] == "src"
    assert payload["reason"] == "same_file"
    assert payload["candidate_lane"] == "runner"
    assert payload["held_lane"] == "swarm"
    assert payload["held_holder"] == "swarm_task:t-1"
    assert payload["blocked_s"] == 12.5
    assert payload["mode"] == "shadow"
    assert rows[0]["actor"] == "scope:runner"


def test_shadow_conflict_does_not_pollute_run_or_project_feeds(
    store: SqliteStore, observing: None
) -> None:
    """``target_type`` is ``scope``, so pre-existing feeds keep their shape.

    ``get_events_for_run`` and ``list_events_for_project`` both filter
    ``target_type='run'``. Telemetry must not change a UI feed as a side effect of
    being switched on.
    """
    record_shadow_conflict(store, conflict(), candidate_lane="swarm", run_id="run-1")
    record_declared_vs_actual(
        store, declared_vs_actual(["src"], ["src/a.py"]), lane="swarm", run_id="run-1"
    )
    assert store.get_events_for_run("run-1") == [], "the run-detail feed is unchanged"
    assert [row["target_type"] for row in scope_events(store)] == [SCOPE_TARGET_TYPE] * 2
    assert payload_of(scope_events(store)[0])["run_id"] == "run-1", "correlation lives in payload"


def test_held_lane_and_holder_are_resolved_from_the_lock_row(
    store: SqliteStore, observing: None
) -> None:
    """``as_claim()`` carries the lock id but not lane/holder — those are columns."""
    locks = PathLockStore(store)
    holder = LockHolder(kind="swarm_task", id="task-7", lane="swarm")
    result = locks.try_acquire_scope([claim("src/a.py")], holder, enforce=False)
    assert result.lock_ids
    live = locks.held_in_realm(REALM)
    assert len(live) == 1

    observed_conflict = ScopeConflict(
        candidate=claim("src/a.py"),
        held=live[0].as_claim(),
        reason="same_file",
    )
    assert resolve_held(locks, observed_conflict) is not None

    record_shadow_conflict(store, observed_conflict, candidate_lane="runner", locks=locks)
    payload = payload_of(scope_events(store, ACTION_CONFLICT_SHADOW)[0])
    assert payload["held_lane"] == "swarm"
    assert payload["held_holder"] == "swarm_task:task-7"


def test_resolve_held_degrades_rather_than_losing_the_event(
    store: SqliteStore, observing: None
) -> None:
    class BrokenLookup:
        def held_in_realm(self, realm: str) -> list[HeldLock]:
            raise RuntimeError("db gone")

    assert resolve_held(None, conflict()) is None
    assert resolve_held(BrokenLookup(), conflict()) is None
    assert resolve_held(BrokenLookup(), conflict(lock_id="")) is None

    record_shadow_conflict(store, conflict(), candidate_lane="swarm", locks=BrokenLookup())
    payload = payload_of(scope_events(store, ACTION_CONFLICT_SHADOW)[0])
    assert payload["held_lane"] == ""
    assert payload["held_holder"] == ""


def test_observe_acquire_is_a_noop_on_the_granted_path(store: SqliteStore, observing: None) -> None:
    from omniagentos.scope.locks import AcquireResult

    clean = AcquireResult(status="granted", mode="shadow")
    assert observe_acquire(store, clean, candidate_lane="swarm") is None
    assert scope_events(store) == []

    collided = AcquireResult(status="granted", mode="shadow", conflict=conflict(), shadowed=True)
    assert observe_acquire(store, collided, candidate_lane="swarm") is not None
    assert len(scope_events(store, ACTION_CONFLICT_SHADOW)) == 1


def test_shadow_mode_writes_real_rows_so_the_conflict_is_real(
    store: SqliteStore, observing: None
) -> None:
    """The premise of stream (a): shadow INSERTS, so contention is measured not guessed.

    Two different holders claim the same file in shadow mode. The second is granted
    (shadow refuses nothing) and reports a conflict — against a row that actually
    exists. If shadow had declined to write, the second claim would have seen an
    empty realm and the soak would have measured zero contention.
    """
    locks = PathLockStore(store)
    first = LockHolder(kind="swarm_task", id="task-a", lane="swarm")
    second = LockHolder(kind="run", id="run-b", lane="runner")

    granted = locks.try_acquire_scope([claim("src/a.py")], first, enforce=False)
    assert granted.status == "granted"

    shadowed = locks.try_acquire_scope([claim("src/a.py")], second, enforce=False)
    assert shadowed.status == "granted", "shadow refuses nothing"
    assert shadowed.conflict is not None, "but it reports the collision"

    observe_acquire(store, shadowed, candidate_lane="runner", locks=locks, blocked_s=3.0)
    payload = payload_of(scope_events(store, ACTION_CONFLICT_SHADOW)[0])
    assert payload["held_holder"] == "swarm_task:task-a"
    assert payload["candidate_lane"] == "runner"
    assert payload["blocked_s"] == 3.0


# ---------------------------------------------------------------------------
# Stream (b): the gate event
# ---------------------------------------------------------------------------


def test_declared_vs_actual_payload_carries_the_four_gate_fields(
    store: SqliteStore, observing: None
) -> None:
    measured = record_terminal_observation(
        store,
        lane="swarm",
        realm=REALM,
        declared=["src/a"],
        run_id="run-1",
        unit_id="task-1",
        terminal_state="completed",
        worktree_path="/wt/task-1",
        base_sha="base",
        worktrees=FakeWorktrees(["src/a/b.py", "uv.lock"]),
    )
    assert measured is not None
    payload = payload_of(scope_events(store, ACTION_DECLARED_VS_ACTUAL)[0])
    assert payload["declared"] == ["src/a"]
    assert payload["actual"] == ["src/a/b.py", "uv.lock"]
    assert payload["missing"] == ["uv.lock"]
    assert payload["precision"] == 0.5
    assert payload["source"] == "git-worktree"
    assert payload["counted"] is True
    assert payload["lane"] == "swarm"
    assert payload["realm"] == REALM
    assert payload["terminal_state"] == "completed"


def test_huge_path_lists_are_truncated_but_counts_stay_exact(
    store: SqliteStore, observing: None
) -> None:
    """The gate arithmetic reads COUNTS, so a 4000-file unit stays a row not a blob."""
    actual = [f"gen/f{i}.py" for i in range(PATH_SAMPLE_LIMIT + 50)]
    record_declared_vs_actual(
        store,
        declared_vs_actual([], actual),
        lane="swarm",
    )
    payload = payload_of(scope_events(store, ACTION_DECLARED_VS_ACTUAL)[0])
    assert len(payload["actual"]) == PATH_SAMPLE_LIMIT
    assert len(payload["missing"]) == PATH_SAMPLE_LIMIT
    assert payload["actual_count"] == PATH_SAMPLE_LIMIT + 50
    assert payload["missing_count"] == PATH_SAMPLE_LIMIT + 50
    assert payload["covered_count"] == 0
    assert payload["truncated"] is True


def test_terminal_observation_skips_the_derivation_when_dark(
    store: SqliteStore,
) -> None:
    """The flag is checked BEFORE the git subprocess, not after."""
    worktrees = FakeWorktrees(["src/a.py"])
    assert (
        record_terminal_observation(
            store, lane="swarm", declared=["src"], worktree_path="/wt", worktrees=worktrees
        )
        is None
    )
    assert worktrees.calls == []


def test_a_telemetry_failure_never_raises_into_a_lane(observing: None) -> None:
    """A measurement must not be able to break a run."""

    class FailingStore:
        def insert_event(self, **kwargs: Any) -> int:
            raise RuntimeError("events table is gone")

    failing = FailingStore()
    assert record_shadow_conflict(failing, conflict(), candidate_lane="swarm") is None
    measured = record_terminal_observation(
        failing, lane="swarm", declared=["src"], session={"files_json": '["src/a.py"]'}
    )
    assert measured is not None, "the measurement still comes back; only the write was lost"
    assert measured.clean is True


# ---------------------------------------------------------------------------
# The gate query
# ---------------------------------------------------------------------------


def _seed_unit(
    store: SqliteStore,
    *,
    lane: str,
    declared: list[str],
    actual: list[str],
    realm: str = REALM,
) -> None:
    record_declared_vs_actual(
        store,
        declared_vs_actual(declared, ObservedChange(source="git-worktree", paths=actual)),
        lane=lane,
        realm=realm,
    )


def test_summary_buckets_per_lane_and_per_realm(store: SqliteStore, observing: None) -> None:
    _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    _seed_unit(store, lane="swarm", declared=["src"], actual=["src/b.py", "uv.lock"])
    _seed_unit(store, lane="runner", declared=["docs"], actual=["docs/x.md"], realm="/realm/two")
    record_shadow_conflict(store, conflict(), candidate_lane="swarm", blocked_s=4.0)
    record_shadow_conflict(store, conflict(), candidate_lane="swarm", blocked_s=6.5)

    report = scope_gate_report(store)

    swarm = report.by_lane["swarm"]
    assert swarm.units == 2
    assert swarm.units_counted == 2
    assert swarm.units_clean == 1
    assert swarm.actual_paths == 3
    assert swarm.covered_paths == 2
    assert swarm.missing_paths == 1
    assert swarm.precision == pytest.approx(2 / 3)
    assert swarm.unit_precision == pytest.approx(0.5)
    assert swarm.conflicts == 2
    assert swarm.blocked_seconds == pytest.approx(10.5)
    assert swarm.conflict_rate == pytest.approx(1.0)
    assert swarm.top_missing == (("uv.lock", 1),)

    runner = report.by_lane["runner"]
    assert (runner.units_counted, runner.missing_paths) == (1, 0)
    assert runner.conflicts == 0

    assert set(report.by_realm) == {("swarm", REALM), ("runner", "/realm/two")}
    assert report.by_realm[("swarm", REALM)].conflicts == 2


def test_the_realm_bucket_separates_contention_by_realm(
    store: SqliteStore, observing: None
) -> None:
    _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"], realm=REALM)
    _seed_unit(store, lane="swarm", declared=["src"], actual=["zzz.py"], realm="/realm/two")
    report = scope_gate_report(store)
    assert report.by_realm[("swarm", REALM)].missing_paths == 0
    assert report.by_realm[("swarm", "/realm/two")].missing_paths == 1
    assert report.by_lane["swarm"].missing_paths == 1, "the lane rollup sums both realms"


def test_unobserved_units_are_counted_but_never_scored(store: SqliteStore, observing: None) -> None:
    record_declared_vs_actual(
        store,
        declared_vs_actual(["src"], ObservedChange(source="unobserved")),
        lane="swarm",
    )
    report = scope_gate_report(store)
    bucket = report.by_lane["swarm"]
    assert bucket.units == 1
    assert bucket.units_unobserved == 1
    assert bucket.units_counted == 0
    assert report.verdict("swarm") == "insufficient_data"


def test_thin_or_short_evidence_is_insufficient_never_pass(
    store: SqliteStore, observing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1-3 answer "has the question been answered"; only 4 can answer ``fail``."""
    assert scope_gate_report(store).verdict("swarm") == "insufficient_data"

    # Clean, but far too few units.
    for _ in range(3):
        _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    report = scope_gate_report(store)
    assert report.by_lane["swarm"].precision == 1.0
    assert report.verdict("swarm") == "insufficient_data"
    assert any("counted units" in b for b in report.blockers("swarm"))

    # Enough units, all clean, but the window is one instant long.
    for _ in range(MIN_GATE_UNITS):
        _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    report = scope_gate_report(store)
    assert report.by_lane["swarm"].units_counted >= MIN_GATE_UNITS
    assert report.by_lane["swarm"].window_hours < SOAK_WINDOW_HOURS
    assert report.verdict("swarm") == "insufficient_data"
    assert any("soak window" in b for b in report.blockers("swarm"))


def test_a_real_precision_shortfall_fails_the_gate(store: SqliteStore, observing: None) -> None:
    """Only condition 4 produces ``fail`` — and it does, with a 72h window present."""
    _seed_events_over_a_soak(
        store,
        lane="swarm",
        clean_units=MIN_GATE_UNITS + 40,
        dirty_units=10,
        hours=SOAK_WINDOW_HOURS + 1,
    )
    report = scope_gate_report(store)
    bucket = report.by_lane["swarm"]
    assert bucket.window_hours >= SOAK_WINDOW_HOURS
    assert bucket.unit_precision < PRECISION_GATE
    assert report.verdict("swarm") == "fail"
    assert any("precision" in b for b in report.blockers("swarm"))


def test_the_gate_passes_only_on_a_full_clean_soak(store: SqliteStore, observing: None) -> None:
    _seed_events_over_a_soak(
        store,
        lane="swarm",
        clean_units=MIN_GATE_UNITS + 10,
        dirty_units=0,
        hours=SOAK_WINDOW_HOURS + 1,
    )
    report = scope_gate_report(store)
    assert report.verdict("swarm") == "pass"
    assert report.blockers("swarm") == []
    assert report.verdict("runner") == "insufficient_data", "the gate is PER LANE"

    snapshot = report.as_dict()
    assert snapshot["precision_gate"] == PRECISION_GATE
    assert snapshot["soak_window_hours"] == SOAK_WINDOW_HOURS
    assert snapshot["lanes"]["swarm"]["verdict"] == "pass"


def test_one_unit_missing_many_paths_still_fails_on_unit_precision(
    store: SqliteStore, observing: None
) -> None:
    """Path and unit precision fail differently, and enforcement hangs UNITS.

    60 clean units of 20 declared paths each (1200 covered paths) plus ONE unit
    that touched 3 undeclared paths: path precision reads 1200/1203 = 0.9975,
    comfortably over the gate, while unit precision is 60/61 = 0.984, under it.
    Enforcement would have hung that one unit, so the gate must still refuse — and
    it does only because :meth:`ScopeGateReport.verdict` checks both numbers.
    """
    _seed_events_over_a_soak(
        store, lane="swarm", clean_units=60, dirty_units=0, hours=80.0, paths_per_unit=20
    )
    record_declared_vs_actual(
        store,
        declared_vs_actual(["src"], ["a.py", "b.py", "c.py"]),
        lane="swarm",
        realm=REALM,
    )
    report = scope_gate_report(store)
    bucket = report.by_lane["swarm"]
    assert bucket.precision >= PRECISION_GATE, "3 of 1203 paths is above the path gate"
    assert bucket.unit_precision < PRECISION_GATE
    assert report.verdict("swarm") == "fail"


def _seed_events_over_a_soak(
    store: SqliteStore,
    *,
    lane: str,
    clean_units: int,
    dirty_units: int,
    hours: float,
    paths_per_unit: int = 1,
) -> None:
    """Write (b) events directly with backdated ``ts`` to synthesize a long window.

    ``insert_event`` stamps ``ts`` itself, so a multi-day soak has to be forged in
    SQL. The payloads are produced by the real :func:`declared_vs_actual` /
    :func:`record_declared_vs_actual` path first, then the timestamps are spread —
    so the arithmetic under test is still the production arithmetic.
    """
    import json

    total = clean_units + dirty_units
    for index in range(total):
        actual = [f"src/f{index}_{n}.py" for n in range(paths_per_unit)]
        if index >= clean_units:
            actual.append(f"stray{index}.py")
        _seed_unit(store, lane=lane, declared=["src"], actual=actual)
    rows = scope_events(store, ACTION_DECLARED_VS_ACTUAL)
    start = datetime.now(UTC) - timedelta(hours=hours)
    step = timedelta(hours=hours / max(len(rows) - 1, 1))
    with store._lock:
        for offset, row in enumerate(rows):
            stamp = (start + step * offset).strftime("%Y-%m-%dT%H:%M:%SZ")
            store._connection.execute("UPDATE events SET ts = ? WHERE id = ?", (stamp, row["id"]))
        store._connection.commit()
    assert json.loads(rows[0]["payload_json"])["lane"] == lane


def test_the_report_reads_the_same_numbers_through_the_frozen_protocol(
    store: SqliteStore, observing: None
) -> None:
    """Both readers agree: the SQL fast path and the ``get_events_after`` fallback.

    The fast path exists because 72 hours of production ``audit.event`` rows is a
    lot to page through; the fallback exists because the frozen ``Store`` protocol
    is the only guaranteed surface. A divergence between them would mean the gate
    answers differently depending on which store it was handed.
    """
    _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py", "uv.lock"])
    _seed_unit(store, lane="runner", declared=["docs"], actual=["docs/a.md"])
    record_shadow_conflict(store, conflict(), candidate_lane="swarm", blocked_s=2.0)

    direct = scope_gate_report(store)
    via_protocol = scope_gate_report(ProtocolOnlyStore(store))

    assert direct.events_scanned == via_protocol.events_scanned == 3
    for lane in ("swarm", "runner"):
        left = direct.by_lane[lane].as_dict()
        right = via_protocol.by_lane[lane].as_dict()
        assert left == right


def test_the_report_ignores_unrelated_audit_events(store: SqliteStore, observing: None) -> None:
    """Other lanes' audit rows must not land in the gate buckets."""
    store.insert_event(
        type=Events.AUDIT,
        actor="scheduler",
        action="task_completed",
        target_type="run",
        target_id="run-1",
        payload={"lane": "swarm"},
    )
    _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    for report in (scope_gate_report(store), scope_gate_report(ProtocolOnlyStore(store))):
        assert report.events_scanned == 1
        assert report.by_lane["swarm"].units == 1


def test_the_window_filter_bounds_both_readers(store: SqliteStore, observing: None) -> None:
    _seed_events_over_a_soak(store, lane="swarm", clean_units=4, dirty_units=0, hours=100.0)
    recent = scope_gate_report(store, window_hours=1.0)
    assert recent.events_scanned < 4
    everything = scope_gate_report(store)
    assert everything.events_scanned == 4

    recent_protocol = scope_gate_report(ProtocolOnlyStore(store), window_hours=1.0)
    assert recent_protocol.events_scanned == recent.events_scanned


def test_the_observed_window_cannot_be_manufactured_by_asking_for_one(
    store: SqliteStore, observing: None
) -> None:
    """``window_hours`` bounds the query; it does not lengthen the evidence."""
    for _ in range(MIN_GATE_UNITS + 5):
        _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    report = scope_gate_report(store, window_hours=SOAK_WINDOW_HOURS * 10)
    assert report.by_lane["swarm"].window_hours < SOAK_WINDOW_HOURS
    assert report.verdict("swarm") == "insufficient_data"


def test_the_report_is_readable_after_the_flag_is_switched_off(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read must not be gated: the soak's result outlives the soak."""
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, "shadow")
    _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    monkeypatch.setenv(SCOPE_OBSERVE_ENV, "off")
    assert scope_observe_enabled() is False
    assert scope_gate_report(store).by_lane["swarm"].units == 1


def test_the_report_on_an_empty_store_is_empty_not_an_error(store: SqliteStore) -> None:
    report = scope_gate_report(store)
    assert report.by_lane == {}
    assert report.by_realm == {}
    assert report.events_scanned == 0
    assert report.truncated is False
    assert report.verdict("swarm") == "insufficient_data"
    assert report.as_dict()["lanes"] == {}


def test_the_scan_cap_reports_truncation(store: SqliteStore, observing: None) -> None:
    for _ in range(5):
        _seed_unit(store, lane="swarm", declared=["src"], actual=["src/a.py"])
    capped = scope_gate_report(store, max_events=2)
    assert capped.truncated is True
    assert capped.events_scanned == 2
    capped_protocol = scope_gate_report(ProtocolOnlyStore(store), max_events=2)
    assert capped_protocol.truncated is True


def test_module_constants_state_the_rollout_rule() -> None:
    """The gate number lives in code, not only in a plan document."""
    assert PRECISION_GATE == 0.99
    assert SOAK_WINDOW_HOURS == 72.0
    assert "precision >= 0.99" in (observe.__doc__ or "")
    assert "72 hours" in (observe.__doc__ or "")
    assert "HANG" in (observe.__doc__ or "")
