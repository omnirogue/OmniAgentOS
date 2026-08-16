"""Targeted regression tests for launchd self-healing.

The sentinel is a standalone script, so load it by path and mock every
``launchctl`` invocation.  These tests must be safe on developer machines and
CI runners that do not have macOS launchd.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "health-sentinel" / "health_sentinel.py"


def _load() -> Any:
    name = "health_sentinel_healing_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sentinel = _load()
LABEL = "com.omniagentos.api"
GATE_WORKSPACE = Path("/mock/gate-workspace")


def test_run_replaces_invalid_utf8_from_subprocess() -> None:
    rc, stdout = sentinel._run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'process: \\xe2\\n')"]
    )

    assert rc == 0
    assert stdout == "process: \ufffd\n"


def _load_drift_detector() -> Any:
    path = _SCRIPT.with_name("mechanism_drift_detector.py")
    name = "mechanism_drift_detector_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_check_providers_surfaces_outcomes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stamp = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    snapshot = tmp_path / "provider-health.json"
    snapshot.write_text(
        json.dumps(
            {
                "ts": "2026-08-05T12:00:00Z",
                "results": {
                    "grok:default": {
                        "ok": False,
                        "clean_exit": False,
                        "outcome": "harness_error",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel, "PROVIDER_HEALTH", snapshot)
    monkeypatch.setattr(sentinel, "_now", lambda: stamp)

    result = sentinel.check_providers()

    assert result.status == sentinel.FAIL
    assert result.detail["reasons"]["grok:default"] == "outcome=harness_error, clean_exit=false"


def test_check_providers_warns_for_freshness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    snapshot = tmp_path / "provider-health.json"
    snapshot.write_text(
        json.dumps(
            {
                "ts": (now - timedelta(hours=37)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "results": {"grok:default": {"ok": True}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel, "PROVIDER_HEALTH", snapshot)
    monkeypatch.setattr(sentinel, "_now", lambda: now)

    result = sentinel.check_providers()

    assert result.status == sentinel.WARN
    assert "snapshot" in result.evidence


@pytest.mark.parametrize(
    "status",
    [
        {"ok": True, "outcome": "mystery"},
        "not-a-status-mapping",
    ],
)
def test_check_providers_warns_for_unparseable_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: object
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    snapshot = tmp_path / "provider-health.json"
    snapshot.write_text(
        json.dumps({"ts": now.isoformat(), "results": {"grok:default": status}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel, "PROVIDER_HEALTH", snapshot)
    monkeypatch.setattr(sentinel, "_now", lambda: now)

    result = sentinel.check_providers()

    assert result.status == sentinel.WARN
    assert "unparseable" in result.evidence


def test_provider_outcomes_match_provider_doctor_vocabulary() -> None:
    """health_sentinel._PROVIDER_OUTCOMES must equal the REAL outcome vocabulary
    classify_provider_doctor_outcome can actually produce -- derived by driving
    it with representative status dicts, not by hardcoding two literals that
    could silently drift apart. The sentinel deliberately imports nothing from
    omniagentos (scripts/health-sentinel/health_sentinel.py); this TEST is
    allowed to import, since it lives in tests/.
    """
    from omniagentos.swarm.provider_exec import classify_provider_doctor_outcome

    # One representative status dict per outcome class classify_provider_doctor_outcome
    # can produce (omniagentos/swarm/provider_exec.py + omniagentos/longhaul/limits.py's
    # classify_limit_text, which it delegates limit-shaped errors to).
    representative_statuses: dict[str, tuple[str, dict[str, Any]]] = {
        "ok": ("grok", {"ok": True}),
        "auth_error": ("grok", {"ok": False, "error": "HTTP 401: invalid API key"}),
        "quota_exhausted": (
            "grok",
            {"ok": False, "probe_status": "error", "probe_error": "out of credits"},
        ),
        "transient_rate_limit": (
            "gemini",
            {"ok": False, "probe_status": "error", "probe_error": "resource_exhausted"},
        ),
        "overloaded": (
            "grok",
            {"ok": False, "probe_status": "error", "probe_error": "model is overloaded"},
        ),
        "harness_error": (
            "gemini",
            {"ok": False, "probe_status": "error", "probe_error": "process exited 1"},
        ),
        "unavailable": ("kimi", {"ok": False}),
    }

    # Every representative input must land on the outcome key it was chosen
    # for -- otherwise the fixture above is stale, not the vocabulary.
    for expected_outcome, (provider, status) in representative_statuses.items():
        assert classify_provider_doctor_outcome(provider, status) == expected_outcome

    derived_outcomes = frozenset(
        classify_provider_doctor_outcome(provider, status)
        for provider, status in representative_statuses.values()
    )

    assert sentinel._PROVIDER_OUTCOMES == derived_outcomes


def _write_accounts_yaml(path: Path, accounts: list[tuple[str, str, bool]]) -> None:
    rows = "".join(
        f"      - id: {account_id}\n"
        f"        config_dir: {config_dir}\n"
        f"        enabled: {'true' if enabled else 'false'}\n"
        for account_id, config_dir, enabled in accounts
    )
    path.write_text(f"providers:\n  claude:\n    accounts:\n{rows}", encoding="utf-8")


def _write_claude_db(path: Path, rows: list[dict[str, object]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE claude_accounts ("
            "id TEXT PRIMARY KEY, label TEXT, config_dir TEXT, enabled INTEGER, "
            "status TEXT, status_detail TEXT, updated_at TEXT, provider TEXT, "
            "paused_until TEXT, cooldown_until TEXT)"
        )
        connection.executemany(
            "INSERT INTO claude_accounts "
            "(id, label, config_dir, enabled, status, status_detail, updated_at, provider, "
            "paused_until, cooldown_until) VALUES "
            "(:id, :id, :config_dir, :enabled, 'ok', NULL, :updated_at, 'claude', "
            ":paused_until, :cooldown_until)",
            rows,
        )


def _configure_claude_pool_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    yaml_accounts: list[tuple[str, str, bool]],
    db_rows: list[dict[str, object]],
) -> None:
    accounts_path = tmp_path / "accounts.yaml"
    db_path = tmp_path / "state.sqlite3"
    _write_accounts_yaml(accounts_path, yaml_accounts)
    _write_claude_db(db_path, db_rows)
    monkeypatch.setattr(sentinel, "ACCOUNTS_YAML", accounts_path)
    monkeypatch.setenv("OMNIAGENTOS_DB", str(db_path))
    monkeypatch.setattr(
        sentinel,
        "_credential_verdict",
        lambda config_dir: (sentinel.OK, "fixture credentials valid", {"path": str(config_dir)}),
    )


def test_check_claude_pool_warns_for_enabled_db_row_missing_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    configured = str(tmp_path / "configured")
    db_only = str(tmp_path / "db-only")
    _configure_claude_pool_fixture(
        monkeypatch,
        tmp_path,
        yaml_accounts=[("configured", configured, True)],
        db_rows=[
            {
                "id": "configured",
                "config_dir": configured,
                "enabled": 1,
                "updated_at": now.isoformat(),
                "paused_until": None,
                "cooldown_until": None,
            },
            {
                "id": "db-only",
                "config_dir": db_only,
                "enabled": 1,
                "updated_at": now.isoformat(),
                "paused_until": None,
                "cooldown_until": None,
            },
        ],
    )
    monkeypatch.setattr(sentinel, "_now", lambda: now)

    result = sentinel.check_claude_pool()

    assert result.status == sentinel.WARN
    assert {item["kind"] for item in result.detail["disagreements"]} == {"config_missing"}
    assert result.detail["disagreements"][0]["config_dir"] == db_only
    assert "1 config/DB enabled-state disagreement(s)" not in result.evidence
    assert "1 disagreement(s): config_missing" in result.evidence


@pytest.mark.parametrize(
    ("window_field", "expected_kind"),
    [("paused_until", "db_paused"), ("cooldown_until", "db_cooling")],
)
def test_check_claude_pool_warns_for_active_db_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    window_field: str,
    expected_kind: str,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    config_dir = str(tmp_path / "configured")
    row: dict[str, object] = {
        "id": "configured",
        "config_dir": config_dir,
        "enabled": 1,
        "updated_at": now.isoformat(),
        "paused_until": None,
        "cooldown_until": None,
    }
    row[window_field] = (now + timedelta(hours=1)).isoformat()
    _configure_claude_pool_fixture(
        monkeypatch,
        tmp_path,
        yaml_accounts=[("configured", config_dir, True)],
        db_rows=[row],
    )
    monkeypatch.setattr(sentinel, "_now", lambda: now)

    result = sentinel.check_claude_pool()

    assert result.status == sentinel.WARN
    assert {item["kind"] for item in result.detail["disagreements"]} == {expected_kind}


def _mock_gate_workspace(monkeypatch: pytest.MonkeyPatch, *, exists: bool = True) -> None:
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(GATE_WORKSPACE))
    monkeypatch.setattr(Path, "is_dir", lambda path: exists and path == GATE_WORKSPACE)


def _gate_git_responder(
    *,
    workspace_sha: str = "a" * 40,
    main_sha: str = "b" * 40,
    dirty: str = "",
    main_rc: int = 0,
    ancestor_rc: int = 0,
    commits_behind: int = 5,
) -> Any:
    """Return a hermetic responder for this check's tiny git command surface."""

    def fake_run(argv: list[str]) -> tuple[int, str]:
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return 0, f"{workspace_sha}\n"
        if argv[-3:] == ["rev-parse", "--verify", "main^{commit}"]:
            return main_rc, f"{main_sha}\n" if main_rc == 0 else ""
        if "status" in argv:
            return 0, dirty
        if "merge-base" in argv:
            return ancestor_rc, ""
        if "rev-list" in argv:
            return 0, f"{commits_behind}\n"
        raise AssertionError(f"unexpected git call: {argv}")

    return fake_run


def test_gate_workspace_staleness_warns_until_workspace_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gate_workspace(monkeypatch, exists=False)

    result = sentinel.check_gate_workspace_staleness()

    assert result.status == sentinel.WARN
    assert result.evidence == "gate workspace not yet pinned"
    assert result.detail == {"workspace": str(GATE_WORKSPACE)}


def test_gate_workspace_staleness_fails_for_dirty_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gate_workspace(monkeypatch)
    monkeypatch.setattr(sentinel, "_run", _gate_git_responder(dirty=" M changed.py\n"))

    result = sentinel.check_gate_workspace_staleness()

    assert result.status == sentinel.FAIL
    assert "is dirty" in result.evidence


def test_gate_workspace_staleness_warns_with_commit_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gate_workspace(monkeypatch)
    monkeypatch.setattr(sentinel, "_run", _gate_git_responder(commits_behind=5))

    result = sentinel.check_gate_workspace_staleness()

    assert result.status == sentinel.WARN
    assert "behind main by 5 commits" in result.evidence
    assert result.detail["workspace_sha"] == "a" * 40
    assert result.detail["main_sha"] == "b" * 40
    assert result.detail["commits_behind"] == 5


def test_gate_workspace_staleness_is_ok_at_main_tip(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_gate_workspace(monkeypatch)
    sha = "c" * 40
    monkeypatch.setattr(sentinel, "_run", _gate_git_responder(workspace_sha=sha, main_sha=sha))

    result = sentinel.check_gate_workspace_staleness()

    assert result.status == sentinel.OK
    assert result.evidence == "gate workspace is current with main"
    assert result.detail["workspace_sha"] == sha
    assert result.detail["main_sha"] == sha


def test_gate_workspace_staleness_handles_missing_main_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gate_workspace(monkeypatch)
    monkeypatch.setattr(sentinel, "_run", _gate_git_responder(main_rc=1))

    result = sentinel.check_gate_workspace_staleness()

    assert result.status == sentinel.WARN
    assert "main is not reachable" in result.evidence


def test_gate_workspace_staleness_is_registered_in_main_check_loop() -> None:
    assert (
        dict(sentinel.CHECKS)["gate_workspace_staleness"] is sentinel.check_gate_workspace_staleness
    )


def _failed_launchd(**detail: Any) -> Any:
    return sentinel.CheckResult(
        "launchd",
        sentinel.FAIL,
        "launchd failure",
        {"installed_not_loaded": [], "nonzero_last_exit": {}, "loaded": [], **detail},
    )


def test_not_loaded_job_uses_bootstrap_and_records_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sentinel, "LAUNCH_AGENTS_DIR", tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="loaded", stderr="")

    monkeypatch.setattr(sentinel.subprocess, "run", fake_run)
    result = _failed_launchd(installed_not_loaded=[LABEL])

    sentinel._apply_kickstart(result)

    assert calls == [
        ["launchctl", "bootstrap", f"gui/{sentinel.os.getuid()}", str(tmp_path / f"{LABEL}.plist")]
    ]
    assert result.detail["heal_attempts"] == [
        {
            "label": LABEL,
            "status": "ok",
            "verb": "bootstrap",
            "rc": 0,
            "stdout": "loaded",
            "stderr": "",
            "attempt": 1,
        }
    ]


def test_bad_exit_job_that_is_running_is_killed_and_relaunched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live instance that keeps failing has to be killed before it restarts."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sentinel.subprocess, "run", fake_run)
    result = _failed_launchd(
        nonzero_last_exit={LABEL: "1"},
        nonzero_last_exit_running=[LABEL],
        loaded=[LABEL],
    )

    sentinel._apply_kickstart(result)

    assert calls == [["launchctl", "kickstart", "-k", f"gui/{sentinel.os.getuid()}/{LABEL}"]]
    assert result.detail["heal_attempts"][0]["verb"] == "kickstart-kill"
    assert result.detail["heal_attempts"][0]["status"] == "ok"


def test_bad_exit_job_that_is_down_is_started_not_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-k`` on a DOWN KeepAlive job kills the instance launchd just respawned.

    That is the 2026-08-05 API outage: every sentinel pass saw ``pid = -`` with
    ``exit = -15``, issued ``kickstart -k``, SIGTERMed the freshly started
    uvicorn, and re-armed its own trigger for the next pass.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sentinel.subprocess, "run", fake_run)
    result = _failed_launchd(nonzero_last_exit={LABEL: "-15"}, loaded=[LABEL])

    sentinel._apply_kickstart(result)

    assert calls == [["launchctl", "kickstart", f"gui/{sentinel.os.getuid()}/{LABEL}"]]
    assert "-k" not in calls[0]
    assert result.detail["heal_attempts"][0]["verb"] == "kickstart"
    assert result.detail["heal_attempts"][0]["status"] == "ok"


def test_check_launchd_reports_which_failing_jobs_are_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer side of the down-vs-running distinction ``_apply_kickstart`` needs.

    Without this key the healer cannot tell a wedged live instance (kill it)
    from a service launchd is already respawning (leave the respawn alone).
    """
    down = f"{sentinel.LAUNCHD_PREFIX}api"
    wedged = f"{sentinel.LAUNCHD_PREFIX}runner"
    for label in (down, wedged):
        (tmp_path / f"{label}.plist").write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(sentinel, "LAUNCH_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sentinel, "RENDERED_LAUNCHD_DIR", tmp_path / "rendered")
    monkeypatch.setattr(
        sentinel,
        "_launchctl_table",
        lambda: ({down: ("-", "-15"), wedged: ("4242", "1")}, None),
    )

    detail = sentinel.check_launchd().detail

    assert set(detail["nonzero_last_exit"]) == {down, wedged}
    assert detail["nonzero_last_exit_running"] == [wedged]


def test_failed_heal_is_recorded_with_capped_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="o" * 501, stderr="failed")

    monkeypatch.setattr(sentinel.subprocess, "run", fake_run)
    monkeypatch.setattr(sentinel, "_launchctl_table", lambda: ({}, None))
    result = _failed_launchd(installed_not_loaded=[LABEL])

    sentinel._apply_kickstart(result)

    attempt = result.detail["heal_attempts"][0]
    assert attempt["status"] == "error"
    assert attempt["rc"] == 1
    assert attempt["stderr"] == "failed"
    assert len(attempt["stdout"]) == 500
    assert attempt["stdout"].endswith("...")


def test_transient_oserror_retries_once_then_records_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        return subprocess.CompletedProcess(argv, 0, stdout="recovered", stderr="")

    monkeypatch.setattr(sentinel.subprocess, "run", fake_run)
    monkeypatch.setattr(sentinel.time, "sleep", sleeps.append)
    result = _failed_launchd(installed_not_loaded=[LABEL])

    sentinel._apply_kickstart(result)

    assert calls == 2
    assert sleeps == [0.1]
    assert [entry["attempt"] for entry in result.detail["heal_attempts"]] == [1, 2]
    assert result.detail["heal_attempts"][0]["status"] == "error"
    assert result.detail["heal_attempts"][0]["error"]
    assert result.detail["heal_attempts"][1]["status"] == "ok"


def test_conflicting_launchd_state_is_visible_and_not_healed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def should_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("state-conflicted job must not be healed")

    monkeypatch.setattr(sentinel.subprocess, "run", should_not_run)
    result = _failed_launchd(installed_not_loaded=[LABEL], loaded=[LABEL])

    sentinel._apply_kickstart(result)

    assert result.status == sentinel.FAIL
    assert result.detail["state_conflicts"] == [LABEL]
    assert result.detail["state_assertion"].startswith("FAIL:")
    assert result.detail["heal_attempts"] == []


def _launchd_remedy_check(label: str, failure_class: str) -> Any:
    detail: dict[str, object] = {
        "installed_not_loaded": [],
        "rendered_not_installed": [],
    }
    detail[failure_class] = [label]
    return sentinel.CheckResult("launchd", sentinel.FAIL, "launchd failure", detail)


def test_remedies_report_only_by_default_and_write_open_remedies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    label = "com.omniagentos.report-only"
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "remedy_ledger.json"
    monkeypatch.delenv("OMNIAGENTOS_SENTINEL_AUTOREMEDY", raising=False)
    monkeypatch.setattr(sentinel, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")

    def should_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("report-only remedies must not run launchctl")

    monkeypatch.setattr(sentinel.subprocess, "run", should_not_run)
    result = _launchd_remedy_check(label, "installed_not_loaded")

    sentinel._record_launchd_remedies(result, now=now, ledger_path=ledger)
    snapshot = {
        "ts": sentinel._iso(now),
        "checks": [result.as_dict()],
    }
    alert = sentinel.write_alert_briefing(snapshot, today=now.date(), briefings_dir=tmp_path)
    text = alert.read_text(encoding="utf-8")
    entry = json.loads(ledger.read_text(encoding="utf-8"))[f"{label}:installed_not_loaded"]

    assert "## OPEN REMEDIES" in text
    assert label in text
    assert "launchctl bootstrap gui/" in text
    assert "awaiting operator action or `OMNIAGENTOS_SENTINEL_AUTOREMEDY=1`" in text
    assert entry["first_seen"] == sentinel._iso(now)
    assert entry["days_recurring"] == 1
    assert entry["last_filed_date"] == "2026-08-09"


def test_autoremedy_bootstraps_unloaded_signature_only_once_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    label = "com.omniagentos.autoremedy-test"
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    calls: list[list[str]] = []
    monkeypatch.setenv("OMNIAGENTOS_SENTINEL_AUTOREMEDY", "1")
    monkeypatch.setattr(sentinel, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sentinel.subprocess, "run", fake_run)
    ledger = tmp_path / "remedy_ledger.json"
    sentinel._record_launchd_remedies(
        _launchd_remedy_check(label, "installed_not_loaded"), now=now, ledger_path=ledger
    )
    sentinel._record_launchd_remedies(
        _launchd_remedy_check(label, "installed_not_loaded"), now=now, ledger_path=ledger
    )

    assert calls == [
        ["launchctl", "bootstrap", f"gui/{sentinel.os.getuid()}", str(tmp_path / "LaunchAgents" / f"{label}.plist")]
    ]


def test_autoremedy_never_executes_rendered_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    label = "com.omniagentos.rendered-only"
    monkeypatch.setenv("OMNIAGENTOS_SENTINEL_AUTOREMEDY", "1")

    def should_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("rendered-not-installed must stay report-only")

    monkeypatch.setattr(sentinel.subprocess, "run", should_not_run)

    sentinel._record_launchd_remedies(
        _launchd_remedy_check(label, "rendered_not_installed"),
        now=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        ledger_path=tmp_path / "remedy_ledger.json",
    )


def test_malformed_holds_yaml_is_crash_safe_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # yaml.YAMLError is not a ValueError: a malformed HOLDS.yaml must neither
    # crash the sentinel run nor read as "no holds" — with the hold list
    # untrustworthy, autoremedy stands down while remedies are still filed.
    label = "com.omniagentos.reflection-nightly"
    monkeypatch.setenv("OMNIAGENTOS_SENTINEL_AUTOREMEDY", "1")
    holds_path = tmp_path / "HOLDS.yaml"
    holds_path.write_text("holds: [unclosed\n", encoding="utf-8")

    def should_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unreadable HOLDS.yaml must disable autoremedy")

    monkeypatch.setattr(sentinel.subprocess, "run", should_not_run)

    assert sentinel._held_launchd_labels(holds_path=holds_path) is None
    ledger_path = tmp_path / "remedy_ledger.json"
    sentinel._record_launchd_remedies(
        _launchd_remedy_check(label, "installed_not_loaded"),
        now=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        ledger_path=ledger_path,
        holds_path=holds_path,
    )
    assert ledger_path.exists(), "remedies must still be FILED when holds are unreadable"


def test_mechanism_registry_fails_for_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry.jsonl"
    output = tmp_path / "stale.log"
    output.write_text("old evidence\n", encoding="utf-8")
    old = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    output.touch()
    output.chmod(0o600)
    os.utime(output, (old, old))
    registry.write_text(
        json.dumps(
            {
                "id": "stale-mechanism",
                "schedule": "every hour",
                "expected_output_path": str(output),
                "freshness_SLA": 60,
                "named_consumer": ["test-consumer"],
                "state": "disabled",
                "enabled": False,
                "installed": False,
                "launchd_label": None,
                "last_known_good_evidence": "test fixture",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel, "_now", lambda: datetime(2026, 8, 5, tzinfo=UTC))
    monkeypatch.setattr(sentinel, "_launchctl_table", lambda: ({}, None))

    result = sentinel.check_mechanism_registry(registry_path=registry)

    assert result.status == sentinel.FAIL
    assert "stale-mechanism" in result.evidence


def test_mechanism_registry_fails_when_registry_is_missing(tmp_path: Path) -> None:
    result = sentinel.check_mechanism_registry(registry_path=tmp_path / "missing.jsonl")

    assert result.status == sentinel.FAIL


def test_mechanism_registry_warns_for_future_dated_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    registry = tmp_path / "registry.jsonl"
    output = tmp_path / "future.log"
    output.write_text("future evidence\n", encoding="utf-8")
    future = (now + timedelta(hours=2)).timestamp()
    os.utime(output, (future, future))
    registry.write_text(
        json.dumps(
            {
                "id": "future-mechanism",
                "schedule": "hourly",
                "expected_output_path": str(output),
                "freshness_SLA": 3600,
                "named_consumer": ["test-consumer"],
                "state": "enabled",
                "enabled": True,
                "installed": True,
                "launchd_label": None,
                "last_known_good_evidence": "test fixture",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel, "_now", lambda: now)
    monkeypatch.setattr(sentinel, "_launchctl_table", lambda: ({}, None))

    result = sentinel.check_mechanism_registry(registry_path=registry)

    assert result.status != sentinel.OK
    assert result.detail["skewed"] == [
        "future-mechanism (output 2h00m in the future; clock/copy fault)"
    ]


def test_mechanism_registry_reports_launchd_drift_in_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    registry = tmp_path / "registry.jsonl"
    output = tmp_path / "fresh.log"
    output.write_text("fresh evidence\n", encoding="utf-8")
    fresh = (now - timedelta(minutes=1)).timestamp()
    os.utime(output, (fresh, fresh))
    registry.write_text(
        json.dumps(
            {
                "id": "declared-enabled-mechanism",
                "schedule": "hourly",
                "expected_output_path": str(output),
                "freshness_SLA": 3600,
                "named_consumer": ["test-consumer"],
                "state": "enabled",
                "enabled": True,
                "installed": True,
                "launchd_label": "com.omniagentos.declared-enabled",
                "last_known_good_evidence": "test fixture",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel, "_now", lambda: now)
    monkeypatch.setattr(sentinel, "_launchctl_table", lambda: ({}, None))

    result = sentinel.check_mechanism_registry(registry_path=registry)

    assert result.status == sentinel.FAIL
    assert result.detail["drifts"]
    assert result.detail["drifts"][0]["id"] == "declared-enabled-mechanism"


def test_drift_detector_reports_enable_disable_mismatch(tmp_path: Path) -> None:
    detector = _load_drift_detector()
    registry = tmp_path / "registry.jsonl"
    registry.write_text(
        json.dumps(
            {
                "id": "fake-enabled-mechanism",
                "schedule": "hourly",
                "expected_output_path": str(tmp_path / "output.log"),
                "freshness_SLA": 3600,
                "named_consumer": ["test-consumer"],
                "state": "enabled",
                "enabled": True,
                "installed": True,
                "launchd_label": "com.omniagentos.fake-enabled",
                "last_known_good_evidence": "test fixture",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    drifts, error = detector.detect_drift(registry_path=registry, loaded_labels=set())

    assert error is None
    assert drifts == [
        {
            "id": "fake-enabled-mechanism",
            "launchd_label": "com.omniagentos.fake-enabled",
            "launchd_enabled": False,
            "registry_enabled": True,
        }
    ]


def test_drift_detector_recognizes_idle_calendar_interval_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _load_drift_detector()
    captured_output = "PID\tStatus\tLabel\n-\t0\tcom.some.label\n"

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=captured_output, stderr="")

    monkeypatch.setattr(detector.subprocess, "run", fake_run)

    loaded, error = detector._loaded_labels()

    assert error is None
    assert loaded == {"com.some.label"}
