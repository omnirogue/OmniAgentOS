"""HTTP surface for the machine-roots mounts registry (P2 mounts browse API).

Read-only filesystem browsing over the P1 registry (:mod:`omniagentos.mounts`)
for the dashboard and agents: list configured mounts, list a single directory
(shallow, never recursive), and preview a single file (mirroring the existing
project-file read semantics in :mod:`omniagentos.api.routes.projects`).

This surface runs UNSANDBOXED (unlike a confined agent session), so it is the
only thing standing between an HTTP caller and this machine's real filesystem.
It must therefore be NO MORE PERMISSIVE than the OS sandbox a confined session
runs under. Four guardrails apply to every request that touches a path:

  1. Traversal is rejected on the RAW string (NUL bytes / leading '/' / '..'
     segments) before any filesystem access happens.
  2. The candidate is then resolved and MUST remain contained under the
     mount's ``resolved`` realpath (computed once at P1 load time -- treated
     as immutable here).
  3. The RESOLVED path (``_read_guard``) is checked against, in order:
       a. the shared secret registry (:mod:`omniagentos.policy.secrets`) -- a
          secret DIR/basename reached via ANY mount or symlink is a 403;
       b. the home-dotfile deny (parity with the OS sandbox's
          ``home_dotfile_read_deny_pattern``): any resolved path whose first
          component directly under ``$HOME`` begins with '.' is a 403. Because
          the path is already realpath-resolved, this also catches a
          non-dotfile-named symlink inside the home mount that RESOLVES to a
          home dotfile (e.g. ``~/.claude.json``, ``~/.env``) -- the exact
          loose-dotfile gap the secret registry misses;
       c. the home top-level skip list (``_HOME_TOP_LEVEL_SKIP``), enforced as a
          real access control on DIRECT requests to the ``home`` mount (not just
          a listing filter) so ``home/file?path=Library/...`` is refused. Scoped
          to ``mount.id == 'home'`` so the cloud mounts that live UNDER
          ``~/Library`` (different ids) stay fully readable.

The app-level session-token gate (``omniagentos.api.main.require_session_token``)
additionally requires ``X-Session-Token`` on every GET/HEAD under this prefix,
exactly like project-file reads -- see ``_is_mounts_request`` in ``main.py``.
"""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from omniagentos.api.routes.control import fail
from omniagentos.api.routes.projects import _MAX_TEXT_PREVIEW_BYTES, _file_kind
from omniagentos.mounts import Mount, load_mounts, mount_by_id
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.policy.secrets import references_secret
from omniagentos.runner.sandbox import home_dotfile_read_deny_pattern

router = APIRouter(prefix="/api/mounts", tags=["mounts"])

# Entries hidden from every directory listing regardless of mount.
_ALWAYS_SKIP_NAMES = frozenset({"node_modules", "__pycache__", ".Trash"})

# Additional top-level entries hidden ONLY at the root of the 'home' mount --
# noisy machine internals a browse UI should never need, not secrets (those are
# separately hard-denied by the secret-registry check below regardless of this
# list). Cloud drives have their own mount ids, so they are never reached by
# walking through 'home'.
_HOME_TOP_LEVEL_SKIP = frozenset(
    {"Library", "anaconda3", "google-cloud-sdk", "Parallels", "Applications (Parallels)"}
)

_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000

# Absolute ceiling on any FileResponse (text download or image stream) served by
# this unsandboxed surface. Well above the 1 MB inline text preview, but bounded
# so a `download=true` request can never stream an arbitrarily large file (fix4).
_MAX_FILE_BYTES = 50 * 1024 * 1024


def _get_mount_or_404(mount_id: str) -> Mount:
    mount = mount_by_id(mount_id)
    if mount is None:
        fail(404, "not_found", "unknown mount", {"mount_id": mount_id})
    return mount


def _require_mount_exists(mount: Mount) -> None:
    if not mount.exists:
        fail(
            409,
            "mount_unavailable",
            f"mount {mount.id!r} is not currently present on disk",
            {"mount_id": mount.id, "path": mount.path},
        )


def _safe_relative_path(value: str) -> str:
    """Normalize-and-reject a relative path fragment BEFORE any FS access.

    Mirrors ``projects._reject_traversal``: rejects NUL bytes, an absolute
    (leading '/') value, and any ``..`` segment. An empty string is valid and
    names the mount's own root.
    """
    if "\x00" in value:
        fail(403, "forbidden", "path is invalid")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        fail(403, "forbidden", "path must be relative to the mount root")
    if ".." in normalized.split("/"):
        fail(403, "forbidden", "path must not contain '..' segments")
    return normalized


def _resolve_within_mount(mount: Mount, relative: str) -> Path:
    """Resolve a relative path against a mount's realpath'd root, enforcing containment.

    ``mount.resolved`` came from the P1 registry (realpath'd once at load,
    treated as immutable here). ``Path.resolve()`` is the security boundary: it
    collapses symlinks, so a link inside the mount that targets elsewhere is
    caught by the containment check below rather than trusted.
    """
    root = Path(mount.resolved)
    try:
        candidate = (
            (root / relative).resolve(strict=False) if relative else root.resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        fail(403, "forbidden", "path could not be resolved")
    if inode_relative_parts_anchored(candidate, root) is None:
        fail(403, "forbidden", "path is outside the mount")
    return candidate


def _deny_secret(target: Path) -> None:
    if references_secret(str(target), None):
        fail(403, "forbidden", "path is inside a secret-registry directory")


def _deny_home_dotfile(target: Path) -> None:
    """Deny any resolved path under a ``$HOME`` dotfile, mirroring the OS sandbox.

    Reuses :func:`omniagentos.runner.sandbox.home_dotfile_read_deny_pattern`
    verbatim (``^$HOME/\\.[^/]*(/.*)?$``) as the SINGLE source of truth, so this
    unsandboxed read can never drift ABOVE what a confined session is allowed --
    it closes the loose-home-dotfile gap (``~/.claude.json`` holds a live token,
    ``~/.env``, ``~/.gemini/.env``) that the secret registry does not enumerate.
    ``target`` is already realpath-resolved by :func:`_resolve_within_mount`, so a
    non-dotfile-named symlink resolving to a home dotfile is caught here too.
    """
    if re.match(home_dotfile_read_deny_pattern(), str(target)):
        fail(403, "forbidden", "path is under a home dotfile (denied for OS-sandbox parity)")


def _deny_home_top_level_skip(mount: Mount, target: Path) -> None:
    """Enforce ``_HOME_TOP_LEVEL_SKIP`` as access control on DIRECT home requests.

    Scoped to the ``home`` mount only: its first-component-under-root entries in
    the skip list (``Library``/``anaconda3``/...) are refused even when named
    directly (``home/file?path=Library/...``), not merely hidden from listings.
    The cloud mounts live UNDER ``~/Library`` but carry their own mount ids, so
    they never reach this check.
    """
    rel = inode_relative_parts_anchored(target, mount.resolved)
    if rel is None:
        return
    if rel and rel[0] in _HOME_TOP_LEVEL_SKIP:
        fail(403, "forbidden", "path is under a hidden home top-level directory")


def _read_guard(mount: Mount, target: Path) -> None:
    """Full fail-closed read-guard for a resolved path (see module docstring).

    Applied AFTER :func:`_resolve_within_mount` on every path-touching request in
    both the ``/dir`` and ``/file`` handlers; any layer 403s. Grouped so the two
    handlers can never drift apart.
    """
    _deny_secret(target)
    _deny_home_dotfile(target)
    if mount.id == "home":
        _deny_home_top_level_skip(mount, target)


def _too_large_response() -> JSONResponse:
    """413 for a FileResponse (download/image) exceeding ``_MAX_FILE_BYTES``.

    Uses the flat ``{error, message}`` body the dashboard's ``MountApiError``
    normalizes for the file-preview endpoint's non-2xx responses.
    """
    return JSONResponse(
        status_code=413,
        content={
            "error": "file_too_large",
            "message": f"Files above {_MAX_FILE_BYTES // (1024 * 1024)} MB cannot be served. Open the file locally instead.",
        },
    )


@router.get("")
def list_mounts() -> dict[str, list[dict[str, Any]]]:
    """List every configured mount with its grant/display metadata."""
    mounts = [
        {
            "id": mount.id,
            "label": mount.label,
            "path": mount.path,
            # Realpath'd absolute form (P1 ``Mount.resolved``). The raw ``path``
            # may be ``~``-relative (e.g. ``~/Work``); create-project requires an
            # absolute '/'-prefixed root, so the dashboard picker must insert
            # ``resolved`` -- surfacing ``path`` alone 422s the intake (fix7).
            "resolved": mount.resolved,
            "kind": mount.kind,
            "cloud": mount.cloud,
            "grantable": mount.grantable,
            "read_only": mount.read_only,
            "exists": mount.exists,
            "notes": mount.notes,
        }
        for mount in load_mounts()
    ]
    return {"mounts": mounts}


@router.get("/{mount_id}/dir")
def browse_mount_dir(
    mount_id: str,
    path: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    """Shallow single-directory listing under a mount (never recurses).

    Uses ``os.scandir`` for metadata only -- it never opens a file's contents,
    so a cloud-dataless file (iCloud/Google Drive/Dropbox placeholder) is never
    forced to download by a listing.
    """
    mount = _get_mount_or_404(mount_id)
    _require_mount_exists(mount)
    relative = _safe_relative_path(path)
    target = _resolve_within_mount(mount, relative)
    _read_guard(mount, target)
    if not target.is_dir():
        fail(404, "not_found", "directory not found", {"path": path})

    skip_names = _ALWAYS_SKIP_NAMES
    if mount.id == "home" and relative in ("", "."):
        skip_names = skip_names | _HOME_TOP_LEVEL_SKIP

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(target) as scanner:
            for entry in scanner:
                if entry.name.startswith("."):
                    continue
                if entry.name in skip_names:
                    continue
                try:
                    stat = entry.stat()
                    is_dir = entry.is_dir()
                except OSError:
                    # A broken symlink or a race with a concurrent delete --
                    # skip rather than fail the whole listing.
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": is_dir,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
    except OSError as exc:
        fail(404, "not_found", "could not read directory", {"path": path, "error": str(exc)})

    entries.sort(key=lambda item: (not item["is_dir"], str(item["name"]).lower()))
    total = len(entries)
    page = entries[offset : offset + limit]
    truncated = offset + len(page) < total
    return {
        "mount_id": mount.id,
        "path": path,
        "entries": page,
        "total": total,
        "truncated": truncated,
    }


@router.get("/{mount_id}/file")
def read_mount_file(mount_id: str, path: str = Query(...), download: bool = False) -> Any:
    """Read a safely-contained text file, or stream an image for browser preview.

    Mirrors ``projects.get_project_file``'s response shape exactly (text <=1MB
    UTF-8 preview / images streamed / other binary 415) so the dashboard's file
    viewer can share rendering logic across both surfaces.
    """
    mount = _get_mount_or_404(mount_id)
    _require_mount_exists(mount)
    relative = _safe_relative_path(path)
    if not relative or relative == ".":
        fail(403, "forbidden", "a file path is required")
    target = _resolve_within_mount(mount, relative)
    _read_guard(mount, target)
    if not target.is_file():
        fail(404, "not_found", "file not found", {"path": path})

    kind = _file_kind(target)
    try:
        size = target.stat().st_size
    except OSError:
        fail(404, "not_found", "file not found", {"path": path})

    if kind == "image":
        if size > _MAX_FILE_BYTES:
            return _too_large_response()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            target, media_type=media_type, filename=target.name if download else None
        )
    if kind != "text":
        return JSONResponse(
            status_code=415,
            content={
                "error": "binary_file",
                "message": "Preview is only available for text and image files.",
            },
        )
    if download:
        # The download branch streams the file whole, so it must honour the size
        # ceiling BEFORE returning a FileResponse (fix4): otherwise it bypassed
        # the inline preview cap entirely and could stream an unbounded file.
        if size > _MAX_FILE_BYTES:
            return _too_large_response()
        return FileResponse(target, media_type="text/plain", filename=target.name)
    if size > _MAX_TEXT_PREVIEW_BYTES:
        return JSONResponse(
            status_code=413,
            content={
                "error": "file_too_large",
                "message": "Text previews are limited to 1 MB. Open or download the file instead.",
            },
        )
    try:
        return PlainTextResponse(target.read_text(encoding="utf-8"), media_type="text/plain")
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=415,
            content={"error": "binary_file", "message": "This file is not valid UTF-8 text."},
        )
    except OSError:
        fail(404, "not_found", "file not found", {"path": path})


__all__ = ["router"]
