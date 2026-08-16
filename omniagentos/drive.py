"""Drive Access for projects (W4): known Drive mount roots + safe path resolution.

OmniAgentOS agents run scoped to a project's granted directories (see
``omniagentos.projects.store.ProjectStore`` -- ``root_dirs`` / ``allowed_dirs``
-- and ``omniagentos.provision.store.ProvisionStore.grant_dir``). This module is
the registry of where cloud-drive storage actually lives on this machine, so a
project can be granted a specific iCloud Drive or Google Drive SUBFOLDER (never
a whole drive root) and have its agents read + write there like any other
granted directory.

Three real, local, syncing mounts on macOS:

* iCloud Drive: ``~/Library/Mobile Documents/com~apple~CloudDocs`` -- one root.
* Google Drive for desktop: ``~/Library/CloudStorage/GoogleDrive-*/My Drive`` --
  one root PER signed-in Google account (there can be more than one), detected
  generically by globbing rather than hardcoding an email. A legacy
  ``~/Google Drive`` alias is also recognized if present on this machine.
* Dropbox: ``~/Library/CloudStorage/Dropbox*`` -- the macOS CloudStorage mount
  (``Dropbox`` for a single account, ``Dropbox-*`` when Personal + Business are
  both linked), detected by globbing the same way. A legacy ``~/Dropbox`` alias
  (older installs / a symlink) is also recognized if present.

Every path this module resolves is realpath'd (symlinks followed, ``..``
segments collapsed) before being compared against a known root, so a symlink
trick or a relative escape can never make an out-of-drive path look granted.

LIMITATION -- native Google Docs (.gdoc/.gsheet/.gform/.gslides) are cloud
STUBS, not content: Google Drive for desktop keeps a native Google Doc/Sheet/
Form/Slides deck as a tiny (commonly ~188 byte) JSON pointer file on disk with
those extensions -- the bytes on disk are a cloud reference, not the document.
Agents granted a Drive folder can fully read/write REAL files there (markdown,
text, csv, images, office documents saved in their native binary format, etc.)
but cannot read or edit a native Google Doc's content through the filesystem.
True Google Docs read/write would need a future Drive API connector (OAuth +
Docs/Sheets API); this module only ever grants filesystem-level folder access.
"""

from __future__ import annotations

import glob
import os

from omniagentos.path_containment import inode_relative_parts_anchored

# The default agent output workspace inside the (first detected) Google Drive
# mount: "<Google Drive root>/My Drive/OmniAgent".
GOOGLE_DRIVE_WORKSPACE_SUBDIR = "OmniAgent"

_ICLOUD_ROOT = "~/Library/Mobile Documents/com~apple~CloudDocs"
_GOOGLE_DRIVE_GLOB = "~/Library/CloudStorage/GoogleDrive-*/My Drive"
_LEGACY_GOOGLE_DRIVE = "~/Google Drive"
_DROPBOX_GLOB = "~/Library/CloudStorage/Dropbox*"
_LEGACY_DROPBOX = "~/Dropbox"


def drive_roots() -> list[str]:
    """Return the realpath of every known Drive mount root that exists on disk.

    Includes iCloud Drive, every signed-in Google Drive account's ``My Drive``
    (globbed, not hardcoded -- there may be more than one
    ``~/Library/CloudStorage/GoogleDrive-*`` mount), the legacy
    ``~/Google Drive`` alias, every Dropbox CloudStorage mount
    (``~/Library/CloudStorage/Dropbox*``, globbed the same way), and the legacy
    ``~/Dropbox`` alias -- each when present. A root that doesn't exist locally
    (iCloud not enabled, Google Drive/Dropbox for desktop not installed/signed
    in) is silently skipped rather than raised, so callers see exactly what's
    usable right now. Order is stable: iCloud, then Google Drive mounts
    (sorted), the legacy Google alias, then Dropbox mounts (sorted), the legacy
    Dropbox alias; duplicates (e.g. a legacy alias resolving to the same
    realpath as a CloudStorage mount) are collapsed.
    """
    candidates: list[str] = [os.path.expanduser(_ICLOUD_ROOT)]
    candidates.extend(sorted(glob.glob(os.path.expanduser(_GOOGLE_DRIVE_GLOB))))
    candidates.append(os.path.expanduser(_LEGACY_GOOGLE_DRIVE))
    candidates.extend(sorted(glob.glob(os.path.expanduser(_DROPBOX_GLOB))))
    candidates.append(os.path.expanduser(_LEGACY_DROPBOX))

    roots: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not os.path.isdir(candidate):
            continue
        resolved = os.path.realpath(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def resolve_drive_path(path: str) -> str | None:
    """Resolve ``path`` to its realpath IFF it lands under a known Drive root.

    Returns ``None`` when the (expanded, realpath'd) path is not under any
    root from :func:`drive_roots` -- including a path that tries to escape a
    root via ``..`` (realpath collapses those before the comparison) or that
    points somewhere outside every recognized mount entirely. This is the
    single validation gate every Drive-grant path MUST pass through before it
    is ever recorded as a project's accessible directory.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    for root in drive_roots():
        if inode_relative_parts_anchored(resolved, root) is not None:
            return resolved
    return None


def is_within_drive(path: str) -> bool:
    """True if ``path`` resolves under a known Drive root (see :func:`resolve_drive_path`)."""
    return resolve_drive_path(path) is not None


def google_drive_workspace() -> str | None:
    """Return (creating if needed) ``<Google Drive>/My Drive/OmniAgent``.

    This is the default location for agent-produced files/folders meant to
    live in Google Drive. Picks the first Google Drive mount found (sorted
    glob order -- stable when only one account is signed in, which is the
    common case) and creates the ``OmniAgent`` subfolder under it if it
    doesn't exist yet. Returns ``None`` when no Google Drive mount is present
    at all (e.g. Google Drive for desktop not installed/signed in, or a
    sandboxed CI machine) -- callers must treat that as "no default Google
    Drive workspace available," not an error.
    """
    for candidate in sorted(glob.glob(os.path.expanduser(_GOOGLE_DRIVE_GLOB))):
        if not os.path.isdir(candidate):
            continue
        workspace = os.path.join(os.path.realpath(candidate), GOOGLE_DRIVE_WORKSPACE_SUBDIR)
        os.makedirs(workspace, exist_ok=True)
        return workspace
    return None


__all__ = [
    "GOOGLE_DRIVE_WORKSPACE_SUBDIR",
    "drive_roots",
    "google_drive_workspace",
    "is_within_drive",
    "resolve_drive_path",
]
