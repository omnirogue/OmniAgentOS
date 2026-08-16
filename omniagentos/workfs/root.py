"""WORK root resolution for the workfs module.

Tests ALWAYS set ``OMNIAGENTOS_WORK_ROOT`` to a temporary directory.  The
default ``~/Work`` is for production only and is never touched by the suite.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["STAGE_PREFIX", "WORK_ROOT_ENV", "work_root"]

WORK_ROOT_ENV = "OMNIAGENTOS_WORK_ROOT"
STAGE_PREFIX = ".omniagentos-workfs-stage-"


def work_root() -> Path:
    """Return the lexical absolute configured root without following symlinks.

    Root authority is established later with ``lstat`` and
    ``O_NOFOLLOW|O_DIRECTORY``. Resolving here would launder a configured
    symlink into an apparently valid outside directory before those checks.
    """
    raw = os.environ.get(WORK_ROOT_ENV)
    if raw is None or str(raw).strip() == "":
        text = os.path.expanduser("~/Work")
    else:
        text = os.path.expanduser(str(raw))
    return Path(os.path.abspath(text))
