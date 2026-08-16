"""Activation guards for the feature-health SCHEDULED lane.

The lane's exit code carries no information: ``run.sh`` exits 0 unconditionally in
``--runner launchd`` mode by design (non-blocking lane, never gates merges). So the
only honest oracle for "is the scheduled lane alive?" is LEDGER FRESHNESS, and the
only way freshness can be trusted is if a run that aborted before doing any work
still leaves a dated, failure-marked record. An absent line and a passing line must
never look the same.

Three guards live here:

1. render target — ``install-feature-health.sh`` must REFUSE a non-canonical
   render dir instead of silently succeeding. Three rendered dirs exist on this
   estate and only one of them is ever loaded from; a plist rendered into
   ``var/launchd/rendered.bak-db-path-fix`` points at a pre-fix database, which
   bootstraps into a loaded, green, useless job (favourable absence).
2. exit-code trap — a launchd-mode run that aborts still exits 0 AND still writes
   an ``aborted`` ledger record. Manual mode keeps propagating its exit code.
3. freshness — ``fh.py freshness`` fails when the newest record predates the
   job's cadence, and can be restricted to ``--runner launchd`` so a human's
   manual run cannot mask a dead scheduler.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "scheduler" / "install-feature-health.sh"
RUN_SH = REPO / "scripts" / "feature_health" / "run.sh"
FH = REPO / "scripts" / "feature_health" / "fh.py"
CANONICAL_DIR = REPO / "var" / "launchd" / "rendered"
LABELS = (
    "com.omniagentos.feature-health-tier1",
    "com.omniagentos.feature-health-nightly",
)


def _python() -> str:
    """The interpreter fh.py runs under (repo venv when present, else this one)."""
    venv = REPO / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def _fake_launchctl(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH shim whose launchctl records that it was called, then fails.

    Render-only is a load-bearing convention: the installer must never touch
    launchd. A marker file proves it, where grepping stdout only proves the
    installer did not PRINT the word.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "launchctl-called"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(f"#!/bin/sh\nprintf called > {marker}\nexit 99\n", encoding="utf-8")
    launchctl.chmod(0o755)
    for name in ("python3", "python3.12"):
        (fake_bin / name).symlink_to(sys.executable)
    return fake_bin, marker


def _run_installer(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess, Path]:
    fake_bin, marker = _fake_launchctl(tmp_path)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    # The shared installer expression reads both of these; clear them so each test
    # states its own target explicitly instead of inheriting the caller's shell.
    env.pop("OMNIAGENTOS_LAUNCHD_TARGET_DIR", None)
    env.pop("OMNIAGENTOS_VAR_DIR", None)
    env.pop("OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK", None)
    env.update(overrides)
    result = subprocess.run(
        ["/bin/sh", str(INSTALLER)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, marker


# --------------------------------------------------------------- 1. render target


def test_installer_refuses_noncanonical_target_dir(tmp_path: Path) -> None:
    """A render into the wrong dir is silent today; it must become a loud refusal."""
    target = tmp_path / "rendered.bak-db-path-fix"
    result, marker = _run_installer(
        tmp_path, OMNIAGENTOS_LAUNCHD_TARGET_DIR=str(target)
    )

    assert result.returncode != 0, (
        "installer rendered into a non-canonical dir instead of refusing\n"
        f"stdout:\n{result.stdout}"
    )
    assert "non-canonical" in result.stderr
    # A refusal must name its own remedy, not just say no.
    assert str(CANONICAL_DIR) in result.stderr
    assert "OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK" in result.stderr
    assert not list(tmp_path.glob("**/*.plist"))
    assert not marker.exists()


def test_installer_refuses_noncanonical_var_dir(tmp_path: Path) -> None:
    """OMNIAGENTOS_VAR_DIR is the other way a render silently lands off-canon."""
    result, marker = _run_installer(
        tmp_path, OMNIAGENTOS_VAR_DIR=str(tmp_path / "runtime")
    )

    assert result.returncode != 0, result.stdout
    assert "non-canonical" in result.stderr
    assert not list(tmp_path.glob("**/*.plist"))
    assert not marker.exists()


def test_installer_renders_both_labels_under_explicit_ack(tmp_path: Path) -> None:
    target = tmp_path / "rendered"
    result, marker = _run_installer(
        tmp_path,
        OMNIAGENTOS_LAUNCHD_TARGET_DIR=str(target),
        OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK="1",
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not marker.exists(), "installer invoked launchctl — render-only convention broken"
    for label in LABELS:
        payload = plistlib.loads((target / f"{label}.plist").read_bytes())
        assert payload["Label"] == label
        args = payload["ProgramArguments"]
        assert args[:2] == ["/bin/sh", "-c"]
        # A login shell would re-source launch-env.sh and repoint the DB.
        assert "-l" not in args
        assert "--runner launchd" in args[2]


def test_installer_default_target_is_canonical_with_no_env_at_all(tmp_path: Path) -> None:
    """The PRODUCTION path: a plain shell, no OMNIAGENTOS_* set.

    Every other installer test forces a target, so a regression that reintroduced
    `var/runtime/launchd/rendered` as the default — the exact bug this lane
    exists to fix — could ship with all of them green. Runs against a copied tree
    so the assertion costs the real canonical dir nothing.
    """
    fake_root = tmp_path / "repo"
    (fake_root / "scripts" / "scheduler").mkdir(parents=True)
    (fake_root / "scripts" / "feature_health").mkdir(parents=True)
    (fake_root / "scripts" / "scheduler" / "install-feature-health.sh").write_bytes(
        INSTALLER.read_bytes()
    )
    (fake_root / "scripts" / "feature_health" / "run.sh").write_bytes(RUN_SH.read_bytes())

    fake_bin, marker = _fake_launchctl(tmp_path)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    for name in ("OMNIAGENTOS_LAUNCHD_TARGET_DIR", "OMNIAGENTOS_VAR_DIR",
                 "OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK"):
        env.pop(name, None)
    result = subprocess.run(
        ["/bin/sh", str(fake_root / "scripts" / "scheduler" / "install-feature-health.sh")],
        cwd=tmp_path,  # deliberately NOT the repo root: resolution must not use cwd
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not marker.exists()
    canonical = fake_root / "var" / "launchd" / "rendered"
    assert sorted(p.name for p in canonical.glob("*.plist")) == sorted(
        f"{label}.plist" for label in LABELS
    )
    # Nothing may land in the decoy the shared installer expression resolves to.
    assert not (fake_root / "var" / "runtime").exists()
    assert "NON-CANONICAL" not in result.stdout
    for label in LABELS:
        assert str(canonical / f"{label}.plist") in result.stdout


def test_rendered_plists_attest_launchd_execution(tmp_path: Path) -> None:
    """FH_LAUNCHD=1 is what lets run.sh tell a scheduled run from a hand-typed one."""
    target = tmp_path / "rendered"
    result, _marker = _run_installer(
        tmp_path,
        OMNIAGENTOS_LAUNCHD_TARGET_DIR=str(target),
        OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK="1",
    )

    assert result.returncode == 0, result.stderr
    for label in LABELS:
        payload = plistlib.loads((target / f"{label}.plist").read_bytes())
        assert payload["EnvironmentVariables"]["FH_LAUNCHD"] == "1"


def test_noncanonical_ack_render_warns_against_bootstrapping_it(tmp_path: Path) -> None:
    target = tmp_path / "rendered"
    result, _marker = _run_installer(
        tmp_path,
        OMNIAGENTOS_LAUNCHD_TARGET_DIR=str(target),
        OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK="1",
    )

    assert result.returncode == 0, result.stderr
    assert "NON-CANONICAL DRY-RUN RENDER" in result.stdout


def test_installer_prints_enable_before_bootstrap(tmp_path: Path) -> None:
    """`bootstrap` alone is a no-op for a label in launchd's DISABLED override DB.

    Five estate labels already sit in it, so instructions that stop at bootstrap
    produce a bootstrap that looks like it worked and a job that never runs.
    """
    target = tmp_path / "rendered"
    result, _marker = _run_installer(
        tmp_path,
        OMNIAGENTOS_LAUNCHD_TARGET_DIR=str(target),
        OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK="1",
    )

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "print-disabled" in out
    gui = f"gui/{os.getuid()}"
    for label in LABELS:
        enable_at = out.find(f"launchctl enable {gui}/{label}")
        bootstrap_at = out.find(f"{label}.plist", out.find("bootstrap"))
        assert enable_at != -1, f"no enable line for {label}\n{out}"
        assert bootstrap_at != -1, f"no bootstrap line for {label}\n{out}"
        assert enable_at < bootstrap_at, f"enable must precede bootstrap for {label}\n{out}"


# ------------------------------------------------------------- 2. exit-code trap


def _ledger_records(var_dir: Path) -> list[dict]:
    records: list[dict] = []
    for shard in sorted(var_dir.glob("ledger-*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _abort_run(tmp_path: Path, runner: str, mode: str = "tier1") -> tuple[subprocess.CompletedProcess, Path]:
    """Force the contention guard (FH_MAX_LOAD=0 is always exceeded)."""
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    env = {**os.environ, "FH_VAR_DIR": str(var_dir), "FH_MAX_LOAD": "0"}
    if runner == "launchd":
        # What the rendered plists set; without it the run is refused as unattested.
        env["FH_LAUNCHD"] = "1"
    result = subprocess.run(
        ["/bin/bash", str(RUN_SH), mode, "--runner", runner],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, var_dir


def test_launchd_abort_exits_zero_and_still_writes_a_failure_record(tmp_path: Path) -> None:
    result, var_dir = _abort_run(tmp_path, "launchd")

    assert result.returncode == 0, (
        "launchd mode must always exit 0 — the ledger is the oracle\n"
        f"stderr:\n{result.stderr}"
    )
    records = _ledger_records(var_dir)
    assert len(records) == 1, f"aborted run left {len(records)} ledger records: {records}"
    record = records[0]
    assert record["schema"] == "feature-health.v1"
    assert record["status"] == "error"
    assert record["aborted"] is True
    assert record["did_not_run"] is True
    assert record["abort_reason"] == "load-guard"
    assert record["runner"] == "launchd"
    assert record["tier"] == "tier1"
    assert record["passed"] == 0


def test_manual_abort_keeps_propagating_its_exit_code(tmp_path: Path) -> None:
    """The non-blocking contract is launchd-only; manual runs still report failure."""
    result, var_dir = _abort_run(tmp_path, "manual")

    assert result.returncode == 2, result.stderr
    records = _ledger_records(var_dir)
    assert [r["runner"] for r in records] == ["manual"]


def test_abort_falls_back_to_a_hand_written_line_when_the_interpreter_is_gone(
    tmp_path: Path,
) -> None:
    """The interpreter-missing abort cannot use fh.py — it writes the line itself.

    That hand-written JSON is the one record no Python code validates, so this
    asserts it parses and carries the same keys a fh.py-written abort does.
    """
    fake_root = tmp_path / "repo"
    (fake_root / "scripts" / "feature_health").mkdir(parents=True)
    for name in ("run.sh", "fh.py"):
        (fake_root / "scripts" / "feature_health" / name).write_bytes(
            (REPO / "scripts" / "feature_health" / name).read_bytes()
        )
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    # No .venv under fake_root, so $PY is missing and the fallback is the only path.
    result = subprocess.run(
        ["/bin/bash", str(fake_root / "scripts" / "feature_health" / "run.sh"),
         "tier2", "--runner", "launchd"],
        cwd=fake_root,
        env={**os.environ, "FH_VAR_DIR": str(var_dir), "FH_LAUNCHD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    records = _ledger_records(var_dir)
    assert len(records) == 1, records
    record = records[0]
    assert record["schema"] == "feature-health.v1"
    assert record["status"] == "error"
    assert record["aborted"] is True
    assert record["did_not_run"] is True
    assert record["abort_reason"] == "interpreter-missing"
    assert record["runner"] == "launchd"
    assert record["tier"] == "tier2"
    assert record["report_path"] is None
    # Must be readable by the same freshness oracle the operator will run.
    assert _freshness(var_dir, 4 * 3600, runner="launchd").returncode == 0


def test_abort_records_one_row_per_tier_for_mode_all(tmp_path: Path) -> None:
    _result, var_dir = _abort_run(tmp_path, "launchd", mode="all")

    tiers = sorted(r["tier"] for r in _ledger_records(var_dir))
    assert tiers == ["tier1", "tier2", "tier3"]


# ---------------------------------------------------------------- 3. freshness


def _write_ledger(
    var_dir: Path, when: datetime, *, runner: str = "launchd", tier: str = "tier1"
) -> None:
    record = {
        "schema": "feature-health.v1",
        "ts": when.isoformat(timespec="seconds"),
        "tier": tier,
        "feature": "dag",
        "env": "isolated",
        "status": "ok",
        "passed": 3,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "runner": runner,
    }
    shard = var_dir / f"ledger-{when.strftime('%Y%m')}.jsonl"
    with shard.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _freshness(
    var_dir: Path,
    max_age_s: int,
    *,
    runner: str | None = None,
    tier: str | None = None,
) -> subprocess.CompletedProcess:
    cmd = [_python(), str(FH), "freshness", "--max-age-s", str(max_age_s), "--json"]
    if runner:
        cmd += ["--runner", runner]
    if tier:
        cmd += ["--tier", tier]
    return subprocess.run(
        cmd,
        cwd=REPO,
        env={**os.environ, "FH_VAR_DIR": str(var_dir)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_freshness_fails_when_newest_record_predates_the_cadence(tmp_path: Path) -> None:
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    # tier1 cadence is 4h; a record from last month is the measured status quo
    # (newest shard on disk today is ledger-202607.jsonl).
    _write_ledger(var_dir, datetime.now(UTC) - timedelta(days=32))

    result = _freshness(var_dir, 4 * 3600)

    assert result.returncode != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["age_s"] > 4 * 3600


def test_freshness_passes_on_a_record_inside_the_cadence(tmp_path: Path) -> None:
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    _write_ledger(var_dir, datetime.now(UTC) - timedelta(minutes=5))

    result = _freshness(var_dir, 4 * 3600)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert json.loads(result.stdout)["ok"] is True


def test_freshness_fails_when_the_lane_never_ran(tmp_path: Path) -> None:
    """No ledger at all is the strongest possible staleness, not an absence of news."""
    var_dir = tmp_path / "fh"
    var_dir.mkdir()

    result = _freshness(var_dir, 4 * 3600)

    assert result.returncode != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["age_s"] is None
    assert payload["newest_ts"] is None


def test_freshness_runner_filter_ignores_manual_runs(tmp_path: Path) -> None:
    """A human running the lane by hand must not make a dead scheduler look alive."""
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    _write_ledger(var_dir, datetime.now(UTC) - timedelta(days=32), runner="launchd")
    _write_ledger(var_dir, datetime.now(UTC) - timedelta(minutes=5), runner="manual")

    unfiltered = _freshness(var_dir, 4 * 3600)
    assert unfiltered.returncode == 0

    filtered = _freshness(var_dir, 4 * 3600, runner="launchd")
    assert filtered.returncode != 0, filtered.stdout
    assert json.loads(filtered.stdout)["ok"] is False


def test_freshness_per_tier_catches_a_dead_nightly_behind_a_healthy_tier1(
    tmp_path: Path,
) -> None:
    """Two labels schedule this lane; an unfiltered oracle grades only the louder one.

    tier1 comes solely from the 4-hourly job and tier2 solely from the nightly one.
    A healthy tier1 must not certify a nightly that is disabled, never bootstrapped,
    or dying every night.
    """
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    _write_ledger(var_dir, datetime.now(UTC) - timedelta(minutes=5), tier="tier1")
    _write_ledger(var_dir, datetime.now(UTC) - timedelta(days=9), tier="tier2")

    # Unfiltered: green, because the healthy job's record is the newest one.
    assert _freshness(var_dir, 4 * 3600, runner="launchd").returncode == 0
    assert _freshness(var_dir, 4 * 3600, runner="launchd", tier="tier1").returncode == 0

    stale = _freshness(var_dir, 30 * 3600, runner="launchd", tier="tier2")
    assert stale.returncode != 0, stale.stdout
    payload = json.loads(stale.stdout)
    assert payload["ok"] is False
    assert payload["tier_filter"] == ["tier2"]


# ------------------------------------------------- 4. runner=launchd attestation


def test_run_sh_refuses_an_unattested_launchd_claim(tmp_path: Path) -> None:
    """`--runner launchd` is a claim; only the plists' FH_LAUNCHD=1 backs it.

    Without this gate a single hand-typed command mints a "scheduled" record and
    the freshness oracle reads green while nothing is bootstrapped.
    """
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    env = {**os.environ, "FH_VAR_DIR": str(var_dir), "FH_MAX_LOAD": "0"}
    env.pop("FH_LAUNCHD", None)
    result = subprocess.run(
        ["/bin/bash", str(RUN_SH), "tier1", "--runner", "launchd"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64, result.stdout + result.stderr
    assert "FH_LAUNCHD=1" in result.stderr
    assert _ledger_records(var_dir) == [], "an unattested run still wrote a ledger record"


def test_fh_abort_refuses_an_unattested_launchd_claim(tmp_path: Path) -> None:
    """The gate must hold at the ledger writer too, not only at the runner."""
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    env = {**os.environ, "FH_VAR_DIR": str(var_dir)}
    env.pop("FH_LAUNCHD", None)
    result = subprocess.run(
        [_python(), str(FH), "abort", "--tier", "tier1", "--reason", "spoof",
         "--runner", "launchd"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "FH_LAUNCHD=1" in result.stderr
    assert _ledger_records(var_dir) == []


def test_fh_append_refuses_an_unattested_launchd_claim(tmp_path: Path) -> None:
    """`append` writes the ordinary records — it needs the same gate as `abort`.

    A gate on only one of the two writers leaves the other minting scheduled
    evidence, which is worse than no gate because it looks covered.
    """
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    report = tmp_path / "report.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuites><testsuite name="t" tests="1">'
        '<testcase classname="tests.x" name="ok" time="0.1"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    env = {**os.environ, "FH_VAR_DIR": str(var_dir)}
    env.pop("FH_LAUNCHD", None)
    result = subprocess.run(
        [_python(), str(FH), "append", "--tier", "tier1", "--report", str(report),
         "--runner", "launchd"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "FH_LAUNCHD=1" in result.stderr
    assert _ledger_records(var_dir) == []


def test_abort_fallback_recovers_from_a_lock_left_by_a_dead_run(tmp_path: Path) -> None:
    """A lock dir outliving its owner must not silently degrade to unlocked appends."""
    fake_root = tmp_path / "repo"
    (fake_root / "scripts" / "feature_health").mkdir(parents=True)
    for name in ("run.sh", "fh.py"):
        (fake_root / "scripts" / "feature_health" / name).write_bytes(
            (REPO / "scripts" / "feature_health" / name).read_bytes()
        )
    var_dir = tmp_path / "fh"
    var_dir.mkdir()
    stale_lock = var_dir / ".abort.lock"
    stale_lock.mkdir()
    old = datetime.now(UTC) - timedelta(minutes=30)
    os.utime(stale_lock, (old.timestamp(), old.timestamp()))

    result = subprocess.run(
        ["/bin/bash", str(fake_root / "scripts" / "feature_health" / "run.sh"),
         "tier1", "--runner", "launchd"],
        cwd=fake_root,
        env={**os.environ, "FH_VAR_DIR": str(var_dir), "FH_LAUNCHD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert len(_ledger_records(var_dir)) == 1
    # The stale lock is reclaimed, and the run releases what it took.
    assert not stale_lock.exists()


# --------------------------------------------- 5. abort records poison nothing


def test_abort_records_do_not_corrupt_summary_issues_or_latest(tmp_path: Path) -> None:
    """`__lane__` records carry `report_path: null` and no failures.

    They flow through the same readers as real records, so assert the readers
    survive them rather than reasoning that they should.
    """
    _result, var_dir = _abort_run(tmp_path, "launchd", mode="all")
    env = {**os.environ, "FH_VAR_DIR": str(var_dir)}

    for argv in (["summary"], ["summary", "--json"], ["issues"], ["issues", "--json"],
                 ["render-latest"]):
        result = subprocess.run(
            [_python(), str(FH), *argv],
            cwd=REPO, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, f"{argv} failed:\n{result.stderr}"

    grid = json.loads(
        subprocess.run([_python(), str(FH), "summary", "--json"], cwd=REPO, env=env,
                       capture_output=True, text=True, check=False).stdout
    )
    # The lane's own row is visible and reads as an error, not as clean coverage.
    assert grid["__lane__"]["tier1"]["status"] == "error"
    assert grid["__lane__"]["tier1"]["passed"] == 0
    assert (var_dir / "LATEST.md").is_file()


def test_abort_record_satisfies_freshness_so_a_dead_lane_is_distinguishable(
    tmp_path: Path,
) -> None:
    """The whole point: an aborted scheduled run is FRESH but not OK.

    Freshness alone proves the scheduler fired; the record's status proves whether
    it did any work. If an abort wrote nothing, those two would be the same signal.
    """
    _result, var_dir = _abort_run(tmp_path, "launchd")

    fresh = _freshness(var_dir, 4 * 3600, runner="launchd")
    assert fresh.returncode == 0, fresh.stdout
    payload = json.loads(fresh.stdout)
    assert payload["ok"] is True
    assert payload["newest_status"] == "error"
    assert payload["newest_aborted"] is True
