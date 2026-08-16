"""Safe previews for files in ``var/artifacts`` and board ``var/**/outputs`` trees.

Preview paths may be absolute or relative to ``var``.  A resolved file is allowed
only when it is under ``var/artifacts`` or its path below ``var`` includes an
``outputs`` directory segment; this deliberately excludes whole project and
intake workspaces.  Resolving before that containment check also rejects
symlinks that lead outside an allowed tree.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from omniagentos.api.services import ApiError
from omniagentos.path_containment import inode_relative_parts_anchored

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".html",
    ".htm",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".log",
    ".xml",
    ".toml",
    ".ini",
    ".cfg",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_MAX_TEXT_BYTES = 256_000

# Content-types that are renderable and can execute code or cause XSS
_DANGEROUS_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "application/javascript",
    "text/javascript",
    "application/json",
    "image/svg+xml",
}


def _repo_var() -> Path:
    return Path(__file__).resolve().parents[3] / "var"


def _validate_raw_path(path: str) -> None:
    if not path or not path.strip() or "\x00" in path:
        raise ApiError(403, "path_forbidden", "invalid artifact path", {"path": path})
    # Treat backslashes as separators too so a path accepted on another host
    # cannot smuggle a traversal segment through this validation.
    if ".." in path.replace("\\", "/").split("/"):
        raise ApiError(403, "path_forbidden", "invalid artifact path", {"path": path})


def _is_allowed_target(candidate: Path) -> bool:
    var = _repo_var().resolve()
    relative = inode_relative_parts_anchored(candidate, var)
    if relative is None:
        return False
    return relative[:1] == ("artifacts",) or "outputs" in relative[:-1]


def _resolve_safe(path: str) -> Path:
    _validate_raw_path(path)
    raw = Path(path).expanduser()
    # Relative paths are rooted at var/.  resolve() collapses any symlink before
    # the containment check below, preventing a symlink escape.
    candidate = (raw if raw.is_absolute() else _repo_var() / raw).resolve()
    if not _is_allowed_target(candidate):
        raise ApiError(403, "path_forbidden", "path outside artifact roots", {"path": path})
    return candidate


# Suffixes whose IANA type this interpreter's mimetypes database may not know.
# Python's table is platform- and version-dependent: `mimetypes.guess_type("x.md")`
# returns None on this host, which silently degraded markdown previews to
# application/octet-stream. Pin the ones the preview contract depends on so the
# served type is a property of the code, not of whichever mimetypes table the
# host happens to ship.
_SUFFIX_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


def _content_type(target: Path) -> str:
    pinned = _SUFFIX_CONTENT_TYPES.get(target.suffix.lower())
    if pinned:
        return pinned
    return mimetypes.guess_type(str(target))[0] or "application/octet-stream"


def _should_inert_content_type(content_type: str) -> bool:
    """Check if content-type is dangerous and should be served as non-renderable."""
    return content_type in _DANGEROUS_CONTENT_TYPES or content_type.startswith("text/")


@router.get("/preview/raw")
def preview_artifact_raw(path: str = Query(..., min_length=1)) -> FileResponse:
    """Stream a previewable artifact after applying the same path checks.

    Renderable text content-types are forced to text/plain to prevent browser
    rendering and XSS attacks (e.g., HTML, JavaScript, JSON stored as artifacts).
    """
    target = _resolve_safe(path)
    if not target.is_file():
        raise ApiError(404, "not_found", "artifact not found", {"path": str(target)})

    guessed_content_type = _content_type(target)

    # Inert renderable text types to prevent XSS
    if _should_inert_content_type(guessed_content_type):
        media_type = "text/plain; charset=utf-8"
    else:
        media_type = guessed_content_type

    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/preview")
def preview_artifact(path: str = Query(..., min_length=1)) -> dict[str, Any]:
    """Return a bounded text preview or browser-loadable image metadata."""
    target = _resolve_safe(path)
    if not target.is_file():
        raise ApiError(404, "not_found", "artifact not found", {"path": str(target)})

    suffix = target.suffix.lower()
    content_type = _content_type(target)
    size_bytes = target.stat().st_size
    if suffix in _TEXT_SUFFIXES or content_type.startswith("text/"):
        data = target.read_bytes()[:_MAX_TEXT_BYTES]
        return {
            "kind": "text",
            "path": str(target),
            "content_type": content_type,
            "text": data.decode("utf-8", errors="replace"),
            "truncated": size_bytes > _MAX_TEXT_BYTES,
            "size_bytes": size_bytes,
        }
    if suffix in _IMAGE_SUFFIXES:
        return {
            "kind": "image",
            "path": str(target),
            "content_type": content_type,
            "size_bytes": size_bytes,
            "url": f"/api/artifacts/preview/raw?{urlencode({'path': str(target)})}",
        }
    return {
        "kind": "binary",
        "path": str(target),
        "content_type": content_type,
        "size_bytes": size_bytes,
    }
