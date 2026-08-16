from pathlib import Path

import pytest

from omniagentos.fleetcap import pull


def test_every_command_has_hard_credential_excludes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pull, "_binary", lambda name: f"/usr/bin/{name}")
    config = pull.load_config(Path("configs/fleetcap/devices.yaml"))
    for _device, command, _timeout in pull.commands(config, tmp_path / "ingest"):
        if Path(command[0]).name == "rsync":
            for pattern in pull.EXCLUDES:
                assert pattern in command


def test_unsafe_root_is_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pull, "_binary", lambda name: f"/usr/bin/{name}")
    config = {
        "devices": [
            {
                "device": "bad",
                "host": "bad",
                "user": "u",
                "mode": "pull",
                "roots": [{"cli": "grok", "account": "default", "path": "/root/.grok"}],
            }
        ]
    }
    with pytest.raises(ValueError, match="unsafe grok root"):
        pull.commands(config, tmp_path)


def test_all_dark_returns_nonzero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pull, "_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        pull.subprocess, "run", lambda *_args, **_kwargs: type("R", (), {"returncode": 255})()
    )
    config = {
        "devices": [
            {
                "device": "dark",
                "host": "dark",
                "user": "u",
                "mode": "pull",
                "roots": [{"cli": "claude", "account": "default", "path": "/x/.claude/projects"}],
            }
        ]
    }
    assert pull.run(config, tmp_path / "ingest") != 0


def test_reachable_device_with_all_transfers_failed_is_nonzero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pull, "_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        pull.subprocess,
        "run",
        lambda command, **_kwargs: type(
            "R", (), {"returncode": 0 if command[-1] == "true" else 11}
        )(),
    )
    config = {
        "devices": [
            {
                "device": "reachable",
                "host": "h",
                "user": "u",
                "mode": "pull",
                "roots": [
                    {"cli": "codex", "account": "default", "path": "/Users/u/.codex/sessions"}
                ],
            }
        ]
    }
    assert pull.run(config, tmp_path / "ingest") != 0


def test_partial_transfer_failure_stays_zero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pull, "_binary", lambda name: f"/usr/bin/{name}")
    outcomes = iter((0, 11, 0))
    monkeypatch.setattr(
        pull.subprocess,
        "run",
        lambda *_args, **_kwargs: type("R", (), {"returncode": next(outcomes)})(),
    )
    roots = [
        {"cli": "claude", "account": "default", "path": "/Users/u/.claude/projects"},
        {"cli": "codex", "account": "default", "path": "/Users/u/.codex/sessions"},
    ]
    config = {
        "devices": [{"device": "partial", "host": "h", "user": "u", "mode": "pull", "roots": roots}]
    }
    assert pull.run(config, tmp_path / "ingest") == 0
