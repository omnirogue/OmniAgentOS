"""Enumerate transcript roots for every supported command-line agent profile.

Historical fleet rows used ``acct1`` for ``~/.claude`` and omitted the real
``account-1`` profile.  Fleetcap deliberately does not rewrite those rows; new
captures use stable labels: ``default``, ``account-1``, ``account-2``, etc.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class Profile(NamedTuple):
    cli: str
    account_label: str
    root: Path
    globs: tuple[str, ...]


def _claude_dirs(home: Path) -> list[Path]:
    dirs = [home / ".claude"]
    dirs.extend(
        sorted(
            path
            for path in home.glob(".claude-account-*")
            if path.is_dir() and path.name != ".claude-account-"
        )
    )
    named = home / ".claude-accounts"
    if named.is_dir():
        dirs.extend(sorted(path for path in named.iterdir() if path.is_dir()))
    return dirs


def _claude_label(path: Path) -> str:
    if path.name == ".claude":
        return "default"
    if path.parent.name == ".claude-accounts":
        return path.name
    return path.name.removeprefix(".claude-")


def enumerate_profiles(home: Path | None = None) -> list[Profile]:
    """Return known profiles, including absent standard roots for enrollment."""
    base = (home or Path.home()).expanduser().resolve()
    result = [
        Profile("claude", _claude_label(path), path, ("projects/**/*.jsonl",))
        for path in _claude_dirs(base)
    ]
    codex_roots = [base / ".codex"]
    codex_roots.extend(sorted(path for path in base.glob(".codex*") if path != base / ".codex"))
    result.extend(
        Profile(
            "codex",
            "default" if path.name == ".codex" else path.name[1:],
            path,
            ("sessions/**/*.jsonl",),
        )
        for path in codex_roots
        if path == base / ".codex" or path.is_dir()
    )
    for name, cli, globs in (
        (".kimi-code", "kimi", ("sessions/**/wire.jsonl",)),
        (".kimi2", "kimi", ("sessions/**/wire.jsonl",)),
        (".grok", "grok", ("**/*.jsonl", "**/*.lock")),
        (".gemini", "gemini", ("**/*.json", "**/*.jsonl", ".project_root")),
    ):
        root = base / name
        if root.is_dir() or cli in {"kimi", "grok", "gemini"}:
            label = "default" if name not in {".kimi2"} else "kimi2"
            result.append(Profile(cli, label, root, globs))
    return result


def existing_profiles(home: Path | None = None) -> list[Profile]:
    return [profile for profile in enumerate_profiles(home) if profile.root.is_dir()]
