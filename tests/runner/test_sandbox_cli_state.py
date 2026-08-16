"""Regressions for CLI state writes in macOS Seatbelt profiles."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from omniagentos.intake.fable import run_fable_json
from omniagentos.runner.sandbox import (
    adapter_write_roots,
    build_profile,
    config_write_deny_targets,
)


def _resolved_home_path(relative: str) -> str:
    return os.path.realpath(os.path.join(os.path.expanduser("~"), relative))


def _write_allow_block(profile: str) -> str:
    start = profile.index("(allow file-write*")
    end = profile.index('  (regex #"^/dev/ttys[0-9]+$"))', start)
    return profile[start:end]


def _literal_write_deny(path: str) -> str:
    return f'(deny file-write* (literal "{path}"))'


def _subpath_write_deny(path: str) -> str:
    return f'(deny file-write* (subpath "{path}"))'


@pytest.mark.fast
def test_profile_contains_claude_json_allow() -> None:
    claude_dir = _resolved_home_path(".claude")
    claude_state = _resolved_home_path(".claude.json")
    settings = _resolved_home_path(".claude/settings.json")
    hooks = _resolved_home_path(".claude/hooks")

    roots = adapter_write_roots(provider="claude")
    with tempfile.TemporaryDirectory() as workspace:
        profile = build_profile(workspace, roots)

    allow_block = _write_allow_block(profile)
    assert claude_dir in roots
    assert claude_state in roots
    assert f'(subpath "{claude_dir}")' in allow_block
    assert f'(subpath "{claude_state}")' in allow_block

    literal_denies, subpath_denies = config_write_deny_targets()
    assert claude_state not in literal_denies
    assert _literal_write_deny(claude_state) not in profile
    assert settings in literal_denies
    assert _literal_write_deny(settings) in profile
    assert hooks in subpath_denies
    assert _subpath_write_deny(hooks) in profile


@pytest.mark.fast
@pytest.mark.parametrize(
    ("provider", "state_dirs", "denied_files"),
    [
        (
            "codex",
            (".codex", ".config/codex"),
            (".codex/config.toml", ".config/codex/config.toml"),
        ),
        (
            "kimi",
            (".kimi-code", ".kimi", ".moonshot", ".config/kimi"),
            (".kimi/config.toml", ".config/kimi/config.toml"),
        ),
        (
            "gemini",
            (".gemini", ".config/gemini"),
            (".gemini/settings.json", ".config/gemini/settings.json"),
        ),
        (
            "qwen",
            (".qwen",),
            (".qwen/settings.json",),
        ),
    ],
)
def test_profile_provider_state_denies(
    provider: str,
    state_dirs: tuple[str, ...],
    denied_files: tuple[str, ...],
) -> None:
    roots = adapter_write_roots(provider=provider)
    with tempfile.TemporaryDirectory() as workspace:
        profile = build_profile(workspace, roots)

    allow_block = _write_allow_block(profile)
    for relative in state_dirs:
        state_dir = _resolved_home_path(relative)
        assert state_dir in roots
        assert f'(subpath "{state_dir}")' in allow_block
    for relative in denied_files:
        assert _literal_write_deny(_resolved_home_path(relative)) in profile


@pytest.mark.live_cli
@pytest.mark.timeout(30)
def test_fable_planner_not_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "fable-live.db"
        monkeypatch.setenv("OMNIAGENTOS_DB", str(database))
        result = run_fable_json(
            'Return JSON {"ok":"yes"}',
            {
                "type": "object",
                "properties": {"ok": {"type": "string"}},
                "required": ["ok"],
            },
            effort="high",
            wall_ms=25_000,
        )

    assert result is not None
    assert result.get("ok") == "yes"
