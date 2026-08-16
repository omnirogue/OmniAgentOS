"""Hermetic behavior tests for ``scripts/gate-workspace.sh``.

The script is exercised through ``sh`` with a fake ``git`` on ``PATH``.  The
fake records no repository state and returns only the mocked answers needed by
the script, so these regressions never create or inspect a real git checkout.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gate-workspace.sh"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
TAG_SHA = "3" * 40

FAKE_GIT = r"""#!/bin/sh
set -eu

if [ "$1" = "-C" ]; then
  shift 2
fi

case "$1" in
  worktree|checkout|status)
    exit 0
    ;;
  merge-base)
    exit "${FAKE_GIT_MERGE_BASE_RC:-0}"
    ;;
  rev-parse)
    shift
    if [ "$1" = "--verify" ]; then
      shift
    fi
    case "$1" in
      main^{commit})
        if [ -e "$FAKE_GIT_MAIN_SEEN" ]; then
          printf '%s\n' "$FAKE_GIT_MAIN_AFTER"
        else
          : > "$FAKE_GIT_MAIN_SEEN"
          printf '%s\n' "$FAKE_GIT_MAIN_BEFORE"
        fi
        ;;
      v1.2.3^{commit}) printf '%s\n' "$FAKE_GIT_TAG_SHA" ;;
      HEAD) printf '%s\n' "$FAKE_GIT_HEAD" ;;
      *) exit 1 ;;
    esac
    ;;
  *)
    printf 'unexpected fake git invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
"""


def _run_script(
    tmp_path: Path, *args: str, main_after: str = NEW_SHA
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(FAKE_GIT)
    fake_git.chmod(0o755)
    workspace = tmp_path / "gate"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "OMNIAGENTOS_GATE_WORKSPACE": str(workspace),
        "FAKE_GIT_MAIN_SEEN": str(tmp_path / "main-seen"),
        "FAKE_GIT_MAIN_BEFORE": OLD_SHA,
        "FAKE_GIT_MAIN_AFTER": main_after,
        "FAKE_GIT_HEAD": OLD_SHA if not args else TAG_SHA,
        "FAKE_GIT_TAG_SHA": TAG_SHA,
    }
    return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def test_gate_workspace_refuses_when_main_advances_during_pin(tmp_path: Path) -> None:
    result = _run_script(tmp_path)

    assert result.returncode != 0
    assert "gate workspace is stale" in result.stderr
    assert str(tmp_path / "gate") in result.stderr
    assert f"pinned SHA: {OLD_SHA[:8]} ({OLD_SHA})" in result.stderr
    assert f"current main SHA: {NEW_SHA[:8]} ({NEW_SHA})" in result.stderr
    assert "Rerun `scripts/gate-workspace.sh main` to advance the pin" in result.stderr


def test_gate_workspace_succeeds_when_pin_is_current(tmp_path: Path) -> None:
    result = _run_script(tmp_path, main_after=OLD_SHA)

    assert result.returncode == 0, result.stderr
    assert "pinned at" in result.stderr
    assert "stale" not in result.stderr


def test_gate_workspace_explicit_ref_is_exempt_from_main_staleness_check(tmp_path: Path) -> None:
    result = _run_script(tmp_path, "v1.2.3")

    assert result.returncode == 0, result.stderr
    assert f"pinned at {TAG_SHA} (v1.2.3)" in result.stderr
    assert "stale" not in result.stderr
