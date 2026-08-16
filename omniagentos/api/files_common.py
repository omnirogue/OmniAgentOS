"""Shared multipart-upload mechanics for project and board-card file uploads.

Extracted from ``projects.upload_project_files`` (P1) so the board-files router
(P2, :mod:`omniagentos.api.routes.board_files`) reuses the EXACT SAME size caps,
write path and instructions log instead of a second hand-rolled copy that could
silently drift -- a 100MB/file, 500MB/request gap here is a real DoS surface on
an unsandboxed localhost API.

Filename *policy* is deliberately NOT baked in here: ``projects.py`` rejects any
path separator in an upload filename outright (unchanged, existing behavior),
while board uploads strip to a basename and add a dotfile-only deny (P2
security spec). Each caller supplies its own ``validate_name`` callable to
``collect_safe_uploads`` so that difference stays a policy choice at the call
site, not a fork of the size-cap/write-path plumbing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from omniagentos.api.routes.control import fail
from omniagentos.path_containment import inode_relative_parts

# Mirrors the caps ``projects.py`` has enforced since the upload endpoint
# shipped (P1). Kept here as the single source of truth; both routers import
# these rather than redeclaring them.
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = 500 * 1024 * 1024

# The append-only upload instructions log basename, shared with the board-files
# router so it can reserve this exact name at upload time (review fix F-017) --
# a single source of truth rather than a second hardcoded literal.
INSTRUCTIONS_LOG_NAME = "_instructions.md"


def is_within(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` is below ``root`` by shared inode proof."""
    return inode_relative_parts(candidate, root) is not None


@dataclass(frozen=True)
class SafeUpload:
    """One multipart upload after filename + size validation."""

    upload: UploadFile
    name: str
    size: int


def collect_safe_uploads(
    files: list[UploadFile],
    *,
    validate_name: Callable[[str | None], str],
    max_file_bytes: int | None = None,
    max_request_bytes: int | None = None,
) -> list[SafeUpload]:
    """Validate every filename (via ``validate_name``) and enforce byte caps.

    Calls ``fail()`` (a 4xx ``ApiError``) on the first violation -- a hostile or
    unreadable filename (whatever ``validate_name`` rejects), a duplicate name
    within one request, an unreadable stream, the per-file cap, or the
    cumulative-request cap -- mirroring the inline logic this replaces byte for
    byte. Never writes anything; callers pass the result to
    :func:`save_uploads_to_dir`.

    ``max_file_bytes``/``max_request_bytes`` default to ``None``, resolved to
    the CURRENT module-level ``MAX_UPLOAD_FILE_BYTES``/``MAX_UPLOAD_REQUEST_BYTES``
    at call time (not bound at import time) -- so a test can
    ``monkeypatch.setattr(files_common, "MAX_UPLOAD_FILE_BYTES", ...)`` the way
    the rest of this codebase patches its size ceilings (see
    ``mounts.py``/``test_mounts_routes.py``), and both callers see it.
    """
    if max_file_bytes is None:
        max_file_bytes = MAX_UPLOAD_FILE_BYTES
    if max_request_bytes is None:
        max_request_bytes = MAX_UPLOAD_REQUEST_BYTES
    safe: list[SafeUpload] = []
    total_bytes = 0
    seen_names: set[str] = set()
    for upload in files:
        try:
            filename = validate_name(upload.filename)
        except ValueError as exc:
            fail(422, "validation", str(exc))
        if filename in seen_names:
            fail(422, "validation", "uploaded filenames must be unique", {"name": filename})
        seen_names.add(filename)
        try:
            upload.file.seek(0, 2)
            size = upload.file.tell()
            upload.file.seek(0)
        except (OSError, ValueError):
            fail(400, "validation", "could not read uploaded file", {"name": filename})
        if size > max_file_bytes:
            fail(
                413,
                "file_too_large",
                f"each uploaded file is limited to {max_file_bytes // (1024 * 1024)} MB",
                {"name": filename},
            )
        total_bytes += size
        if total_bytes > max_request_bytes:
            fail(
                413,
                "request_too_large",
                f"total upload size is limited to {max_request_bytes // (1024 * 1024)} MB",
            )
        safe.append(SafeUpload(upload=upload, name=filename, size=size))
    return safe


def save_uploads_to_dir(safe_files: list[SafeUpload], uploads_dir: Path) -> list[dict[str, Any]]:
    """Write validated uploads under an already-contained ``uploads_dir``.

    Re-resolves each write target and re-checks containment immediately before
    opening it -- defense in depth: the assembled path is never trusted as a
    capability on its own, mirroring the pre-write check the inline
    ``projects.py`` loop always did. Returns ``{"name", "path", "size"}`` rows,
    ``path`` relative to the uploads directory's PARENT (the workspace), i.e.
    ``uploads/<name>``.
    """
    saved: list[dict[str, Any]] = []
    for item in safe_files:
        target = (uploads_dir / item.name).resolve(strict=False)
        if not is_within(target, uploads_dir):
            fail(422, "validation", "upload path must stay within the upload directory")
        try:
            with target.open("wb") as destination:
                while chunk := item.upload.file.read(1024 * 1024):
                    destination.write(chunk)
        except OSError:
            fail(400, "validation", "could not save uploaded file", {"name": item.name})
        finally:
            item.upload.file.close()
        saved.append({"name": item.name, "path": f"uploads/{item.name}", "size": item.size})
    return saved


def append_upload_instructions(uploads_dir: Path, names: list[str], instructions: str) -> None:
    """Append one timestamped log line per uploaded file to ``uploads/_instructions.md``."""
    if not names:
        return
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    note = instructions.replace("\r", " ").replace("\n", " ")
    try:
        with (uploads_dir / INSTRUCTIONS_LOG_NAME).open("a", encoding="utf-8") as log:
            for name in names:
                log.write(f"{timestamp} | {name} | {note}\n")
    except OSError:
        fail(400, "validation", "could not save upload instructions")


__all__ = [
    "INSTRUCTIONS_LOG_NAME",
    "MAX_UPLOAD_FILE_BYTES",
    "MAX_UPLOAD_REQUEST_BYTES",
    "SafeUpload",
    "append_upload_instructions",
    "collect_safe_uploads",
    "inode_relative_parts",
    "is_within",
    "save_uploads_to_dir",
]
