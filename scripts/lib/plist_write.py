"""Fail-closed, atomic writer for rendered launchd plists."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


class PlistWriteError(RuntimeError):
    """Raised when rendered plist content cannot safely replace a target."""


def write_plist_atomic(target: str | Path, content: str) -> None:
    """Lint ``content`` in a sibling temporary file, then atomically replace ``target``.

    The existing target is deliberately untouched until the rendered document is
    validated as a real property list.  Keeping the temporary file in
    ``target.parent`` makes ``os.replace`` atomic on the target filesystem.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # plutil is macOS-only. When it exists we lint with it (authoritative);
        # on Linux (e.g. CI) shutil.which returns None, so we validate the rendered
        # document with stdlib plistlib instead of exec-ing a nonexistent
        # /usr/bin/plutil -- the old `or "/usr/bin/plutil"` fallback raised
        # FileNotFoundError and reddened Linux CI. Either path keeps the writer
        # fail-closed: invalid content is refused before the atomic replace.
        plutil = shutil.which("plutil")
        if plutil is not None:
            lint = subprocess.run(
                [plutil, "-lint", str(temporary)], capture_output=True, text=True, check=False
            )
            if lint.returncode:
                detail = (lint.stderr or lint.stdout).strip() or "plutil rejected the content"
                raise PlistWriteError(f"refusing to replace {target}: invalid plist: {detail}")
        else:
            try:
                with open(temporary, "rb") as rendered:
                    plistlib.load(rendered)
            except Exception as exc:  # InvalidFileException, ExpatError, ValueError, ...
                raise PlistWriteError(
                    f"refusing to replace {target}: invalid plist: {exc}"
                ) from exc
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
