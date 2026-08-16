"""Alert dedupe: exactly one notification per (provider, issue) per calendar
night. emit_alerts embeds the night's date into ref_id, so
record_notification's own unread-ref dedupe naturally scopes to "one per
night" rather than "one forever until read" -- the next night's run must
still be able to alert on a persisting problem."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from omniagentos.notifications.dal import NotificationsDal


def _grok_auth_alert(sentinel: ModuleType) -> Any:
    return sentinel.Alert(
        provider="grok",
        account_id=None,
        issue="auth_failure",
        title="Provider health: grok auth failure",
        body="grok:default failed provider_doctor with an auth-shaped error",
        mark_error=False,
    )


def test_same_alert_twice_same_night_dedupes_to_one_row(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = str(tmp_path / "notif.db")
    today = date(2026, 7, 24)
    alert = _grok_auth_alert(sentinel)

    first = sentinel.emit_alerts([alert], today=today, db_path=db)
    second = sentinel.emit_alerts([alert], today=today, db_path=db)

    assert len(first) == 1
    assert second == []  # deduped: unread notification already targets tonight's ref

    dal = NotificationsDal(db)
    rows = [r for r in dal.list() if r["ref_type"] == "provider_health"]
    assert len(rows) == 1


def test_same_issue_next_night_is_not_suppressed_by_last_nights_unread_row(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = str(tmp_path / "notif.db")
    alert = _grok_auth_alert(sentinel)

    night_1 = sentinel.emit_alerts([alert], today=date(2026, 7, 23), db_path=db)
    night_2 = sentinel.emit_alerts([alert], today=date(2026, 7, 24), db_path=db)

    assert len(night_1) == 1
    assert len(night_2) == 1  # a NEW night must still be able to alert, unread or not
    assert night_1[0] != night_2[0]

    dal = NotificationsDal(db)
    rows = [r for r in dal.list() if r["ref_type"] == "provider_health"]
    assert len(rows) == 2


def test_distinct_issues_same_provider_same_night_both_land(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = str(tmp_path / "notif.db")
    today = date(2026, 7, 24)
    auth_alert = _grok_auth_alert(sentinel)
    repeat_alert = sentinel.Alert(
        provider="grok",
        account_id=None,
        issue="doctor_repeat_failure",
        title="Provider health: grok doctor failing 2 nights running",
        body="grok:default has failed provider_doctor for 2 consecutive nights.",
        mark_error=False,
    )
    emitted = sentinel.emit_alerts([auth_alert, repeat_alert], today=today, db_path=db)
    assert len(emitted) == 2

    dal = NotificationsDal(db)
    rows = [r for r in dal.list() if r["ref_type"] == "provider_health"]
    assert len(rows) == 2
    assert {r["ref_id"].rsplit(":", 1)[0].split(":", 2)[2] for r in rows} == {
        "auth_failure",
        "doctor_repeat_failure",
    }


def test_distinct_providers_same_issue_same_night_do_not_collide(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    db = str(tmp_path / "notif.db")
    today = date(2026, 7, 24)
    grok_alert = _grok_auth_alert(sentinel)
    gemini_alert = sentinel.Alert(
        provider="gemini",
        account_id=None,
        issue="auth_failure",
        title="Provider health: gemini auth failure",
        body="gemini:default failed provider_doctor with an auth-shaped error",
        mark_error=False,
    )
    emitted = sentinel.emit_alerts([grok_alert, gemini_alert], today=today, db_path=db)
    assert len(emitted) == 2
