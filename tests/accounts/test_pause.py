"""Operator-initiated temporary account pause.

Three levers act on rotation and they must stay orthogonal:
``enabled=0`` (operator, permanent), ``cooldown_until`` (limit_state,
automatic), ``paused_until`` (operator, self-expiring). These tests pin the
boundaries between them -- above all that a provider outcome can never erase an
operator pause, which is why the pause is a separate column rather than a reuse
of ``cooldown_until``.

Hermetic: tmp DB with migrations applied, detection stubbed out so the machine's
real ~/.claude accounts are never read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.accounts import service as acc
from omniagentos.db.migrate import migrate
from omniagentos.routing import limit_state


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _future(minutes: float = 30) -> str:
    return _iso(datetime.now(UTC) + timedelta(minutes=minutes))


def _past(minutes: float = 30) -> str:
    return _iso(datetime.now(UTC) - timedelta(minutes=minutes))


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])  # no machine accounts
    path = str(tmp_path / "accounts.db")
    migrate(path)
    return path


def _account(db: str, tmp_path: Path, name: str) -> str:
    directory = tmp_path / name
    directory.mkdir()
    return str(acc.add_account(label=name, config_dir=str(directory), db_path=db)["id"])


# ------------------------------------------------------------------ rotation


def test_pause_removes_account_from_rotation(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")

    assert acc.next_account_for_spawn(db) is not None
    acc.pause_account(only, _future(), "draining for maintenance", db_path=db)

    assert acc.next_account_for_spawn(db) is None


def test_pause_leaves_the_other_accounts_serving(db: str, tmp_path: Path) -> None:
    paused = _account(db, tmp_path, "a")
    _account(db, tmp_path, "b")
    acc.pause_account(paused, _future(), db_path=db)

    picks = {acc.next_account_for_spawn(db).account_id for _ in range(4)}

    assert paused not in picks
    assert len(picks) == 1


def test_expired_pause_restores_rotation_with_no_sweep(db: str, tmp_path: Path) -> None:
    """Availability is evaluated per query, so nothing has to reap the pause.

    This matters because the only caller of ``clear_expired_cooldowns`` is
    longhaul's engine loop -- if the pause depended on a sweeper, an operator
    running without longhaul would find the account never came back.
    """
    only = _account(db, tmp_path, "solo")
    acc.pause_account(only, _past(), "already elapsed", db_path=db)

    assert acc.next_account_for_spawn(db) is not None


# --------------------------------------------- the reason for a separate column


def test_clean_completion_does_not_erase_an_operator_pause(db: str, tmp_path: Path) -> None:
    """``report_outcome('ok')`` NULLs ``cooldown_until`` on every clean run.

    A pause stored in that column would be wiped by the next successful session
    on the account -- silently, and precisely in the common case of pausing an
    account that is currently busy.
    """
    only = _account(db, tmp_path, "solo")
    acc.pause_account(only, _future(), "operator pause", db_path=db)

    limit_state.report_outcome("claude", only, "ok", db_path=db)

    assert acc.next_account_for_spawn(db) is None
    assert acc.get_account(only, db_path=db)["paused"] is True


def test_pause_does_not_shorten_a_live_provider_cooldown(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")
    acc.set_account_cooldown(only, _future(60), "rate limited", db_path=db)

    # A 1-minute pause must not become "available again in 1 minute".
    acc.pause_account(only, _future(1), db_path=db)
    acc.pause_account(only, _past(), db_path=db)  # pause fully elapsed

    assert acc.next_account_for_spawn(db) is None  # cooldown still in force


def test_resume_does_not_override_a_provider_cooldown(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")
    acc.set_account_cooldown(only, _future(60), "rate limited", db_path=db)
    acc.pause_account(only, _future(), db_path=db)

    acc.resume_account(only, db_path=db)

    assert acc.next_account_for_spawn(db) is None
    assert acc.get_account(only, db_path=db)["paused"] is False


def test_resume_lifts_the_pause_early(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")
    acc.pause_account(only, _future(), db_path=db)
    assert acc.next_account_for_spawn(db) is None

    assert acc.resume_account(only, db_path=db) is True

    assert acc.next_account_for_spawn(db) is not None


def test_re_pausing_replaces_the_window(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")
    acc.pause_account(only, _future(600), "long hold", db_path=db)
    acc.pause_account(only, _past(), "changed my mind", db_path=db)

    assert acc.next_account_for_spawn(db) is not None


def test_pause_and_resume_report_missing_accounts(db: str) -> None:
    assert acc.pause_account("acct_nope", _future(), db_path=db) is False
    assert acc.resume_account("acct_nope", db_path=db) is False


# ------------------------------------------------ every read path must agree


def test_all_selection_paths_honor_the_pause(db: str, tmp_path: Path) -> None:
    """A pause honored by one query and ignored by another is worse than none."""
    only = _account(db, tmp_path, "solo")
    acc.pause_account(only, _future(), db_path=db)

    assert acc.next_account_for_spawn(db) is None
    assert limit_state.pick_account(db_path=db) is None
    assert limit_state.list_available_accounts(db_path=db) == []
    assert limit_state.reserve_account(db_path=db) is None
    assert limit_state.all_cooling(db_path=db) is True


def test_pause_registers_as_provider_backpressure(db: str, tmp_path: Path) -> None:
    _account(db, tmp_path, "a")
    paused = _account(db, tmp_path, "b")
    assert limit_state.provider_pressure(db_path=db) == pytest.approx(0.0)

    acc.pause_account(paused, _future(), db_path=db)

    # Half the fleet is unavailable — the router must feel that before spawning.
    assert limit_state.provider_pressure(db_path=db) == pytest.approx(0.5)


# ------------------------------------------------------------------ reporting


def test_paused_flag_is_derived_not_stored(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")

    acc.pause_account(only, _future(), "maintenance window", db_path=db)
    live = acc.get_account(only, db_path=db)
    assert live["paused"] is True
    assert live["pause_reason"] == "maintenance window"

    # Same row, elapsed window: the flag must flip with no write in between.
    acc.pause_account(only, _past(), "maintenance window", db_path=db)
    elapsed = acc.get_account(only, db_path=db)
    assert elapsed["paused"] is False
    assert elapsed["pause_reason"] is None


def test_listing_never_leaks_secrets_while_paused(db: str, tmp_path: Path) -> None:
    only = _account(db, tmp_path, "solo")
    acc.pause_account(only, _future(), db_path=db)

    listed = acc.list_accounts(db_path=db)[0]

    assert "secret_ref" not in listed
    assert listed["has_secret"] is False
