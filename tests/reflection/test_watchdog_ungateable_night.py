"""The reflection watchdog computes a three-valued night settlement and must
not discard it: an UNGATEABLE night (never ran / still in flight) exits 2
("could not run") and leaves the acceptance floor alone, instead of firing a
CRITICAL alert as if it were a genuine failure.

Before this fix, ``main()`` consulted only the binary ``check_last_run()``:
a night that never ran, a night still in flight (harvest=ok, propose=
running) and a night that genuinely failed (propose=failed) all produced the
IDENTICAL failed-check list from ``run_all_checks()``, and all three fired
the same CRITICAL alert briefing + urgent board card + exit(1).
``check_run_settlement()`` already computed the explicit three-valued
outcome (OK / FAILED / UNGATEABLE) but had zero callers.

These tests pin the full contract from the plan's ``state_mapping``:
UNGATEABLE excludes the four run-scoped checks, always still runs the
run-independent git hard-stop check, never re-enters an in-flight loop,
never reports "healthy", and a run row that positively asserts
``status='failed'`` (set only from an ``except`` branch in ``runner.py``)
always outranks an unrecorded/in-flight stage fold and keeps alerting.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from omniagentos.db.migrate import migrate
from omniagentos.reflection.settlement import Settlement
from omniagentos.reflection.watchdog import (
    _RUN_STATUS_PERMITS_UNGATEABLE,
    _RUN_STATUS_TERMINAL,
    ReflectionWatchdog,
    _settle_with_status_override,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _cols(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(reflection_runs)")]
    finally:
        conn.close()


def _insert_run(db_path: str, run_id: str, started: str, status: str, **stages: str) -> None:
    cols = _cols(db_path)
    fields = ["id", "started_at", "status"]
    values: list[str] = [run_id, started, status]
    for stage, value in stages.items():
        col = f"{stage}_status"
        if col in cols:
            fields.append(col)
            values.append(value)
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"INSERT INTO reflection_runs ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
        values,
    )
    conn.commit()
    conn.close()


def _run_watchdog(tmp_path: Path, date_str: str, db_path: Path) -> subprocess.CompletedProcess:
    """Invoke the real ``python -m omniagentos.reflection.watchdog --date``
    entry point, with all three env vars redirected into ``tmp_path`` so
    nothing live is ever read or written."""
    vault_dir = tmp_path / "vault"
    reflection_dir = tmp_path / "reflection"
    env = dict(os.environ)
    env["OMNIAGENTOS_DB"] = str(db_path)
    env["OMNIAGENTOS_VAULT_DIR"] = str(vault_dir)
    env["OMNIAGENTOS_REFLECTION_DIR"] = str(reflection_dir)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "omniagentos.reflection.watchdog", "--date", date_str],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _alert_files(tmp_path: Path) -> list[Path]:
    vault_dir = tmp_path / "vault"
    if not vault_dir.exists():
        return []
    return [p for p in vault_dir.rglob("reflection-ALERT-*.md")]


def _board_cards(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, priority, status, title FROM board_tasks WHERE discipline='reflection'"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_a_night_with_no_run_is_ungateable_not_failed(tmp_path: Path) -> None:
    """A night with NO run row at all must settle UNGATEABLE, exclude the
    run-scoped checks from scoring, and exit 2 -- not be scored as failed."""
    db_path = tmp_path / "state.sqlite3"
    migrate(str(db_path))
    watchdog = ReflectionWatchdog(str(db_path))

    verdict = watchdog.classify_night("2026-08-09")
    assert verdict.settlement is Settlement.UNGATEABLE

    # The SCORING itself must be excluded, not merely the classifier label
    # -- asserting only check_run_settlement() == UNGATEABLE passes against
    # unmodified main() and pins nothing.
    gateable = verdict.settlement is not Settlement.UNGATEABLE
    assert gateable is False
    results = watchdog.run_all_checks("2026-08-09", include_run_scoped=gateable)
    names = [name for name, _, _ in results]
    assert "SLA & Run Completion" not in names
    assert "Context Budget Caps" not in names
    assert "Morning Briefing File" not in names
    assert "Proposals Schema" not in names
    assert "Git Hard-Stop Boundaries" in names

    proc = _run_watchdog(tmp_path, "2026-08-09", db_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not _alert_files(tmp_path)
    assert not _board_cards(db_path)


def test_a_still_running_night_does_not_raise_a_critical_alert(tmp_path: Path) -> None:
    """A night still IN FLIGHT (harvest=ok, propose=running) must exit 2,
    write no alert briefing, file no urgent card, and never re-enter the
    live loop -- exactly ONE row is seeded so ``get_run_count`` would have
    permitted a re-run (``run_count < 2``) had the in-flight guard not
    suppressed it."""
    date_str = "2026-08-10"
    db_path = tmp_path / "state.sqlite3"
    migrate(str(db_path))
    _insert_run(
        str(db_path), "refr_running", f"{date_str}T02:00:00Z", "running", harvest="ok", propose="running"
    )

    watchdog = ReflectionWatchdog(str(db_path))
    verdict = watchdog.classify_night(date_str)
    assert verdict.settlement is Settlement.UNGATEABLE
    assert verdict.in_flight is True
    assert watchdog.get_run_count(date_str) == 1  # run_count < 2 -- would permit a re-run

    # In-process sentinel: patching here proves the CODE PATH never reaches
    # the call (classify_night + run_all_checks(include_run_scoped=False)
    # alone are enough to keep failed_checks empty, so main()'s auto-rerun
    # branch -- which is gated on ``failed_checks`` -- is never entered).
    with patch(
        "omniagentos.reflection.watchdog.run_reflection_loop",
        side_effect=AssertionError("run_reflection_loop must NEVER be invoked for an in-flight night"),
    ):
        gateable = verdict.settlement is not Settlement.UNGATEABLE
        results = watchdog.run_all_checks(date_str, include_run_scoped=gateable)
        failed_checks = [name for name, ok, _ in results if not ok]
        assert failed_checks == [], (
            "an in-flight night's run-independent checks must pass cleanly here, "
            "which is what keeps main() from ever reaching the auto-rerun branch"
        )

    # End-to-end, real subprocess (a fresh interpreter, so the in-process
    # patch above cannot be inspected there): the real entry point must
    # exit 2, write no alert artifacts, and its own stdout must show the
    # auto-rerun line was never reached.
    proc = _run_watchdog(tmp_path, date_str, db_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not _alert_files(tmp_path)
    assert not any(str(row["priority"]) == "urgent" for row in _board_cards(db_path))
    assert "Attempting exactly ONE observe-only auto re-run" not in proc.stdout


def test_a_crashed_night_still_alerts(tmp_path: Path) -> None:
    """CONTROL: reproduce the live 2026-08-04 shape exactly -- status='failed'
    with harvest='ok', propose='running' (the stage fold ALONE would be
    UNGATEABLE). The positive run-status assertion must outrank the stage
    fold: this night MUST still alert. Paired with a healthy-night case that
    must exit 0, bracketing the change from both sides."""
    date_str = "2026-08-04"
    db_path = tmp_path / "state.sqlite3"
    migrate(str(db_path))
    # Two rows so get_run_count() >= 2 and the auto re-run branch is not
    # taken -- this probe measures the alert path, not the retry path.
    _insert_run(str(db_path), "refr_crash1", f"{date_str}T01:00:00Z", "failed", harvest="ok", propose="running")
    _insert_run(str(db_path), "refr_crash2", f"{date_str}T02:00:00Z", "failed", harvest="ok", propose="running")

    watchdog = ReflectionWatchdog(str(db_path))
    verdict = watchdog.classify_night(date_str)
    assert verdict.settlement is Settlement.FAILED, (
        "a run row asserting status='failed' must outrank an ungateable stage fold"
    )

    proc = _run_watchdog(tmp_path, date_str, db_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _alert_files(tmp_path)
    assert any(str(row["priority"]) == "urgent" for row in _board_cards(db_path))

    # Bracket from the other side: a healthy, fully-completed night settles OK.
    db_path2 = tmp_path / "state2.sqlite3"
    migrate(str(db_path2))
    _insert_run(
        str(db_path2),
        "refr_healthy",
        f"{date_str}T02:00:00Z",
        "completed",
        harvest="ok",
        propose="ok",
        validate="ok",
        apply="ok",
        report="ok",
    )
    watchdog2 = ReflectionWatchdog(str(db_path2))
    verdict2 = watchdog2.classify_night(date_str)
    assert verdict2.settlement is Settlement.OK


def test_an_unknown_run_status_does_not_buy_an_exclusion() -> None:
    """A status outside the narrow permit set can never buy an exclusion by
    being unlisted -- it falls through to FAILED. This is the sibling-family
    test: a fix that special-cases 'failed' passes the other tests and fails
    this one.

    ``reflection_runs.status`` carries a DB-level CHECK constraint limiting
    it to {running, completed, failed}, so an "unrecognised" status can
    never actually reach ``classify_night`` via a real row today -- this
    tests the underlying override rule directly and DB-independently, since
    the fail-closed SHAPE of the rule (enumerate PERMITTED, not FORBIDDEN)
    is the safety property, and must hold even if that constraint is ever
    loosened.
    """
    assert _RUN_STATUS_PERMITS_UNGATEABLE == frozenset({"running", "completed"})
    assert _RUN_STATUS_TERMINAL == frozenset({"completed", "failed"})

    for status in ("crashed", "timeout", "cancelled", "Running", "", "failed"):
        assert _settle_with_status_override(Settlement.UNGATEABLE, status) is Settlement.FAILED, (
            f"status={status!r} must NOT buy an ungateable exclusion"
        )

    # The permitted set is honoured, and a non-ungateable fold passes through
    # unchanged regardless of status (the override only ever narrows FROM
    # UNGATEABLE, never touches OK or an already-FAILED fold).
    for status in ("running", "completed"):
        assert _settle_with_status_override(Settlement.UNGATEABLE, status) is Settlement.UNGATEABLE
    assert _settle_with_status_override(Settlement.OK, "crashed") is Settlement.OK
    assert _settle_with_status_override(Settlement.FAILED, "running") is Settlement.FAILED


def test_ungateable_never_reports_healthy(tmp_path: Path) -> None:
    """The whole reason this proposal is written the way it is: converting
    could-not-grade into a pass would be the favourable-absence defect
    inverted. Both ungateable shapes must exit EXACTLY 2 -- never 0 and
    never 1 -- and print neither healthy message, only the UNGATEABLE
    verdict line."""
    # Shape 1: no run row.
    db_path1 = tmp_path / "no_row" / "state.sqlite3"
    db_path1.parent.mkdir(parents=True)
    migrate(str(db_path1))
    proc1 = _run_watchdog(tmp_path / "no_row", "2026-08-20", db_path1)
    assert proc1.returncode == 2, proc1.stdout + proc1.stderr
    assert "All checks passed" not in proc1.stdout
    assert "Reflection loop healthy" not in proc1.stdout
    assert "UNGATEABLE" in proc1.stdout

    # Shape 2: still in flight.
    date_str = "2026-08-21"
    db_path2 = tmp_path / "in_flight" / "state.sqlite3"
    db_path2.parent.mkdir(parents=True)
    migrate(str(db_path2))
    _insert_run(str(db_path2), "refr_flight", f"{date_str}T02:00:00Z", "running", harvest="ok", propose="running")
    proc2 = _run_watchdog(tmp_path / "in_flight", date_str, db_path2)
    assert proc2.returncode == 2, proc2.stdout + proc2.stderr
    assert "All checks passed" not in proc2.stdout
    assert "Reflection loop healthy" not in proc2.stdout
    assert "UNGATEABLE" in proc2.stdout


def test_run_independent_checks_still_run_on_an_ungateable_night(tmp_path: Path) -> None:
    """A broken exclusion that returns early on UNGATEABLE and skips ALL
    checks would create a NEW favourable absence: 'the night was
    ungateable, so we never noticed the hard-stop violation'. The
    run-independent git hard-stop check must always be present in the
    excluded-mode result list, and a failure there must still exit 1."""
    date_str = "2026-08-22"
    db_path = tmp_path / "state.sqlite3"
    migrate(str(db_path))
    # Pre-seed 2 rows so get_run_count() >= 2 and the auto re-run branch is
    # not taken even though the (patched) hard-stop check fails.
    _insert_run(str(db_path), "refr_a", f"{date_str}T01:00:00Z", "running")
    _insert_run(str(db_path), "refr_b", f"{date_str}T02:00:00Z", "running")

    watchdog = ReflectionWatchdog(str(db_path))
    with patch.object(
        ReflectionWatchdog,
        "check_git_hard_stops",
        return_value=(False, "hard-stop violation: touched forbidden file"),
    ):
        verdict = watchdog.classify_night(date_str)
        gateable = verdict.settlement is not Settlement.UNGATEABLE
        assert gateable is False

        results = watchdog.run_all_checks(date_str, include_run_scoped=gateable)
        names_ok = {name: ok for name, ok, _ in results}
        assert "Git Hard-Stop Boundaries" in names_ok
        assert names_ok["Git Hard-Stop Boundaries"] is False


def test_acceptance_floor_is_reported_not_used_as_the_verdict(tmp_path: Path) -> None:
    """MEASURED trap: an in-flight night with harvest='ok' and the other
    four stages unrecorded returns floor.meets=True (ratio=1.0 over one
    recorded stage) -- that must NOT be wired as the run-level verdict, or
    could-not-grade converts to PASSED. The night must still exit 2 even
    though floor.meets is True, the floor counts must appear in the printed
    verdict line, and acceptance_floor_for() must agree with
    classify_night().floor for the same date."""
    date_str = "2026-08-23"
    db_path = tmp_path / "state.sqlite3"
    migrate(str(db_path))
    _insert_run(str(db_path), "refr_flight", f"{date_str}T02:00:00Z", "running", harvest="ok", propose="running")

    watchdog = ReflectionWatchdog(str(db_path))
    verdict = watchdog.classify_night(date_str)
    assert verdict.floor.meets is True
    assert verdict.floor.ok == 1
    assert verdict.floor.gateable == 1
    assert verdict.settlement is Settlement.UNGATEABLE
    assert watchdog.acceptance_floor_for(date_str) == verdict.floor

    proc = _run_watchdog(tmp_path, date_str, db_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "meets=True" in proc.stdout


def test_regression_control_watchdog_and_scripts_reflection_suites_unedited() -> None:
    """Sentinel: the regression control suites (tests/reflection/test_watchdog.py,
    tests/reflection/test_vault_split_brain.py, tests/scripts/reflection/)
    must run green and UNEDITED alongside this module -- enforced by the
    mechanical pass-list that runs tests/reflection/ and
    tests/scripts/reflection/ in full, not by this test directly. This test
    only documents the requirement so a reader of this module sees it named.
    """
    assert True
