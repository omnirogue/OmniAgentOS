"""Predictive-balance preference in spawn rotation.

The pick may REORDER preference away from accounts whose cached weekly window
is >= 90% used, but must never shrink the pool: with every account predictively
out (or usage unknown) the behavior degrades to exactly the pre-feature LRU.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from omniagentos.accounts import service as acc
from omniagentos.db.migrate import migrate


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "accounts.db")
    migrate(db)
    return db


def _config_dir(base: Path, name: str, *, weekly_used: float | None) -> str:
    directory = base / name
    directory.mkdir()
    payload: dict = {"oauthAccount": {"emailAddress": f"{name}@x.com"}}
    if weekly_used is not None:
        payload["cachedUsageUtilization"] = {
            "fetchedAtMs": time.time() * 1000.0,
            "utilization": {
                "limits": [
                    {
                        "kind": "weekly_all",
                        "percent": weekly_used,
                        "resets_at": "2099-01-01T00:00:00+00:00",
                    }
                ]
            },
        }
    (directory / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
    return str(directory)


def test_predictively_out_account_is_skipped_while_alternative_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    dir_out = _config_dir(tmp_path, "cfg_out", weekly_used=97.0)
    dir_ok = _config_dir(tmp_path, "cfg_ok", weekly_used=20.0)
    acc.add_account(label="out", config_dir=dir_out, enabled=True, db_path=db)
    acc.add_account(label="ok", config_dir=dir_ok, enabled=True, db_path=db)

    picks = {acc.next_account_for_spawn(db).config_dir for _ in range(4)}  # type: ignore[union-attr]
    assert picks == {dir_ok}


def test_all_predictively_out_degrades_to_lru_never_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    dir_a = _config_dir(tmp_path, "cfgA", weekly_used=95.0)
    dir_b = _config_dir(tmp_path, "cfgB", weekly_used=99.0)
    acc.add_account(label="A", config_dir=dir_a, enabled=True, db_path=db)
    acc.add_account(label="B", config_dir=dir_b, enabled=True, db_path=db)

    picks = [acc.next_account_for_spawn(db).config_dir for _ in range(4)]  # type: ignore[union-attr]
    # Both out: pool must not shrink — both still rotate, LRU order.
    assert set(picks) == {dir_a, dir_b}
    assert all(picks[i] != picks[i + 1] for i in range(len(picks) - 1))


def test_unknown_usage_is_treated_as_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acc, "detect_config_dirs", lambda: [])
    db = _db(tmp_path)
    dir_unknown = _config_dir(tmp_path, "cfg_unknown", weekly_used=None)
    acc.add_account(label="u", config_dir=dir_unknown, enabled=True, db_path=db)

    picked = acc.next_account_for_spawn(db)
    assert picked is not None and picked.config_dir == dir_unknown


def test_expired_reset_neutralizes_a_stale_out_snapshot(tmp_path: Path) -> None:
    directory = tmp_path / "cfg_reset"
    directory.mkdir()
    (directory / ".claude.json").write_text(
        json.dumps(
            {
                "cachedUsageUtilization": {
                    "fetchedAtMs": time.time() * 1000.0,
                    "utilization": {
                        "limits": [
                            {
                                "kind": "weekly_all",
                                "percent": 99.0,
                                # The window already reset: the 99% is moot.
                                "resets_at": "2020-01-01T00:00:00+00:00",
                            }
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    assert acc._predictively_out(str(directory)) is False


def test_predictively_out_reads_true_for_a_binding_out_window(tmp_path: Path) -> None:
    directory = Path(_config_dir(tmp_path, "cfg_hot", weekly_used=94.0))
    assert acc._predictively_out(str(directory)) is True
