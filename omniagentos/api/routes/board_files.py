"""HTTP surface for a board card's files: upload / list / download / archive (P2).

Gives every board card a files story: operators upload input files into the
card's workspace, list what is there (uploads/ + outputs/ + whatever the linked
Session Bridge session has reported writing), download one file, or download a
bounded zip of the whole workspace -- so deliverables are reachable via links in
the dashboard instead of an agent dumping them on the Desktop.

This is a FILE-SERVING SECURITY SURFACE on the localhost API, exactly like
:mod:`omniagentos.api.routes.mounts`: it stands directly between an HTTP caller
and this machine's real filesystem once a workspace is resolved, so it borrows
that module's guardrails outright rather than re-deriving them:

  1. Traversal is rejected on the RAW string (NUL bytes / leading '/' / '..'
     segments) before any filesystem access happens (mirrors
     ``mounts._safe_relative_path``).
  2. The candidate is then resolved and MUST remain contained under the
     resolved workspace root (mirrors ``mounts._resolve_within_mount``) -- this
     is also what catches a symlink inside the workspace that targets outside
     it, since ``Path.resolve()`` collapses symlinks before the containment
     check runs.
  3. The RESOLVED path is checked against the shared secret registry
     (:mod:`omniagentos.policy.secrets`) and the home-dotfile deny
     (:func:`omniagentos.runner.sandbox.home_dotfile_read_deny_pattern`) --
     imported directly from ``mounts.py`` (not reimplemented) so the two
     surfaces can never drift apart.
  4. The resolved workspace itself is held to an APPROVED-ROOT FLOOR
     (:func:`_enforce_workspace_floor`, review fix F-015) before any of it is
     served. Normally it must sit under this repo's ``var/`` dir or under a
     currently grantable mount root
     (:func:`omniagentos.mounts.grantable_mount_roots`). An isolated simulation
     instead admits only its canonical campaign root; it does not consult the
     machine mount registry or admit the executing checkout, which may name the
     production checkout. Simulation mode without a coherent campaign context
     fails closed. An approved workspace that still sits under ``$HOME`` is
     additionally checked by inode, belt-and-braces, against the mounts
     home-top-level skip set with case-insensitive component matching.

Auth: the router itself carries ``dependencies=[Depends(_authorized)]``
(review fix F-016, hoisted from a per-route copy) -- the same X-Session-Token +
:func:`omniagentos.sessions.token.verify_token` check ``control.py``/
``sessions.py`` use for their own routes -- rather than teaching
``omniagentos.api.main.require_session_token`` a new path pattern; main.py
stays append-only (one import + one include_router line) per this package's
ownership boundary. Hoisting it to the constructor means a route added to this
file later can never forget the gate. POST /upload is additionally covered by
the app-level mutating-method gate; the router-level dependency just keeps
every route here uniformly gated regardless of HTTP method.
"""

from __future__ import annotations

import json
import mimetypes
import os
import platform
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any

from fastapi import APIRouter, Depends, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.files_common import (
    INSTRUCTIONS_LOG_NAME,
    append_upload_instructions,
    collect_safe_uploads,
    save_uploads_to_dir,
)
from omniagentos.api.routes.collab import CollabStoreDep, fail
from omniagentos.api.routes.mounts import _HOME_TOP_LEVEL_SKIP, _deny_home_dotfile, _deny_secret
from omniagentos.api.services import ApiError
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import Store
from omniagentos.db.store import SqliteStore
from omniagentos.mounts import grantable_mount_roots
from omniagentos.path_containment import inode_paths_equal, inode_relative_parts
from omniagentos.policy.secrets import references_secret


def _authorized(x_session_token: str | None = Header(None, alias="X-Session-Token")) -> None:
    from omniagentos.sessions.token import verify_token

    if not verify_token(x_session_token):
        fail(401, "unauthorized", "invalid session token")


# Hoisted to the router constructor (review fix F-016) rather than a per-route
# ``dependencies=[Depends(_authorized)]`` copy on each ``@router...`` -- a
# route added to this file later can never forget the gate.
router = APIRouter(prefix="/api/board", tags=["board-files"], dependencies=[Depends(_authorized)])

# Bare-filename length ceiling (aligned with projects.py's _MAX_PATH).
_MAX_FILENAME_LEN = 4096

# Absolute ceiling on a single downloaded file, mirroring mounts.py's
# _MAX_FILE_BYTES for this equally-unsandboxed surface.
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

# A listing response is capped so a workspace with unbounded scratch files can
# never blow up the board UI or this response's own memory footprint.
_MAX_LIST_ENTRIES = 1000

# Zip bounds (P2 spec): stop adding files once EITHER ceiling is hit rather
# than 413ing the whole request -- the response still succeeds with whatever
# fit, and reports the truncation via response headers.
_MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
_MAX_ARCHIVE_FILES = 2000

# The zip archive spools in memory up to this many bytes before spilling to a
# real temp file on disk (review fix F-018) -- a "few MB" working set never
# forces a near-``_MAX_ARCHIVE_BYTES`` archive fully into process memory.
_ARCHIVE_SPOOL_MAX_BYTES = 8 * 1024 * 1024


# --- workspace resolution -------------------------------------------------


def _working_dir_from_run(store: Store, run: dict[str, Any]) -> str | None:
    """The working_dir a 'tools'-mode run's agent step was scoped to.

    Mirrors what ``intake.service.dispatch_spec`` actually persists: a single
    ``agent`` step with ``params.working_dir`` on the run's ``plan_json``
    (``runner.core`` reads it the exact same way:
    ``params.get("working_dir", task_input.get("working_dir"))``). Falls back
    to the task's ``input_json`` for parity with that same runner fallback.
    """
    plan_raw = run.get("plan_json")
    if isinstance(plan_raw, str) and plan_raw.strip():
        try:
            plan = json.loads(plan_raw)
        except (json.JSONDecodeError, TypeError):
            plan = None
        if isinstance(plan, list):
            for step in plan:
                if not isinstance(step, dict):
                    continue
                params = step.get("params")
                if isinstance(params, dict):
                    working_dir = params.get("working_dir")
                    if isinstance(working_dir, str) and working_dir.strip():
                        return working_dir
    task_id = run.get("task_id")
    if isinstance(task_id, str) and task_id:
        task = store.get_task(task_id)
        if task is not None:
            input_raw = task.get("input_json")
            if isinstance(input_raw, str) and input_raw.strip():
                try:
                    task_input = json.loads(input_raw)
                except (json.JSONDecodeError, TypeError):
                    task_input = None
                if isinstance(task_input, dict):
                    working_dir = task_input.get("working_dir")
                    if isinstance(working_dir, str) and working_dir.strip():
                        return working_dir
    return None


def _orchestration_working_dir(store: Store, orchestration_id: str) -> str | None:
    """``SELECT working_dir FROM orchestrations WHERE id = ?``, tolerant of absence.

    The ``orchestrations`` table is created by a SIBLING package's migration
    039 and MAY NOT EXIST in this worktree -- a missing table degrades to
    "no workspace" (None), never a 500. This package does not create the
    table itself.
    """
    if not isinstance(store, SqliteStore):
        return None
    try:
        row = store._connection.execute(
            "SELECT working_dir FROM orchestrations WHERE id = ?", (orchestration_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    value = row["working_dir"]
    return str(value) if value else None


def resolve_board_workspace(
    store: Store, collab_store: CollabStore, task_id: str
) -> tuple[dict[str, Any] | None, Path | None]:
    """Resolve a board card's workspace directory, per the frozen resolution order.

    Returns ``(board_row, workspace)``. ``board_row`` is ``None`` only when the
    card itself does not exist; a known card with no resolvable workspace
    returns ``(board_row, None)`` so callers can tell "unknown card" (404
    board task not found) apart from "no workspace for this card" (404, a
    different message) -- neither is ever a 500.

    Order: (1) a ``ses_`` ``result_ref`` -> that session's ``project_dir``;
    (2) a ``run_id`` -> the run's agent-step ``working_dir``; (3) an ``orch_``
    ``result_ref`` -> ``orchestrations.working_dir`` (tolerating a missing
    table); (4) otherwise ``None``.
    """
    board = collab_store.get_board_task(task_id)
    if board is None:
        return None, None
    result_ref = board.get("result_ref")

    if isinstance(result_ref, str) and result_ref.startswith("ses_"):
        from omniagentos.api.routes.sessions import get_sessions_dal

        session = get_sessions_dal().get_session(result_ref)
        project_dir = session.get("project_dir") if session is not None else None
        if isinstance(project_dir, str) and project_dir.strip():
            return board, Path(project_dir)

    run_id = board.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        run = store.get_run(run_id)
        if run is not None:
            working_dir = _working_dir_from_run(store, run)
            if working_dir:
                return board, Path(working_dir)

    if isinstance(result_ref, str) and result_ref.startswith("orch_"):
        working_dir = _orchestration_working_dir(store, result_ref)
        if working_dir:
            return board, Path(working_dir)

    return board, None


# --- approved-root floor (F-015) --------------------------------------------


def _repo_var_dir() -> str:
    """This repo's ``var/`` dir, honoring ``OMNIAGENTOS_VAR_DIR`` when set.

    Mirrors ``intake.service._intake_workspace_base``'s own resolution -- the
    SAME knob that anchors per-task intake workspaces and per-project managed
    workspaces: the env override wins when configured, else ``<repo>/var``
    computed from this package's own location so it never depends on the
    process cwd.
    """
    base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if not base:
        import omniagentos

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
        base = os.path.join(repo_root, "var")
    return os.path.realpath(os.path.expanduser(base))


def _serving_checkout_root() -> str:
    """Realpath of the checkout this process is actually running from."""
    import omniagentos

    return os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__))))


def _is_code_checkout_root(root: str) -> bool:
    """True when ``root`` (already realpath'd by the caller) is ITSELF a code checkout root.

    F-1 (LS-018): a grantable mount root can happen to BE a code checkout --
    including the live serving checkout -- and the F-015 floor must never
    admit that as a board-card workspace root; a card workspace rooted there
    would make this file-serving surface equivalent to open repo access.

    Two independent checks, both evaluated on the REALPATH (never the raw
    string -- a symlink, or a sibling directory whose name merely shares a
    prefix with the serving root, must not matter):

      1. ``root`` IS this process's own serving checkout (the case a live
         daemon is most exposed to). Decided by FILESYSTEM IDENTITY, not by
         spelling: the string compare is kept as an additive fast path, and
         :func:`omniagentos.path_containment.inode_paths_equal` refuses the
         same directory reached under any other name that ``realpath`` does
         not collapse (a bind mount, a mount namespace alias). ``is True`` is
         deliberate -- an UNKNOWN inode verdict must not silently become a
         refusal here, because check 2 and the lazily-created-root rule below
         own that case; unknown therefore adds nothing and removes nothing.
      2. ``root`` directly contains a version-control metadata marker (a repo
         worktree's marker is a *file* pointing at the real metadata dir, so
         both file and directory forms count).

    A root that simply does not exist yet (many approved roots, e.g. an
    intake var/ dir, are created lazily) is NOT itself a checkout -- absence
    is not evidence of the defect this closes, so it is left to the ordinary
    existence checks the floor already performs elsewhere. Only an actual OS
    error while probing the marker (permission denied, embedded NUL, etc.)
    fails CLOSED, since that is an unparseable/unresolvable path, not a
    legitimately-absent one.
    """
    serving_root = _serving_checkout_root()
    if root == serving_root or inode_paths_equal(root, serving_root) is True:
        return True
    try:
        return os.path.exists(os.path.join(root, ".git"))
    except (OSError, ValueError):
        return True


def _approved_workspace_roots() -> list[str]:
    """The APPROVED-ROOT FLOOR (F-015): realpath'd roots a card workspace may live under.

    A board card's workspace comes from a session/run/orchestration row this
    router does not author, so it must never be trusted as a capability on its
    own. Normally approved: this repo's ``var/`` dir (where intake and
    per-project managed workspaces actually live) and every currently-grantable
    mount root from :func:`omniagentos.mounts.grantable_mount_roots` (the SAME
    registry the directory-grant layer trusts) -- EXCLUDING any candidate that
    is itself a code checkout root (:func:`_is_code_checkout_root`, F-1/LS-018):
    a grantable mount root can happen to be the operator's production checkout,
    and admitting it here would turn this file-serving surface into open repo
    access via any board card whose workspace resolves under it.

    Simulation mode is resolved by :func:`omniagentos.simgate.resolve_sim_context`
    and admits exactly the canonical campaign root. It does not consult the
    machine mount registry or admit the executing checkout, which may be the
    operator's production checkout.

    Deliberately narrower than
    :func:`omniagentos.policy.dir_grants.allowed_grant_roots`: no bare
    ``~/OmniAgentOS`` default and no ``OMNIAGENTOS_PROJECT_BASES`` escape hatch
    -- a board card workspace is server-resolved, not an operator-typed grant.
    """
    from omniagentos.simgate import SimGateError, resolve_sim_context

    try:
        sim_ctx = resolve_sim_context()
    except SimGateError as exc:
        fail(403, "forbidden", str(exc))

    if sim_ctx.sim_mode:
        if sim_ctx.campaign_root is None:
            fail(403, "forbidden", "broken invariant: sim context has no campaign root")
        return [os.path.realpath(str(sim_ctx.campaign_root))]

    candidates = [_repo_var_dir()] + grantable_mount_roots()

    roots: list[str] = []
    for root in candidates:
        resolved = os.path.realpath(os.path.expanduser(root))
        if resolved in roots:
            continue
        if _is_code_checkout_root(resolved):
            continue
        roots.append(resolved)
    return roots


def _enforce_workspace_floor(workspace: Path) -> None:
    """403 unless ``workspace`` clears the F-015 floor.

    ``workspace`` MUST be absolute and already its own realpath; this is
    ENFORCED (not merely a caller precondition). Callers that forget
    ``.resolve(strict=True)`` get a loud 403 with a distinct message
    (``workspace path is not canonical``) rather than silent admission of a
    symlink- or ``..``-spelled path.

    Further fail-closed gates:

      1. ``workspace`` must sit under (or equal) one of
         :func:`_approved_workspace_roots` -- refuses a bare ``$HOME`` or any
         other unlisted directory a session/run/orchestration row happens to
         name (the reviewer's live probe: a ``$HOME``-rooted card served
         ``Library/Messages/chat.db``). Each root is realpath'd again here so
         containment is decided on real paths even if a root is
         symlink-spelled.
      2. Belt-and-braces: an APPROVED workspace that still sits under ``$HOME``
         by inode is additionally checked against the mounts home-top-level
         skip set
         (:data:`omniagentos.api.routes.mounts._HOME_TOP_LEVEL_SKIP`, imported
         not copied). The first inode-relative component is compared
         case-insensitively, so firmlink and APFS case aliases of, say,
         ``$HOME/Library`` are refused by the same access control mounts.py
         enforces for direct ``home`` mount requests.
    """
    # Fail-closed canonicality gate: must run FIRST so a non-canonical path
    # can never reach the admit path. Refuse loudly (do not silently normalize).
    try:
        canonical = os.path.realpath(str(workspace))
    except (OSError, ValueError):
        fail(
            403,
            "forbidden",
            "workspace path is not canonical",
            {"workspace": str(workspace)},
        )
    if not workspace.is_absolute() or canonical != str(workspace):
        fail(
            403,
            "forbidden",
            "workspace path is not canonical",
            {"workspace": str(workspace)},
        )

    # Defense in depth: realpath each approved root before containment so a
    # symlink-spelled root still admits a legitimately-contained workspace.
    # Skip roots that fail to realpath rather than crashing.
    roots = _approved_workspace_roots()
    canonical_roots: list[Path] = []
    for root in roots:
        try:
            canonical_roots.append(Path(os.path.realpath(root)))
        except (OSError, ValueError):
            continue
    if not any(inode_relative_parts(workspace, root) is not None for root in canonical_roots):
        fail(403, "forbidden", "workspace outside approved roots", {"workspace": str(workspace)})
    home = Path(os.path.realpath(os.path.expanduser("~")))
    rel_parts = inode_relative_parts(workspace, home)
    skipped_home_names = {name.casefold() for name in _HOME_TOP_LEVEL_SKIP}
    if rel_parts and rel_parts[0].casefold() in skipped_home_names:
        fail(
            403,
            "forbidden",
            "workspace is under a hidden home top-level directory",
            {"workspace": str(workspace)},
        )


def _require_workspace(
    store: Store, collab_store: CollabStore, task_id: str
) -> tuple[dict[str, Any], Path]:
    """Resolve + realpath a card's workspace, or fail with the documented 404s.

    The resolved workspace is then held to the F-015 approved-root floor
    (:func:`_enforce_workspace_floor`) -- applied AFTER the 404s so an unknown
    card or a card with no workspace still reads as "not found" rather than
    leaking floor state.
    """
    board, workspace = resolve_board_workspace(store, collab_store, task_id)
    if board is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    if workspace is None:
        fail(404, "not_found", "no workspace for this card", {"id": task_id})
    try:
        resolved = workspace.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        fail(404, "not_found", "no workspace for this card", {"id": task_id})
    if not resolved.is_dir():
        fail(404, "not_found", "no workspace for this card", {"id": task_id})
    _enforce_workspace_floor(resolved)
    return board, resolved


# --- containment + deny parity (mirrors mounts.py) -------------------------


def _resolve_contained_path(workspace: Path, relative: str) -> Path:
    """Reject traversal on the RAW string, then resolve with realpath containment.

    Mirrors ``mounts._safe_relative_path`` + ``mounts._resolve_within_mount``:
    NUL bytes / a leading '/' / any '..' segment are rejected before any
    filesystem access; the resolved candidate (symlinks collapsed) must then
    remain inside the resolved workspace root. Never raises anything but the
    documented 4xx ``ApiError`` -- a bad path is always a client error, never
    a 500.
    """
    if not relative or "\x00" in relative:
        fail(403, "forbidden", "path is invalid")
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/"):
        fail(403, "forbidden", "path must be relative to the workspace")
    if ".." in normalized.split("/"):
        fail(403, "forbidden", "path must not contain '..' segments")
    try:
        candidate = (workspace / normalized).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        fail(403, "forbidden", "path could not be resolved")
    if inode_relative_parts(candidate, workspace) is None:
        fail(403, "forbidden", "path is outside the workspace")
    return candidate


def _system_deny_roots() -> list[str]:
    """Realpath'd OS-sensitive roots that must never be served, upload or download.

    F-2 (LS-017): the prior N-4 denylist only knew the secret registry and the
    home-dotfile pattern -- it did not fail closed on plain OS directories, so
    a workspace resolving anywhere under ``/etc``, ``/tmp`` or ``~/.ssh`` still
    admitted individual files inside them. Each entry is realpath'd HERE, once,
    at import time, so containment below is always inode/realpath comparison,
    never a string prefix (macOS symlinks ``/etc`` -> ``/private/etc`` and
    ``/tmp`` -> ``/private/tmp``, so both spellings collapse to one root and a
    literal-prefix test on either spelling alone would miss the other).
    """
    home = os.path.expanduser("~")
    candidates = ["/private/etc", "/private/tmp", "/etc", "/tmp", os.path.join(home, ".ssh")]
    resolved: list[str] = []
    for candidate in candidates:
        try:
            real = os.path.realpath(candidate)
        except (OSError, ValueError):
            continue
        if real not in resolved:
            resolved.append(real)
    return resolved


_SYSTEM_DENY = _system_deny_roots()


def _deny_system(target: Path) -> None:
    """Fail-closed 403 for any path realpath-contained under :data:`_SYSTEM_DENY`.

    ``target`` is realpath'd again here (defense in depth: a caller that
    forgot to resolve it, or a TOCTOU symlink swap between an earlier resolve
    and this call, still gets caught) and compared to each denied root with
    :func:`omniagentos.path_containment.inode_relative_parts` -- the same
    inode-relative containment primitive every other consumer of this
    boundary uses, never an unresolved string-prefix test. An unresolvable
    target is denied, not silently admitted.
    """
    try:
        resolved = Path(os.path.realpath(target))
    except (OSError, ValueError):
        fail(403, "forbidden", "path could not be resolved")
    for root in _SYSTEM_DENY:
        try:
            root_path = Path(root)
        except (OSError, ValueError):
            continue
        if inode_relative_parts(resolved, root_path) is not None:
            fail(
                403,
                "forbidden",
                "path is under a denied system directory",
                {"root": root},
            )


def _deny_guard(target: Path) -> None:
    """Hard-403 a resolved path denied by system roots, secret-registry, or home-dotfile parity.

    Checks the module's own fail-closed system-directory denylist
    (:func:`_deny_system`, F-2/LS-017) FIRST, then imports (not reimplements)
    ``mounts._deny_secret`` / ``mounts._deny_home_dotfile`` so this surface can
    never drift looser than the mounts browse API.

    This is defense-in-depth BEHIND the F-015 approved-workspace-root floor
    (:func:`_enforce_workspace_floor`, checked earlier in every caller via
    :func:`_require_workspace` or the upload route) -- an allowlist already
    bounds which workspace roots a resolved path can even be reached under
    before ``_deny_guard`` ever runs. It is not the last line of defence on
    its own; it is the second, narrower gate that additionally refuses
    specific sensitive subpaths (OS directories, secrets, home dotfiles) even
    inside an otherwise-approved workspace tree. As a denylist it remains
    inherently incomplete -- new sensitive paths must be added explicitly --
    but the blast radius of a gap here is bounded by the floor already having
    rejected anything outside an approved root.
    """
    _deny_system(target)
    _deny_secret(target)
    _deny_home_dotfile(target)


def _is_denied(target: Path) -> bool:
    """Non-raising form of :func:`_deny_guard`, for filtering list/archive entries."""
    try:
        _deny_guard(target)
    except ApiError:
        return True
    return False


# --- upload filename policy (board-specific; see files_common.py docstring) -


def _sanitize_board_filename(filename: str | None) -> str:
    """Reduce an untrusted multipart filename to a bare, upload-safe basename.

    Strips any path components (both '/' and '\\' conventions) BEFORE
    validating, so a crafted ``"../../etc/passwd"`` or ``"sub/dir/x"`` filename
    collapses to its final segment rather than ever being trusted as a write
    path -- by construction the result can no longer contain a separator, so
    it cannot "normalize into an uploads/ escape". Empty, '.', '..', and
    dotfile-only results are rejected outright.

    Two more names are refused (review fix F-017), both surfaced as a 422 by
    :func:`omniagentos.api.files_common.collect_safe_uploads`'s ``ValueError``
    handling:

      * the reserved instructions-log basename
        (:data:`omniagentos.api.files_common.INSTRUCTIONS_LOG_NAME`) -- an
        upload literally named ``_instructions.md`` could otherwise splice
        forged lines into the append-only operator log; and
      * any secret-registry-named basename, via
        :func:`omniagentos.policy.secrets.references_secret` called with
        ``project_dir=None`` -- the SAME oracle the read path's
        :func:`_deny_secret` uses, so an upload can never write a filename
        (``id_rsa``, ``*.pem``, ``credentials``, ...) that the download/list/
        archive routes would refuse to ever serve back.
    """
    if filename is None or "\x00" in filename:
        raise ValueError("filename must not contain NUL bytes")
    candidate = filename.replace("\\", "/").split("/")[-1].strip()
    if not candidate or candidate in {".", ".."}:
        raise ValueError("filename must resolve to a real file name")
    if candidate.startswith("."):
        raise ValueError("filename must not be a dotfile")
    if len(candidate) > _MAX_FILENAME_LEN:
        raise ValueError(f"filename is too long (max {_MAX_FILENAME_LEN} characters)")
    if candidate == INSTRUCTIONS_LOG_NAME:
        raise ValueError(f"{INSTRUCTIONS_LOG_NAME!r} is a reserved filename")
    if references_secret(candidate, None):
        raise ValueError("filename matches a secret-registry name and cannot be uploaded")
    return candidate


# --- session-reported files --------------------------------------------------


def _session_reported_files(board: dict[str, Any]) -> list[str]:
    """Raw path strings the linked session's live stream reported writing.

    Empty unless the card's ``result_ref`` is a live/finished session id --
    the same ``files_json`` column ``sessions.dal.set_session_files``
    populates (migration 034), read RAW here (not through
    ``sessions._progress_fields``, which drops the column entirely) so this
    router owns its own existence/containment checks on each entry.
    """
    result_ref = board.get("result_ref")
    if not isinstance(result_ref, str) or not result_ref.startswith("ses_"):
        return []
    from omniagentos.api.routes.sessions import get_sessions_dal

    session = get_sessions_dal().get_session(result_ref)
    if session is None:
        return []
    raw = session.get("files_json")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item.strip()]


# --- routes ------------------------------------------------------------------


@router.post("/{task_id}/files/upload", status_code=201)
def upload_board_files(
    task_id: str,
    store: StoreDep,
    collab_store: CollabStoreDep,
    files: list[UploadFile] | None = None,
    instructions: str = Form(""),
) -> dict[str, Any]:
    """Save operator uploads under ``<card workspace>/uploads/``.

    Reuses the exact size caps / write path / instructions log
    :mod:`omniagentos.api.files_common` extracted from ``projects.py``'s
    upload endpoint; only the filename policy (basename-only, dotfile-only,
    secret-name, and reserved-instructions-log denied -- F-017) is
    board-specific. The resolved workspace is held to the same F-015
    approved-root floor (:func:`_enforce_workspace_floor`) the read routes
    enforce via :func:`_require_workspace` -- a write is "serving" this
    surface guards against exactly as much as a read.
    """
    board, workspace = resolve_board_workspace(store, collab_store, task_id)
    if board is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    if workspace is None:
        fail(404, "not_found", "no workspace for this card", {"id": task_id})
    if not files:
        fail(400, "validation", "at least one file is required")

    safe_files = collect_safe_uploads(files, validate_name=_sanitize_board_filename)

    # F-015 decides BEFORE the first side effect. ``workspace`` is whatever a
    # session/run/orchestration row named (``resolve_board_workspace``), which
    # the floor's own docstring says must never be trusted as a capability --
    # and ``mkdir(parents=True)`` on it IS trusting it: an out-of-root path was
    # created and only then refused, so a 403 still left a directory tree
    # behind anywhere the API process can write. The read routes never had this
    # (``_require_workspace`` resolves strictly and 404s), so only the write
    # path could create what it was about to refuse.
    #
    # ``realpath`` rather than ``resolve(strict=True)`` because the legitimate
    # workspace may not exist yet -- realpath collapses symlinks in whatever
    # prefix DOES exist and normalizes the rest, so a symlinked prefix pointing
    # out of root is still caught here. The post-create call below is unchanged
    # and still enforces canonicality on the real resolved path.
    _enforce_workspace_floor(Path(os.path.realpath(workspace)))

    try:
        workspace.mkdir(parents=True, exist_ok=True)
        workspace = workspace.resolve(strict=True)
        _enforce_workspace_floor(workspace)
        uploads_dir = (workspace / "uploads").resolve(strict=False)
        if inode_relative_parts(uploads_dir, workspace) is None:
            fail(422, "validation", "uploads directory must stay within the workspace")
        uploads_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir = uploads_dir.resolve(strict=True)
        if inode_relative_parts(uploads_dir, workspace) is None:
            fail(422, "validation", "uploads directory must stay within the workspace")
    except (OSError, RuntimeError, ValueError):
        fail(400, "validation", "workspace upload directory is unavailable")

    saved = save_uploads_to_dir(safe_files, uploads_dir)
    for item in saved:
        # Reuse the same post-write containment re-check the projects.py
        # upload path uses -- never trust the assembled path as a capability.
        resolved = (workspace / str(item["path"])).resolve(strict=False)
        if inode_relative_parts(resolved, workspace) is None or not resolved.is_file():
            fail(422, "validation", "upload path must stay within the workspace")

    append_upload_instructions(uploads_dir, [str(item["name"]) for item in saved], instructions)
    return {"saved": saved, "workspace": str(workspace)}


@router.get("/{task_id}/files")
def list_board_files(task_id: str, store: StoreDep, collab_store: CollabStoreDep) -> dict[str, Any]:
    """List a card's ``uploads/**`` + ``outputs/**`` + session-reported files.

    Entries are existence-checked, realpath-contained, deduped by resolved
    path (uploads/outputs win over a session-reported duplicate of the same
    file), and denied by the same secret-registry/home-dotfile rules as
    download/archive. Bounded to ``_MAX_LIST_ENTRIES``, sorted by mtime desc.
    """
    _board, workspace = _require_workspace(store, collab_store, task_id)

    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def _add(candidate: Path, kind: str) -> None:
        if len(rows) >= _MAX_LIST_ENTRIES:
            return
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return
        if resolved in seen or inode_relative_parts(resolved, workspace) is None:
            return
        if not resolved.is_file() or _is_denied(resolved):
            return
        try:
            stat = resolved.stat()
        except OSError:
            return
        seen.add(resolved)
        rows.append(
            {
                "rel": resolved.relative_to(workspace).as_posix(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "kind": kind,
            }
        )

    for sub, kind in (("uploads", "upload"), ("outputs", "output")):
        base = workspace / sub
        if not base.is_dir():
            continue
        try:
            for path in base.rglob("*"):
                if len(rows) >= _MAX_LIST_ENTRIES:
                    break
                if path.is_file():
                    _add(path, kind)
        except OSError:
            pass

    for raw in _session_reported_files(_board):
        if len(rows) >= _MAX_LIST_ENTRIES:
            break
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        _add(candidate, "session_file")

    rows.sort(key=lambda row: float(row["mtime"]), reverse=True)
    return {"workspace": str(workspace), "files": rows[:_MAX_LIST_ENTRIES]}


@router.get("/{task_id}/files/download")
def download_board_file(
    task_id: str, store: StoreDep, collab_store: CollabStoreDep, path: str = Query(...)
) -> Any:
    """Stream a single workspace-relative file as an attachment.

    404 on an unknown card or a missing file; 4xx on any containment/deny
    violation; 413 above ``_MAX_DOWNLOAD_BYTES`` (50 MB) -- never a 500.
    """
    _board, workspace = _require_workspace(store, collab_store, task_id)
    target = _resolve_contained_path(workspace, path)
    _deny_guard(target)
    if not target.is_file():
        fail(404, "not_found", "file not found", {"path": path})
    try:
        size = target.stat().st_size
    except OSError:
        fail(404, "not_found", "file not found", {"path": path})
    if size > _MAX_DOWNLOAD_BYTES:
        fail(
            413,
            "file_too_large",
            f"files above {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB cannot be downloaded",
            {"path": path},
        )
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)


def _archive_candidates(workspace: Path) -> list[Path]:
    candidates: list[Path] = []
    for sub in ("uploads", "outputs"):
        base = workspace / sub
        if not base.is_dir():
            continue
        try:
            candidates.extend(sorted(p for p in base.rglob("*") if p.is_file()))
        except OSError:
            continue
    return candidates


@router.get("/{task_id}/files/archive")
def download_board_archive(
    task_id: str, store: StoreDep, collab_store: CollabStoreDep
) -> StreamingResponse:
    """Zip a card's ``uploads/`` + ``outputs/``, bounded and deny-filtered.

    Stops adding files once EITHER ``_MAX_ARCHIVE_BYTES`` (500 MB of source
    bytes) or ``_MAX_ARCHIVE_FILES`` (2000) is reached -- the response still
    succeeds (200) with whatever fit, reporting the truncation via
    ``X-Archive-Truncated`` / ``X-Archive-File-Count`` response headers rather
    than failing the whole download. A symlink inside the workspace that
    resolves outside it is silently skipped (never followed into the zip),
    exactly like the per-file denies. The zip is built into a
    :class:`tempfile.SpooledTemporaryFile` (review fix F-018) instead of an
    in-memory ``BytesIO``: it stays in memory up to ``_ARCHIVE_SPOOL_MAX_BYTES``
    and transparently spills to a real temp file on disk beyond that, so an
    archive approaching ``_MAX_ARCHIVE_BYTES`` never has to live entirely in
    process memory at once. Streamed to the client exactly as before.
    """
    _board, workspace = _require_workspace(store, collab_store, task_id)

    buffer = SpooledTemporaryFile(max_size=_ARCHIVE_SPOOL_MAX_BYTES)
    total_bytes = 0
    file_count = 0
    truncated = False
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for candidate in _archive_candidates(workspace):
            if file_count >= _MAX_ARCHIVE_FILES or total_bytes >= _MAX_ARCHIVE_BYTES:
                truncated = True
                break
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                continue
            if inode_relative_parts(resolved, workspace) is None or not resolved.is_file():
                continue
            if _is_denied(resolved):
                continue
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            if total_bytes + size > _MAX_ARCHIVE_BYTES:
                truncated = True
                break
            arcname = resolved.relative_to(workspace).as_posix()
            try:
                archive.write(resolved, arcname=arcname)
            except OSError:
                continue
            total_bytes += size
            file_count += 1
    buffer.seek(0)
    headers = {
        "Content-Disposition": f'attachment; filename="{task_id}.zip"',
        "X-Archive-File-Count": str(file_count),
        "X-Archive-Truncated": "true" if truncated else "false",
    }
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)


# --- open-locally / reveal (C0) ----------------------------------------------

# Kill switch: any truthy value disables the whole local-reveal surface (403).
_DISABLE_REVEAL_ENV = "OMNIAGENTOS_DISABLE_LOCAL_REVEAL"

# The reveal subprocess is hard-bounded: ``/usr/bin/open`` returns immediately,
# but a wedged LaunchServices must never hang the request.
_REVEAL_TIMEOUT_S = 5

# macOS bundle ids for the "Open in <app>" targets. The argv is assembled
# ENTIRELY server-side from this FIXED map -- the client only names an app KEY
# (``finder``/``vscode``/``cursor``/``terminal``), never a path to an executable
# or a bundle id -- so there is no shell (``shell=True`` is never used) and no
# argument-injection surface.
_REVEAL_APP_BUNDLES: dict[str, str] = {
    "vscode": "com.microsoft.VSCode",
    "cursor": "com.todesktop.230313mzl4w4u92",
    "terminal": "com.apple.Terminal",
}
# ``finder`` is special-cased (``open -R`` reveals AND selects the target); the
# bundle-id apps open the containing directory instead.
_REVEAL_APPS = frozenset({"finder", *_REVEAL_APP_BUNDLES})


class RevealRequest(BaseModel):
    """Body for ``POST /{task_id}/files/reveal``: a workspace-relative path (or
    none -> the workspace root) and an app key (default ``finder``)."""

    path: str | None = None
    app: str | None = None


@router.post("/{task_id}/files/reveal")
def reveal_board_file(
    task_id: str,
    body: RevealRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
) -> dict[str, Any]:
    """Reveal a card's workspace (or a file in it) in a local macOS app.

    A localhost-only convenience: the operator clicks "Reveal in Finder" /
    "Open in VS Code" and the SERVER opens it, because the browser cannot. This
    is the SAME file-serving security surface as the read routes and reuses
    their exact guardrails, in this order:

      1. :func:`_require_workspace` -> the F-015 approved-root floor (403 for a
         ``$HOME``/unlisted workspace) plus the documented 404s.
      2. Unknown ``app`` -> 422; the kill switch env -> 403 (both before any
         filesystem work, so the surface can be disabled outright).
      3. :func:`_resolve_contained_path` -> traversal rejected on the RAW string
         and symlinks COLLAPSED BEFORE the containment check, so a symlink inside
         the workspace that targets OUTSIDE it is a 403 and is never opened; then
         :func:`_deny_guard` for the secret-registry / home-dotfile denies. This
         runs BEFORE the platform gate so the containment check is fail-closed
         regardless of host.
      4. Non-darwin host -> 501.

    The argv is a FIXED, server-side map (``finder``: ``open -R <path>``; the
    others: ``open -b <bundle-id> <dir>``), never ``shell=True``, hard-bounded
    to ``_REVEAL_TIMEOUT_S``. Request bodies carry a WORKSPACE-RELATIVE path
    only (or none -> the workspace root).
    """
    _board, workspace = _require_workspace(store, collab_store, task_id)

    app = (body.app or "finder").strip().lower()
    if app not in _REVEAL_APPS:
        fail(422, "validation", "unknown reveal app", {"app": app})

    if os.environ.get(_DISABLE_REVEAL_ENV):
        fail(403, "forbidden", "local reveal is disabled on this host")

    rel = (body.path or "").strip()
    if rel:
        target = _resolve_contained_path(workspace, rel)
        _deny_guard(target)
        if not target.exists():
            fail(404, "not_found", "file not found", {"path": rel})
    else:
        target = workspace

    if platform.system() != "Darwin":
        fail(501, "not_implemented", "local reveal is only available on macOS")

    if app == "finder":
        argv = ["/usr/bin/open", "-R", str(target)]
    else:
        directory = target if target.is_dir() else target.parent
        # The parent of a contained file is still contained, but re-check
        # belt-and-braces before handing a directory to the launcher.
        if inode_relative_parts(directory, workspace) is None:
            fail(403, "forbidden", "path is outside the workspace")
        argv = ["/usr/bin/open", "-b", _REVEAL_APP_BUNDLES[app], str(directory)]

    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, never shell=True
            argv,
            capture_output=True,
            timeout=_REVEAL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fail(504, "reveal_timeout", "the reveal command timed out", {"app": app})
    except OSError:
        fail(500, "reveal_failed", "could not launch the reveal command", {"app": app})
    if result.returncode != 0:
        fail(502, "reveal_failed", "the reveal command failed", {"app": app})

    rel_out = target.relative_to(workspace).as_posix() if target != workspace else ""
    return {"revealed": True, "app": app, "path": rel_out, "workspace": str(workspace)}


__all__ = ["resolve_board_workspace", "router"]
