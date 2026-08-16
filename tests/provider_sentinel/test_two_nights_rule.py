"""sentinel's two(N)-consecutive-nights rule: consecutive_fail_streak's unit
behavior, archive_and_persist's disk-archiving contract, and the wiring
through evaluate_alerts (condition (c) -- ok=false N nights running,
compared against var/provider-health/<date>.json archives)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType


def _seed_archive(archive_dir: Path, day: date, results: dict) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{day.isoformat()}.json").write_text(
        json.dumps({"ts": f"{day.isoformat()}T22:30:00Z", "results": results}), encoding="utf-8"
    )


def test_paid_http_providers_receive_explicit_snapshot_rows(sentinel: ModuleType) -> None:
    assert {"fireworks", "moonshot"}.issubset(set(sentinel.DOCTOR_PROVIDERS))
    rows = sentinel._static_paid_provider_health()
    assert set(rows) == {"fireworks", "moonshot"}
    assert all(row["ok"] is False and row["outcome"] == "unprobed" for row in rows.values())


# --------------------------------------------------------------- consecutive_fail_streak


def test_streak_false_when_tonight_ok(sentinel: ModuleType, tmp_path: Path) -> None:
    today = date(2026, 7, 24)
    assert (
        sentinel.consecutive_fail_streak(
            "grok:default", tonight_ok=True, archive_dir=tmp_path, today=today, nights_needed=2
        )
        is False
    )


def test_streak_false_on_first_ever_run_no_archive(sentinel: ModuleType, tmp_path: Path) -> None:
    today = date(2026, 7, 24)
    # No archive directory/file exists at all -- can't prove a repeat, so no streak.
    assert (
        sentinel.consecutive_fail_streak(
            "grok:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=2
        )
        is False
    )


def test_streak_true_when_yesterday_also_failed(sentinel: ModuleType, tmp_path: Path) -> None:
    today = date(2026, 7, 24)
    yesterday = today - timedelta(days=1)
    _seed_archive(tmp_path, yesterday, {"grok:default": {"ok": False}})
    assert (
        sentinel.consecutive_fail_streak(
            "grok:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=2
        )
        is True
    )


def test_streak_false_when_yesterday_was_ok(sentinel: ModuleType, tmp_path: Path) -> None:
    today = date(2026, 7, 24)
    yesterday = today - timedelta(days=1)
    _seed_archive(tmp_path, yesterday, {"grok:default": {"ok": True}})
    assert (
        sentinel.consecutive_fail_streak(
            "grok:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=2
        )
        is False
    )


def test_streak_false_when_key_absent_from_archive(sentinel: ModuleType, tmp_path: Path) -> None:
    today = date(2026, 7, 24)
    yesterday = today - timedelta(days=1)
    _seed_archive(tmp_path, yesterday, {"codex:default": {"ok": True}})  # different key
    assert (
        sentinel.consecutive_fail_streak(
            "grok:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=2
        )
        is False
    )


def test_streak_needs_all_n_minus_1_prior_nights(sentinel: ModuleType, tmp_path: Path) -> None:
    """nights_needed=3 requires yesterday AND the day before both failing --
    a partial history breaks the streak conservatively."""
    today = date(2026, 7, 24)
    yesterday = today - timedelta(days=1)
    two_nights_ago = today - timedelta(days=2)

    _seed_archive(tmp_path, yesterday, {"gemini:default": {"ok": False}})
    # two_nights_ago missing entirely -> cannot prove the third night.
    assert (
        sentinel.consecutive_fail_streak(
            "gemini:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=3
        )
        is False
    )

    _seed_archive(tmp_path, two_nights_ago, {"gemini:default": {"ok": False}})
    assert (
        sentinel.consecutive_fail_streak(
            "gemini:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=3
        )
        is True
    )


def test_streak_breaks_on_corrupt_archive_file(sentinel: ModuleType, tmp_path: Path) -> None:
    today = date(2026, 7, 24)
    yesterday = today - timedelta(days=1)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{yesterday.isoformat()}.json").write_text("not json{{{", encoding="utf-8")
    assert (
        sentinel.consecutive_fail_streak(
            "grok:default", tonight_ok=False, archive_dir=tmp_path, today=today, nights_needed=2
        )
        is False
    )


# --------------------------------------------------------------------- archive_and_persist


def test_first_run_has_no_previous_and_creates_no_archive(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    health_path = tmp_path / "provider-health.json"
    archive_dir = tmp_path / "provider-health"

    previous, payload = sentinel.archive_and_persist(
        {"grok:default": {"ok": False}},
        health_path=health_path,
        archive_dir=archive_dir,
        ts="2026-07-24T22:30:00Z",
    )

    assert previous is None
    assert payload == {"ts": "2026-07-24T22:30:00Z", "results": {"grok:default": {"ok": False}}}
    assert json.loads(health_path.read_text()) == payload
    assert not archive_dir.exists() or list(archive_dir.iterdir()) == []


def test_second_run_archives_prior_night_and_overwrites_health_json(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    health_path = tmp_path / "provider-health.json"
    archive_dir = tmp_path / "provider-health"

    sentinel.archive_and_persist(
        {"grok:default": {"ok": False}},
        health_path=health_path,
        archive_dir=archive_dir,
        ts="2026-07-23T22:30:05Z",
    )

    previous, payload = sentinel.archive_and_persist(
        {"grok:default": {"ok": False}, "codex:default": {"ok": True}},
        health_path=health_path,
        archive_dir=archive_dir,
        ts="2026-07-24T22:30:05Z",
    )

    assert previous == {"ts": "2026-07-23T22:30:05Z", "results": {"grok:default": {"ok": False}}}
    archived = json.loads((archive_dir / "2026-07-23.json").read_text())
    assert archived == previous
    assert json.loads(health_path.read_text()) == payload
    assert payload["results"]["codex:default"]["ok"] is True


def test_same_night_rerun_never_clobbers_an_already_archived_prior_night(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    health_path = tmp_path / "provider-health.json"
    archive_dir = tmp_path / "provider-health"

    # Night 1.
    sentinel.archive_and_persist(
        {"grok:default": {"ok": False}},
        health_path=health_path,
        archive_dir=archive_dir,
        ts="2026-07-23T22:30:00Z",
    )
    # Night 2, first run: archives night 1 for real.
    sentinel.archive_and_persist(
        {"grok:default": {"ok": True}},
        health_path=health_path,
        archive_dir=archive_dir,
        ts="2026-07-24T22:30:00Z",
    )
    night_1_archive_mtime = (archive_dir / "2026-07-23.json").stat().st_mtime_ns

    # Night 2, SECOND run (e.g. a manual re-run): "previous" is now night 2's
    # own first-run payload, dated the SAME day -- archiving it must not
    # touch the night-1 archive that already exists.
    sentinel.archive_and_persist(
        {"grok:default": {"ok": True}},
        health_path=health_path,
        archive_dir=archive_dir,
        ts="2026-07-24T23:00:00Z",
    )
    assert (archive_dir / "2026-07-23.json").stat().st_mtime_ns == night_1_archive_mtime
    assert json.loads((archive_dir / "2026-07-23.json").read_text())["results"] == {
        "grok:default": {"ok": False}
    }


# --------------------------------------------------------------------- evaluate_alerts wiring


def test_evaluate_alerts_fires_repeat_failure_without_disabling(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    """End-to-end: a non-auth-shaped failure repeated for
    consecutive_fail_nights produces a 'doctor_repeat_failure' alert but
    NEVER sets mark_error -- only an auth-shaped failure disables an
    account, never the two/N-nights condition alone."""
    archive_dir = tmp_path / "provider-health"
    today = date(2026, 7, 24)
    yesterday = today - timedelta(days=1)
    _seed_archive(archive_dir, yesterday, {"grok:default": {"ok": False, "provider": "grok"}})

    doctor_results = {
        "grok:default": {
            "provider": "grok",
            "account_id": None,
            "ok": False,
            "error": "GrokCliExitError: exited 1 with no recognizable output",
        }
    }
    alerts = sentinel.evaluate_alerts(
        doctor_results=doctor_results,
        previous_results=None,
        usages=[],
        policy=dict(sentinel.DEFAULT_POLICY),
        archive_dir=archive_dir,
        today=today,
    )

    issues = {a.issue for a in alerts}
    assert "doctor_repeat_failure" in issues
    assert "auth_failure" not in issues
    assert all(not a.mark_error for a in alerts)
