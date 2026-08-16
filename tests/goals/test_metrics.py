from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.goals.metrics import get_metric_value, ledger_merges_per_hour
from omniagentos.steward.store import StewardStore


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> StewardStore:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    return StewardStore(SqliteStore(db_path))


def _count_rows(store: StewardStore, table: str) -> int:
    return int(store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _init_repo(path: Path, *commits: str) -> list[str]:
    """git init -b main + one empty commit per message; returns their shas.

    Hermetic: global config and signing are disabled so a host gitconfig
    (hooks, gpg) can never flake these tests.
    """
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    run = lambda *a: subprocess.run(a, cwd=path, check=True, capture_output=True, env=env)  # noqa: E731
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True, env=env
    )
    run("git", "config", "user.email", "metrics@example.invalid")
    run("git", "config", "user.name", "Metrics Test")
    run("git", "config", "commit.gpgsign", "false")
    shas = []
    for message in commits:
        run("git", "commit", "--allow-empty", "-m", message)
        shas.append(
            subprocess.check_output(["git", "rev-parse", "main"], cwd=path, text=True).strip()
        )
    return shas


def test_ledger_merges_uses_merge_sha_and_stayed_landed(
    store: StewardStore, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    sha, second_sha, foreign_only_sha = _init_repo(
        tmp_path, "landed", "also landed", "foreign-only"
    )

    def _merged(merge_sha: str, **extra: object) -> dict[str, object]:
        return {
            "event": "merged",
            "ts": now.isoformat(),
            "detail": {"merge_sha": merge_sha, **extra},
        }

    rows = [
        # r4: SIX namings of ONE commit (full + abbreviations) count ONCE —
        # dedup keys on the RESOLVED oid, not the raw string.
        _merged(sha),
        _merged(sha[:7]),
        _merged(sha[:10]),
        _merged(sha[:16], receipt="r", pushed=True),
        _merged(second_sha, receipt="r", pushed=True),
        # r4: a bogus sha is an unverifiable DATUM — skipped, never a global
        # fault (this row's deletion in r3 hid exactly that inflation bug).
        _merged("0" * 40),
        # r5: LOAD-BEARING foreign row — this commit is named ONLY here, so
        # deleting the repo filter changes the rate (r4's row was inert).
        _merged(foreign_only_sha, repo="ThreeLoops"),
        # r5: ref names resolve but are not object ids — agent-typable for free.
        _merged("main"),
        _merged("HEAD"),
    ]
    ledger = tmp_path / "var" / "loopqueue" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    # Two distinct real landed commits in a 30-minute window = 4.0/hour: the
    # foreign-only commit, the bogus sha, and both ref names must not count.
    assert (
        ledger_merges_per_hour(store, repo_path=tmp_path, now=now, window=timedelta(minutes=30))
        == 4.0
    )


def test_ledger_blind_window_is_unmeasurable(store: StewardStore, tmp_path: Path) -> None:
    """r5: a window where EVERY sha is unresolvable and nothing landed is
    BLIND (None) — indistinguishability from a quiet window was the lie."""
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    _init_repo(tmp_path, "root")
    ledger = tmp_path / "var" / "loopqueue" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = [
        {"event": "merged", "ts": now.isoformat(), "detail": {"merge_sha": "1" * 40}},
        {"event": "merged", "ts": now.isoformat(), "detail": {"merge_sha": "2" * 40}},
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert ledger_merges_per_hour(store, repo_path=tmp_path, now=now) is None


def test_ledger_measured_zero_and_missing_file_differ_without_writes(
    store: StewardStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    # r4 fault taxonomy: measured zero requires WORKING instruments — a real
    # repo with a main ref makes "quiet window" and "no instruments" differ.
    _init_repo(tmp_path, "root")
    ledger = tmp_path / "var" / "loopqueue" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"event": "merged", "ts": (now - timedelta(hours=2)).isoformat(), "detail": {}})
        + "\n"
    )
    before = _count_rows(store, "goal_readings")
    assert ledger_merges_per_hour(store, repo_path=tmp_path, now=now) == 0.0
    ledger.unlink()
    assert ledger_merges_per_hour(store, repo_path=tmp_path, now=now) is None
    # A readable ledger inside a directory with NO git repo is an instrument
    # fault too — None, never a confident zero.
    no_git = tmp_path.parent / f"{tmp_path.name}-nogit"  # SIBLING: outside the repo
    (no_git / "var" / "loopqueue").mkdir(parents=True)
    (no_git / "var" / "loopqueue" / "ledger.jsonl").write_text("")
    # r5: fence git's upward walk — with a basetemp inside some outer checkout
    # the sibling would otherwise resolve THAT repo's main and flip None→0.0.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(no_git))
    assert ledger_merges_per_hour(store, repo_path=no_git, now=now) is None
    assert _count_rows(store, "goal_readings") == before


def test_snapshot_absence_and_read_only(store: StewardStore) -> None:
    first = store.upsert_goal({"name": "metric-first", "north_star": {}})
    second = store.upsert_goal({"name": "metric-second", "north_star": {}})
    assert get_metric_value(store, "metric_snapshot:collector:revenue", goal_id=first["id"]) is None
    for goal, value in ((first, 4.5), (second, 999.0)):
        store.insert_metric_snapshot(
            {"goal_id": goal["id"], "source": "collector", "metric": "revenue", "value": value}
        )
    before = _count_rows(store, "metric_snapshots")
    assert get_metric_value(store, "metric_snapshot:collector:revenue", goal_id=first["id"]) == 4.5
    assert (
        get_metric_value(store, "metric_snapshot:collector:revenue", goal_id=second["id"]) == 999.0
    )
    assert _count_rows(store, "metric_snapshots") == before


def test_gate_pass_rate_absence_and_read_only(store: StewardStore) -> None:
    with pytest.raises(KeyError):
        get_metric_value(store, "gate_pass_rate:missing")
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO routines (id, name, trigger_type, trigger_config_json, task_template_json, "
        "gate_type, gate_config_json, hard_cap_type, hard_cap_value, notification_target_json, created_at, updated_at) "
        "VALUES (?, ?, 'cron', '{}', '{}', 'exit_code', '{}', 'max_iterations', 1, '{}', ?, ?)",
        ("r_zero", "zero", now, now),
    )
    before = _count_rows(store, "routine_runs")
    assert get_metric_value(store, "gate_pass_rate:zero") is None
    assert _count_rows(store, "routine_runs") == before


def test_northstar_cert_absence_does_not_write(
    store: StewardStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r5: absence is asserted against an EXPLICIT absent path — the default
    resolves to the real package root, where a live cert DB exists in
    production (the r4 default-path assertion went red the moment it landed)."""
    before = (_count_rows(store, "eval_suites"), _count_rows(store, "eval_results"))
    assert (
        get_metric_value(store, "northstar_cert", cert_db_path=tmp_path / "absent.sqlite3") is None
    )
    # A torn/corrupt cert file is a quiet instrument None, never an exception.
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\xde\xad" * 64)
    assert get_metric_value(store, "northstar_cert", cert_db_path=corrupt) is None
    assert (_count_rows(store, "eval_suites"), _count_rows(store, "eval_results")) == before


def test_default_state_paths_resolve_via_runtime_paths(
    store: StewardStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r5 anchor pin, updated for the D3 ratchet: default STATE paths resolve
    through omniagentos.runtime_paths (the env var root is honoured; never
    Path.cwd()), while the git object store keeps the package anchor. Mutating
    either default back to Path.cwd() must still fail this test."""
    from omniagentos.goals import metrics as metrics_module

    monkeypatch.setattr(metrics_module, "_REPO_ROOT", tmp_path)
    _init_repo(tmp_path, "root")
    var_root = tmp_path / "runtime-var"
    (var_root / "loopqueue").mkdir(parents=True)
    (var_root / "loopqueue" / "ledger.jsonl").write_text("")
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_root))
    monkeypatch.chdir(tmp_path.parent)  # cwd deliberately elsewhere
    assert ledger_merges_per_hour(store) == 0.0
    assert get_metric_value(store, "northstar_cert") is None  # absent under var root


def test_resolver_refusal_is_an_instrument_fault_never_a_zero(
    store: StewardStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """r6 negative control: a runtime-root REFUSAL (RuntimePathError) must read
    as an instrument fault — logged None — for BOTH converted metrics. A mutant
    that turns either branch into a favourable 0.0 must fail here."""
    from omniagentos.goals import metrics as metrics_module

    def _refuse(*_args: object, **_kwargs: object) -> Path:
        raise metrics_module.runtime_paths.RuntimePathError("refused for test")

    monkeypatch.setattr(metrics_module.runtime_paths, "resolve_var_root", _refuse)
    monkeypatch.setattr(metrics_module, "_REPO_ROOT", tmp_path)
    _init_repo(tmp_path, "root")
    with caplog.at_level("WARNING"):
        assert ledger_merges_per_hour(store) is None
        assert get_metric_value(store, "northstar_cert") is None
    assert "merges/hr unmeasurable" in caplog.text
    assert "northstar-cert unmeasurable" in caplog.text


def test_northstar_default_reads_the_var_root_never_cwd(
    store: StewardStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r6 negative control: the default cert path must resolve under the env
    var root even when cwd holds no cert DB — a cwd-anchored (or repo-anchored)
    mutant returns None here instead of the seeded 0.01."""
    now, run, suite = utc_now_iso(), "run-cert", "suite-cert"
    store._connection.execute(
        "INSERT INTO eval_suites (id, discipline, created_at) VALUES (?, 'northstar_cert', ?)",
        (suite, now),
    )
    rows = [("pass", {"pass": 1.0, "fail": 0.0, "not_evaluable": 0.0, "void": 0.0}, {"reason": {}})]
    rows += [
        (f"ne-{i}", {"pass": 0.0, "fail": 0.0, "not_evaluable": 1.0, "void": 0.0}, {})
        for i in range(99)
    ]
    store._connection.executemany(
        "INSERT INTO eval_results (id, experiment_id, arm, suite_id, suite_version, split, per_case_json, created_at) "
        "VALUES (?, ?, 'champion', ?, 1, 'dev', ?, ?)",
        [
            (f"result-{case}", run, suite, json.dumps({case: metrics, **reason}), now)
            for case, metrics, reason in rows
        ],
    )
    store._connection.commit()
    var_root = tmp_path / "runtime-var"
    (var_root / "northstar-cert").mkdir(parents=True)
    # backup API, not a file copy: the store runs WAL, so a byte copy of the
    # main DB file silently drops uncheckpointed rows.
    import sqlite3

    with sqlite3.connect(var_root / "northstar-cert" / "results.sqlite3") as cert_db:
        store._connection.backup(cert_db)
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_root))
    monkeypatch.chdir(tmp_path)  # cwd holds no cert DB — a cwd mutant reads None
    assert get_metric_value(store, "northstar_cert") == pytest.approx(0.01)


def test_northstar_cert_aggregates_real_writer_rows(store: StewardStore) -> None:
    now, run, suite = utc_now_iso(), "run-cert", "suite-cert"
    store._connection.execute(
        "INSERT INTO eval_suites (id, discipline, created_at) VALUES (?, 'northstar_cert', ?)",
        (suite, now),
    )
    rows = [("pass", {"pass": 1.0, "fail": 0.0, "not_evaluable": 0.0, "void": 0.0}, {"reason": {}})]
    rows += [
        (f"ne-{i}", {"pass": 0.0, "fail": 0.0, "not_evaluable": 1.0, "void": 0.0}, {})
        for i in range(99)
    ]
    store._connection.executemany(
        "INSERT INTO eval_results (id, experiment_id, arm, suite_id, suite_version, split, per_case_json, created_at) "
        "VALUES (?, ?, 'champion', ?, 1, 'dev', ?, ?)",
        [
            (f"result-{case}", run, suite, json.dumps({case: metrics, **reason}), now)
            for case, metrics, reason in rows
        ],
    )
    value = get_metric_value(store, "northstar_cert", cert_db_path=os.environ["OMNIAGENTOS_DB"])
    assert value is not None and value < 1.0
    assert value == pytest.approx(0.01)


def test_mission_direct_no_terminal_rows_is_none_and_read_only(store: StewardStore) -> None:
    before = (_count_rows(store, "board_tasks"), _count_rows(store, "company_goals"))
    assert get_metric_value(store, "mission_direct") is None
    assert (_count_rows(store, "board_tasks"), _count_rows(store, "company_goals")) == before


def test_mission_direct_counts_achieved_goals(store: StewardStore) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES ('co', 'co', 'Co', 'active', ?)",
        (now,),
    )
    store._connection.executemany(
        "INSERT INTO company_goals (id, org_company_id, title, horizon, status, created_at, updated_at) VALUES (?, 'co', ?, 'long_term', ?, ?, ?)",
        [("active", "Active", "active", now, now), ("achieved", "Achieved", "achieved", now, now)],
    )
    store._connection.executemany(
        "INSERT INTO board_tasks (id, title, status, goal_id, created_at, updated_at) VALUES (?, ?, 'done', ?, ?, ?)",
        [
            ("task-active", "Active", "active", now, now),
            ("task-achieved", "Achieved", "achieved", now, now),
        ],
    )
    assert get_metric_value(store, "mission_direct") == 1.0


@pytest.mark.parametrize(
    "source", ["nonsense", "metric_snapshot:collector", "metric_snapshot::metric"]
)
def test_unknown_or_malformed_source_raises_key_error(store: StewardStore, source: str) -> None:
    with pytest.raises(KeyError):
        get_metric_value(store, source)
