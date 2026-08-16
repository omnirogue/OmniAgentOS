"""Auth-shape gating: classify_doctor_failure's structured-first invariant,
evaluate_alerts's auth_failure/mark_error wiring, and emit_alerts's real
mark_status("error") side effect -- ONLY for a genuinely auth-shaped
failure, NEVER for a transient one, no matter how it is phrased."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import ModuleType

from omniagentos.accounts import service as accounts_service
from omniagentos.db.migrate import migrate


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "sentinel.db")
    migrate(db)
    return db


def _config_dir(base: Path, name: str) -> str:
    directory = base / name
    directory.mkdir(exist_ok=True)
    return str(directory)


# --------------------------------------------------------------- classify_doctor_failure


def test_ok_status_is_never_classified_even_with_auth_looking_text(sentinel: ModuleType) -> None:
    # STRUCTURED-FIRST: a clean run (ok=True) must never be reclassified by a
    # coincidental phrase -- provider_doctor's own `ok` already settled it.
    status = {"ok": True, "error": "unauthorized 401 invalid api key"}
    assert sentinel.classify_doctor_failure("grok", status) is None


def test_auth_shaped_failure_classifies_as_auth_error(sentinel: ModuleType) -> None:
    status = {"ok": False, "error": "GrokApiError: invalid api key provided"}
    assert sentinel.classify_doctor_failure("grok", status) == "auth_error"


def test_terminal_probe_error_also_classifies_as_auth_error(sentinel: ModuleType) -> None:
    status = {"ok": False, "probe_status": "error", "probe_error": "invalid api key"}
    assert sentinel.classify_doctor_failure("grok", status) == "auth_error"


def test_harness_wrapper_session_id_is_not_auth_shaped(sentinel: ModuleType) -> None:
    status = {
        "ok": False,
        "probe_status": "error",
        "probe_error": "session ses_a401_harness timed out after 65s; kill requested",
    }
    assert sentinel.classify_doctor_failure("grok", status) is None


def test_transient_exit_semantics_failure_is_not_auth_shaped(sentinel: ModuleType) -> None:
    # The exact shape the build brief says to expect from grok/gemini right
    # now: a nonzero/odd exit with no auth phrasing anywhere in it.
    status = {"ok": False, "error": "GeminiCliExitError: process exited 1, no result event"}
    assert sentinel.classify_doctor_failure("gemini", status) is None


def test_failure_with_no_captured_error_text_is_not_auth_shaped(sentinel: ModuleType) -> None:
    # provider_doctor only sets `error` when an exception fired; a purely
    # structural failure (stream_events/clean_exit/kill_within_5s false)
    # leaves no text at all -- must never be guessed as auth-shaped.
    status = {"ok": False, "stream_events": 0, "clean_exit": False, "kill_within_5s": True}
    assert sentinel.classify_doctor_failure("kimi", status) is None


def test_rate_limit_shaped_failure_is_transient_not_auth(sentinel: ModuleType) -> None:
    status = {"ok": False, "error": "429 too many requests, retry later"}
    assert sentinel.classify_doctor_failure("codex", status) == "transient_rate_limit"


# ------------------------------------------------------------------- evaluate_alerts


def test_auth_shaped_failure_produces_alert_with_mark_error(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    doctor_results = {
        "grok:acct_1": {
            "provider": "grok",
            "account_id": "acct_1",
            "ok": False,
            "error": "unauthorized: invalid api key",
        }
    }
    alerts = sentinel.evaluate_alerts(
        doctor_results=doctor_results,
        previous_results=None,
        usages=[],
        policy=dict(sentinel.DEFAULT_POLICY),
        archive_dir=tmp_path / "archive",
        today=date(2026, 7, 24),
    )
    auth_alerts = [a for a in alerts if a.issue == "auth_failure"]
    assert len(auth_alerts) == 1
    assert auth_alerts[0].mark_error is True
    assert auth_alerts[0].account_id == "acct_1"


def test_disable_on_auth_failure_false_still_alerts_but_never_marks(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    policy = dict(sentinel.DEFAULT_POLICY)
    policy["disable_on_auth_failure"] = False
    doctor_results = {
        "grok:acct_1": {
            "provider": "grok",
            "account_id": "acct_1",
            "ok": False,
            "error": "unauthorized: invalid api key",
        }
    }
    alerts = sentinel.evaluate_alerts(
        doctor_results=doctor_results,
        previous_results=None,
        usages=[],
        policy=policy,
        archive_dir=tmp_path / "archive",
        today=date(2026, 7, 24),
    )
    auth_alerts = [a for a in alerts if a.issue == "auth_failure"]
    assert len(auth_alerts) == 1
    assert auth_alerts[0].mark_error is False


def test_transient_failure_never_produces_auth_alert(sentinel: ModuleType, tmp_path: Path) -> None:
    doctor_results = {
        "gemini:default": {
            "provider": "gemini",
            "account_id": None,
            "ok": False,
            "error": "GeminiCliExitError: process exited 1, no result event",
        }
    }
    alerts = sentinel.evaluate_alerts(
        doctor_results=doctor_results,
        previous_results=None,
        usages=[],
        policy=dict(sentinel.DEFAULT_POLICY),
        archive_dir=tmp_path / "archive",
        today=date(2026, 7, 24),
    )
    assert all(a.issue != "auth_failure" for a in alerts)
    assert all(not a.mark_error for a in alerts)


# ------------------------------------------------------------------------ emit_alerts


def test_emit_alerts_marks_status_error_only_for_auth_shaped_account(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    account = accounts_service.add_account(
        label="grok-1", config_dir=_config_dir(tmp_path, "grok-cfg"), enabled=True, db_path=db
    )
    assert accounts_service.get_account(account["id"], db_path=db)["status"] != "error"

    alerts = [
        sentinel.Alert(
            provider="grok",
            account_id=account["id"],
            issue="auth_failure",
            title="Provider health: grok auth failure",
            body="grok:acct failed provider_doctor with an auth-shaped error: invalid api key",
            mark_error=True,
        )
    ]
    sentinel.emit_alerts(alerts, today=date(2026, 7, 24), db_path=db)

    updated = accounts_service.get_account(account["id"], db_path=db)
    assert updated["status"] == "error"
    assert "auth-shaped" in (updated["status_detail"] or "")


def test_emit_alerts_never_marks_status_for_transient_or_repeat_alerts(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    account = accounts_service.add_account(
        label="grok-2", config_dir=_config_dir(tmp_path, "grok-cfg-2"), enabled=True, db_path=db
    )
    original_status = accounts_service.get_account(account["id"], db_path=db)["status"]

    alerts = [
        sentinel.Alert(
            provider="grok",
            account_id=account["id"],
            issue="doctor_repeat_failure",
            title="Provider health: grok doctor failing 2 nights running",
            body="grok:acct has failed provider_doctor for 2 consecutive nights.",
            mark_error=False,
        ),
        sentinel.Alert(
            provider="grok",
            account_id=account["id"],
            issue="session_budget_low",
            title="Provider health: grok session budget low",
            body="grok account x: session window at 95.0% used",
            mark_error=False,
        ),
    ]
    sentinel.emit_alerts(alerts, today=date(2026, 7, 24), db_path=db)

    updated = accounts_service.get_account(account["id"], db_path=db)
    # untouched -- never disable on a transient/repeat-only shape
    assert updated["status"] == original_status
    assert updated["status"] != "error"


def test_emit_alerts_skips_mark_status_when_no_account_id(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    # No registered account at all (default CLI login) -- mark_error=True but
    # account_id=None must never raise or attempt a write.
    alerts = [
        sentinel.Alert(
            provider="grok",
            account_id=None,
            issue="auth_failure",
            title="Provider health: grok auth failure",
            body="grok:default failed provider_doctor with an auth-shaped error",
            mark_error=True,
        )
    ]
    emitted = sentinel.emit_alerts(alerts, today=date(2026, 7, 24), db_path=db)
    assert len(emitted) == 1  # the notification itself still lands
