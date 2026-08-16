"""collect_claude_usage: per-machine Claude balances for the drop-file."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from omniagentos.team.session_collector import collect_claude_usage


def _profile(
    home: Path,
    name: str,
    *,
    email: str = "",
    uuid: str = "",
    weekly: float | None = None,
    scoped: float | None = None,
    session: float | None = None,
    authed: bool = True,
    fetched_ms: float | None = None,
) -> Path:
    directory = home / name
    directory.mkdir()
    if authed:
        (directory / ".credentials.json").write_text('{"oauth": "x"}', encoding="utf-8")
    payload: dict[str, Any] = {}
    if email or uuid:
        payload["oauthAccount"] = {"emailAddress": email, "accountUuid": uuid}
    limits = []
    if session is not None:
        limits.append({"kind": "session", "percent": session})
    if weekly is not None:
        limits.append(
            {"kind": "weekly_all", "percent": weekly, "resets_at": "2099-01-01T00:00:00Z"}
        )
    if scoped is not None:
        limits.append({"kind": "weekly_scoped", "percent": scoped})
    if limits:
        payload["cachedUsageUtilization"] = {
            "fetchedAtMs": fetched_ms if fetched_ms is not None else time.time() * 1000.0,
            "utilization": {"limits": limits},
        }
    (directory / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def test_dedupes_shared_accounts_and_picks_best_measured(tmp_path: Path) -> None:
    _profile(tmp_path, ".claude-account-1", email="a@x.com", uuid="U1", weekly=80.0)
    _profile(tmp_path, ".claude-account-2", email="b@x.com", uuid="U2", weekly=30.0)
    # Same account as -2: must NOT count as a second fallback.
    _profile(tmp_path, ".claude-account-3", email="b@x.com", uuid="U2", weekly=30.0)

    usage = collect_claude_usage(tmp_path)
    assert usage["distinct_accounts"] == 2
    assert usage["best_remaining_percent"] == 70.0
    assert usage["best_dir"] == ".claude-account-2"
    dup = next(a for a in usage["accounts"] if a["dir"] == ".claude-account-3")
    assert dup["duplicate_of"] == ".claude-account-2"


def test_worst_weekly_binds_not_the_session_window(tmp_path: Path) -> None:
    _profile(tmp_path, ".claude-account-1", uuid="U1", weekly=20.0, scoped=60.0, session=99.0)
    usage = collect_claude_usage(tmp_path)
    account = usage["accounts"][0]
    # Session 99% must not mask a healthy weekly; scoped 60 is the binding weekly.
    assert account["worst_used_percent"] == 60.0
    assert account["remaining_percent"] == 40.0


def test_unauthed_and_unmeasured_accounts_never_shape_best(tmp_path: Path) -> None:
    _profile(tmp_path, ".claude-account-1", uuid="U1", weekly=5.0, authed=False)
    _profile(tmp_path, ".claude-account-2", uuid="U2", weekly=95.0)
    _profile(tmp_path, ".claude-account-3", uuid="U3", weekly=None)  # authed, no snapshot

    usage = collect_claude_usage(tmp_path)
    # The unauthed 95%-left account is not a fallback; best is the measured authed one.
    assert usage["best_remaining_percent"] == 5.0
    assert usage["best_dir"] == ".claude-account-2"
    assert usage["authed_accounts"] == 2
    assert usage["authed_no_snapshot"] == 1


def test_no_measured_account_reports_none_never_a_number(tmp_path: Path) -> None:
    _profile(tmp_path, ".claude-account-1", uuid="U1", weekly=None)
    usage = collect_claude_usage(tmp_path)
    assert usage["best_remaining_percent"] is None
    assert usage["best_dir"] is None


def test_report_carries_claude_usage(tmp_path: Path, monkeypatch: Any) -> None:
    import omniagentos.team.session_collector as sc

    monkeypatch.setattr(sc.Path, "home", classmethod(lambda cls: tmp_path))
    _profile(tmp_path, ".claude-account-1", uuid="U1", weekly=10.0)
    report = sc.collect("emp_test", window_min=60, active_min=15)
    assert report["claude_usage"]["best_remaining_percent"] == 90.0


def test_dedupe_is_evidence_first_not_scan_order(tmp_path: Path) -> None:
    # First-seen dir is UNAUTHED; its duplicate carries the binding snapshot.
    _profile(tmp_path, ".claude-account-1", uuid="U1", weekly=None, authed=False)
    _profile(tmp_path, ".claude-account-2", uuid="U1", weekly=88.0, authed=True)

    usage = collect_claude_usage(tmp_path)
    two = next(a for a in usage["accounts"] if a["dir"] == ".claude-account-2")
    one = next(a for a in usage["accounts"] if a["dir"] == ".claude-account-1")
    assert two["duplicate_of"] is None  # measured+authed wins primary
    assert one["duplicate_of"] == ".claude-account-2"
    assert usage["best_remaining_percent"] == 12.0


def test_session_only_snapshot_is_unmeasured_never_weekly(tmp_path: Path) -> None:
    _profile(tmp_path, ".claude-account-1", uuid="U1", session=99.0)
    usage = collect_claude_usage(tmp_path)
    account = usage["accounts"][0]
    # No weekly entry at all: unmeasured, never a fabricated weekly number.
    assert account["worst_used_percent"] is None
    assert usage["authed_no_snapshot"] == 1
