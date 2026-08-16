"""The nightly briefing is written and read through ONE path chain.

Regression for the split brain that made a SUCCESSFUL reflection score as a
failed night: ``reflection.report`` wrote to ``contracts.default_vault_dir()``
(``OMNIAGENTOS_VAULT_DIR`` → ``var/runtime/vault``, as launch-env.sh and
the reflection plists set it) while both readers — the reflection watchdog and
the health sentinel — were anchored on a hardcoded ``<repo>/vault``. Nothing
threw; the loop simply reported FAIL about a file it had just written, and filed
its ALERT into a second vault nobody reads.

These tests assert AGREEMENT between the components rather than any particular
literal path, so moving the vault (a new env var, a new default) cannot
re-open the gap without failing here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.reflection.report import generate_reflection_report
from omniagentos.reflection.watchdog import ReflectionWatchdog

_SENTINEL_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "health-sentinel" / "health_sentinel.py"
)

DATE = "2026-07-26"


def _load_sentinel() -> Any:
    """Load the standalone sentinel by path, exactly the way launchd runs it."""
    name = "health_sentinel_vault_split_brain"
    spec = importlib.util.spec_from_file_location(name, _SENTINEL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def runtime_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A relocated vault, the way every launcher relocates it in production."""
    vault = tmp_path / "runtime" / "vault"
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))
    return vault


def test_watchdog_reads_the_briefing_the_writer_wrote(tmp_path: Path, runtime_vault: Path) -> None:
    """Writer default resolution == watchdog resolution, end to end."""
    db_path = str(tmp_path / "state.sqlite3")
    SqliteStore(db_path)  # migrate an empty control plane; the report tolerates no rows.

    # No vault_dir argument: the writer resolves it exactly as the nightly does.
    written = Path(generate_reflection_report(date_str=DATE, db_path=db_path)).resolve()
    assert written.is_file()
    # The relocated vault is the one that got written -- not <repo>/vault.
    assert runtime_vault.resolve() in written.parents

    watchdog = ReflectionWatchdog(db_path)
    assert watchdog.briefing_path(DATE).resolve() == written

    ok, err = watchdog.check_briefing_written(DATE)
    assert ok is True, err
    assert err == ""


def test_watchdog_alert_lands_beside_the_briefing_it_reports(
    tmp_path: Path, runtime_vault: Path
) -> None:
    watchdog = ReflectionWatchdog(str(tmp_path / "state.sqlite3"))
    watchdog.write_alert_briefing(DATE, "No run found", [("SLA & Run Completion", False, "boom")])

    alert = runtime_vault / "briefings" / f"reflection-ALERT-{DATE}.md"
    assert alert.is_file()
    assert alert.parent == watchdog.briefing_path(DATE).parent


def test_health_sentinel_looks_where_the_writer_writes(tmp_path: Path, runtime_vault: Path) -> None:
    """The sentinel's reflection check reads the same directory, at call time."""
    sentinel = _load_sentinel()
    assert sentinel.resolve_briefings_dir() == runtime_vault / "briefings"

    # A briefing the loop really wrote must read as ok, not as "the nightly loop
    # did not produce one".
    db_path = str(tmp_path / "state.sqlite3")
    SqliteStore(db_path)
    today = sentinel._now().date().isoformat()
    written = Path(generate_reflection_report(date_str=today, db_path=db_path))
    assert written.is_file()

    result = sentinel.check_reflection()
    assert result.status == sentinel.OK, result.evidence


def test_health_sentinel_reports_a_missing_briefing_as_fail(runtime_vault: Path) -> None:
    """The check still fails when the vault genuinely has no briefing."""
    sentinel = _load_sentinel()
    (runtime_vault / "briefings").mkdir(parents=True, exist_ok=True)

    result = sentinel.check_reflection()
    assert result.status == sentinel.FAIL
    assert "no reflection briefing" in result.evidence
    # Evidence names the directory it actually consulted, even off-repo.
    assert result.detail["briefings_dir"] == str(runtime_vault / "briefings")
