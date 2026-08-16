#!/usr/bin/env python3
"""Idempotently merge fleetcap hooks into a Claude settings.json file."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def merge_hooks(settings: dict[str, Any], hooks_dir: Path) -> bool:
    changed = False
    raw_hooks = settings.get("hooks")
    if not isinstance(raw_hooks, dict):
        settings["hooks"] = {}
        changed = raw_hooks is not None
    hooks: dict[str, Any] = settings["hooks"]
    for event, script in (("SessionStart", "session-start.sh"), ("SessionEnd", "session-end.sh")):
        command = str((hooks_dir / script).resolve())
        raw_groups = hooks.get(event)
        groups = raw_groups if isinstance(raw_groups, list) else []
        if groups is not raw_groups:
            hooks[event] = groups
            changed = True
        present = False
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            if isinstance(entries, list) and any(
                isinstance(item, dict) and item.get("command") == command for item in entries
            ):
                present = True
                break
        if not present:
            groups.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
            changed = True
    return changed


def patch(path: Path, hooks_dir: Path) -> bool:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"settings root must be an object: {path}")
    if not merge_hooks(loaded, hooks_dir):
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(loaded, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path)
    parser.add_argument("--hooks-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    print("updated" if patch(args.settings.expanduser(), args.hooks_dir) else "already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
