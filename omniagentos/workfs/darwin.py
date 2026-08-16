"""Darwin's kernel-enforced atomic WorkFS staging install."""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any

from omniagentos.workfs.errors import WorkfsError
from omniagentos.workfs.root import STAGE_PREFIX

RENAME_EXCL = 0x04
RENAME_NOFOLLOW_ANY = 0x10
RENAME_RESOLVE_BENEATH = 0x20
ATOMIC_RENAME_FLAGS = RENAME_EXCL | RENAME_NOFOLLOW_ANY | RENAME_RESOLVE_BENEATH
_REQUIRED_KERNEL_MASK = 0x34

_renameatx_np: Any | None = None
_bind_attempted = False


def _load_renameatx_np() -> Any:
    """Bind the public Darwin libc primitive once, failing closed."""
    global _bind_attempted, _renameatx_np
    if sys.platform != "darwin":
        raise WorkfsError(
            "workfs_unavailable",
            "atomic WorkFS directory creation requires Darwin renameatx_np",
            detail={"platform": sys.platform},
        )
    if not _bind_attempted:
        _bind_attempted = True
        try:
            function = ctypes.CDLL(None, use_errno=True).renameatx_np
        except (AttributeError, OSError) as exc:
            raise WorkfsError(
                "workfs_unavailable",
                "Darwin renameatx_np is unavailable",
                detail={"error": str(exc)},
            ) from exc
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        _renameatx_np = function
    if _renameatx_np is None:
        raise WorkfsError(
            "workfs_unavailable",
            "Darwin renameatx_np binding failed closed",
        )
    return _renameatx_np


def rename_stage_atomic(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
    flags: int,
) -> None:
    """Install one fresh reserved stage with the exact protected flag mask."""
    if source_fd != destination_fd:
        raise WorkfsError(
            "atomic_install_invalid",
            "source and destination must use the same held WORK root descriptor",
        )
    if (
        RENAME_EXCL != 0x04
        or RENAME_NOFOLLOW_ANY != 0x10
        or RENAME_RESOLVE_BENEATH != 0x20
        or ATOMIC_RENAME_FLAGS != _REQUIRED_KERNEL_MASK
        or flags != _REQUIRED_KERNEL_MASK
    ):
        raise WorkfsError(
            "atomic_install_invalid",
            "atomic WorkFS install requires the exact protected rename flag mask",
            detail={"flags": flags, "required": _REQUIRED_KERNEL_MASK},
        )
    if not source.startswith(STAGE_PREFIX) or "/" in source or "\\" in source or "\x00" in source:
        raise WorkfsError(
            "atomic_install_invalid",
            "atomic install source is not a reserved direct-root stage",
            detail={"source": source},
        )
    _validate_relative_destination(destination)

    function = _load_renameatx_np()
    ctypes.set_errno(0)
    result = function(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination,
        )


def _validate_relative_destination(destination: str) -> None:
    if (
        not destination
        or os.path.isabs(destination)
        or "\\" in destination
        or "\x00" in destination
    ):
        raise WorkfsError(
            "atomic_install_invalid",
            "atomic install destination must be root-relative",
            detail={"destination": destination},
        )
    parts = destination.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkfsError(
            "atomic_install_invalid",
            "atomic install destination contains traversal",
            detail={"destination": destination},
        )
