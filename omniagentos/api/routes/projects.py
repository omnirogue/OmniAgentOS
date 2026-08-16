"""HTTP surface for W2 projects and their scoped permission grants.

A project scopes work, budget, tool grants and (eventually) filesystem reach.
Its detail view returns the *resolved* action-class policy: the global
``configs/policy.yaml`` posture with this project's grants overlaid, so an
operator can see exactly what the project loosens or tightens -- and confirm a
globally hard-human class (``consequential``) stays hard-human regardless.
"""

from __future__ import annotations

import logging
import mimetypes
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omniagentos.api.deps import PolicyDep, StoreDep
from omniagentos.api.files_common import (
    append_upload_instructions,
    collect_safe_uploads,
    save_uploads_to_dir,
)
from omniagentos.api.routes.control import fail
from omniagentos.db.store import SqliteStore
from omniagentos.path_containment import inode_relative_parts
from omniagentos.policy import PolicyConfig, PolicyError, validate_tools
from omniagentos.policy.dir_grants import DirGrantError, validate_grant_dir
from omniagentos.projects import (
    ProjectError,
    ProjectStore,
    assert_grants_monotonic,
    build_project_activity,
    project_pending_activity,
    resolve_project_policy,
)
from omniagentos.projects.activity import project_log_dir
from omniagentos.simgate import SimGateError, resolve_sim_context

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])

# Bounds keep a single request from smuggling in unbounded strings/lists (F7).
_MAX_NAME = 200
_MAX_PATH = 4096
_MAX_LIST = 64
_MAX_FILE_COUNT = 1_000
_MAX_TEXT_PREVIEW_BYTES = 1_000_000
# An org_companies id/slug is a short internal identifier; bound it so an
# oversized reference never reaches a SQL round trip or gets reflected back
# in an error body (F-company ADOPT 3).
_MAX_ORG_COMPANY_REF = 255
_ORG_COMPANY_REF_ERROR_PREVIEW = 80
_TEXT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".css",
        ".html",
        ".js",
        ".json",
        ".log",
        ".md",
        ".mdx",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_IMAGE_EXTENSIONS = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})


def _reject_traversal(value: str, *, absolute: bool) -> str:
    """Normalize-and-reject a filesystem path fragment (F7).

    Rejects NUL bytes, ``..`` segments, and enforces the absolute/relative
    contract. Absolute roots must start at ``/``; the vault subfolder must be a
    relative path that cannot climb out of the project's vault folder.
    """
    if "\x00" in value:
        raise ValueError("path must not contain NUL bytes")
    if len(value) > _MAX_PATH:
        raise ValueError(f"path is too long (max {_MAX_PATH} characters)")
    segments = value.replace("\\", "/").split("/")
    if ".." in segments:
        raise ValueError("path must not contain '..' segments")
    if absolute:
        if not value.startswith("/"):
            raise ValueError("must be an absolute path (starting with '/')")
    elif value.startswith("/"):
        raise ValueError("must be a relative path (no leading '/')")
    return value


class ProjectModel(BaseModel):
    # Reject unknown fields so a typo (or a silently-dropped policy control) is a
    # 422, not an accepted-and-ignored no-op (F7).
    model_config = ConfigDict(extra="forbid")


class GrantInput(ProjectModel):
    action_class: str
    requires_approval: bool = True
    always_human: bool = False


class CreateProjectRequest(ProjectModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME)
    parent_project_id: str | None = None
    root_dirs: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    vault_subfolder: str = Field(default="", max_length=_MAX_PATH)
    budget_usd: float | None = Field(default=None, ge=0)
    allowed_tools: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    allowed_dirs: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    grants: list[GrantInput] = Field(default_factory=list, max_length=_MAX_LIST)

    @field_validator("root_dirs", "allowed_dirs")
    @classmethod
    def _validate_dirs(cls, value: list[str]) -> list[str]:
        return [_reject_traversal(item, absolute=True) for item in value]

    @field_validator("vault_subfolder")
    @classmethod
    def _validate_vault(cls, value: str) -> str:
        return _reject_traversal(value, absolute=False) if value else value

    @field_validator("allowed_tools")
    @classmethod
    def _bound_tools(cls, value: list[str]) -> list[str]:
        for tool in value:
            if len(tool) > _MAX_PATH:
                raise ValueError("tool name is too long")
        return value


def _project_store(store: StoreDep) -> ProjectStore:
    return ProjectStore(cast(SqliteStore, store))


def _is_within(candidate: Path, root: Path) -> bool:
    """Return whether a candidate remains in a grant root by shared inode proof."""
    return inode_relative_parts(candidate, root) is not None


def _project_file_roots(project: dict[str, Any]) -> list[Path]:
    """Load the canonical filesystem roots exposed through a project's Files tab.

    The scoped workspace under ``var/projects/<id>`` is included alongside the
    project's explicit root/allowed grants. Explicit grants were canonicalized
    before persistence and MUST NOT be resolved again here: doing so would turn
    an attacker-repointed symlink path into a newly trusted root. Candidate paths
    are resolved separately and checked against these immutable pathnames.
    """
    raw_roots = [*project.get("root_dirs", []), *project.get("allowed_dirs", [])]
    try:
        raw_roots.append(str(project_log_dir(str(project["id"])).parent))
    except (OSError, RuntimeError, ValueError):
        _LOG.warning("could not resolve managed workspace for project %s", project.get("id"))

    roots: list[Path] = []
    for raw in raw_roots:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            root = Path(raw)
            if not root.is_absolute():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _safe_file_path(file_path: str) -> Path:
    """Validate an untrusted project-relative path before resolving it.

    This deliberately rejects ``..`` rather than normalising it away, giving a
    clear forbidden response for traversal attempts before any filesystem read.
    """
    if not file_path or "\x00" in file_path or len(file_path) > _MAX_PATH:
        fail(403, "forbidden", "file path is invalid")
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        fail(403, "forbidden", "file path must stay within the project directories")
    return Path(normalized)


def _safe_upload_filename(filename: str | None) -> str:
    """Accept only a single, project-relative filename for an upload."""
    if not filename or "\x00" in filename or "/" in filename or "\\" in filename:
        raise ValueError("filename must not contain path separators or NUL bytes")
    if filename in {".", ".."} or ".." in filename:
        raise ValueError("filename must not contain '..'")
    if len(filename) > _MAX_PATH:
        raise ValueError(f"filename is too long (max {_MAX_PATH} characters)")
    # Keep upload validation aligned with file-read validation.  The upload
    # target itself may not exist yet, so _resolve_project_file cannot apply.
    _safe_file_path(f"uploads/{filename}")
    return filename


def _resolve_project_file(file_path: str, roots: list[Path]) -> Path:
    """Resolve a relative file request while enforcing realpath containment."""
    relative = _safe_file_path(file_path)
    stayed_within_a_root = False
    for root in roots:
        try:
            # Path.resolve() is the security boundary: it collapses symlinks and
            # catches a link inside a project directory that targets elsewhere.
            candidate = (root / relative).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if _is_within(candidate, root):
            stayed_within_a_root = True
            if candidate.is_file():
                return candidate
    if stayed_within_a_root:
        fail(404, "not_found", "file not found", {"path": file_path})
    fail(403, "forbidden", "file is outside the project's granted directories")


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or (suffix[1:] if suffix else "file")


def _file_row(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": path.relative_to(root).as_posix(),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "type": _file_kind(path),
    }


def _with_resolved_policy(project: dict[str, Any], cfg: PolicyConfig) -> dict[str, Any]:
    resolved = resolve_project_policy(cfg, project.get("grants", []))
    project["resolved_policy"] = {
        action_class.value: {
            "requires_approval": policy.requires_approval,
            "always_human": policy.always_human,
        }
        for action_class, policy in resolved.items()
    }
    return project


@router.get("")
def list_projects(store: StoreDep, policy_cfg: PolicyDep) -> list[dict[str, Any]]:
    # Every row carries grants + resolved_policy so the response matches the
    # declared Project type; without resolved_policy the dashboard crashed (F5).
    return [
        _with_resolved_policy(project, policy_cfg)
        for project in _project_store(store).list_projects()
    ]


@router.get("/portfolio")
def get_portfolio(store: StoreDep) -> dict[str, Any]:
    """Redesign Phase B: one payload for the attention queue (constant query count)."""
    from omniagentos.projects.portfolio import build_portfolio

    try:
        return build_portfolio(cast(Any, store)._connection)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("portfolio assembly failed")
        fail(500, "portfolio", str(exc))
        return {}  # unreachable


@router.post("", status_code=201)
def create_project(
    body: CreateProjectRequest, store: StoreDep, policy_cfg: PolicyDep
) -> dict[str, Any]:
    projects = _project_store(store)
    grants = [grant.model_dump() for grant in body.grants]
    # Policy-aware validation happens *before* any write so an invalid tool or a
    # relaxation attempt is a clean 422 (F1/F7) and never conflated with the
    # 409 name-conflict path (F9).
    try:
        if body.allowed_tools:
            validate_tools(body.allowed_tools, policy_cfg)
        assert_grants_monotonic(policy_cfg, grants)
    except PolicyError as exc:
        fail(422, "validation", str(exc), {"name": body.name})
    # SECURITY (AC-policy fix7): every directory grant must resolve inside a
    # user-approved allow-root AND must not be (or contain) a secret-registry dir.
    # Closes the self-grant escape where an agent POSTs root_dirs:[~/.ssh] (or a
    # broad ~ that engulfs it) and then rides that attacker-defined scope into a
    # run's trusted working_dir. Rejected BEFORE any write, as a clean 422.
    try:
        sim_ctx = resolve_sim_context()
        canonical_root_dirs = [
            str(Path(validate_grant_dir(directory, sim_ctx=sim_ctx)).resolve(strict=True))
            for directory in body.root_dirs
        ]
        canonical_allowed_dirs = [
            str(Path(validate_grant_dir(directory, sim_ctx=sim_ctx)).resolve(strict=True))
            for directory in body.allowed_dirs
        ]
        if any(
            not Path(directory).is_dir()
            for directory in (*canonical_root_dirs, *canonical_allowed_dirs)
        ):
            raise DirGrantError("directory grants must reference existing directories")
    except SimGateError as exc:
        fail(403, "forbidden", str(exc), {"name": body.name})
    except (OSError, RuntimeError, ValueError) as exc:
        if not isinstance(exc, DirGrantError):
            exc = DirGrantError(f"directory grant must reference an existing directory: {exc}")
        fail(422, "validation", str(exc), {"name": body.name})
    try:
        project = projects.create_project(
            {
                "name": body.name,
                "parent_project_id": body.parent_project_id,
                "root_dirs": canonical_root_dirs,
                "vault_subfolder": body.vault_subfolder,
                "budget_usd": body.budget_usd,
                "allowed_tools": body.allowed_tools,
                "allowed_dirs": canonical_allowed_dirs,
            },
            grants=grants,
        )
    except ProjectError as exc:
        fail(409, "conflict", str(exc), {"name": body.name})
    return _with_resolved_policy(project, policy_cfg)


class PatchProjectRequest(ProjectModel):
    """PATCH body for project updates (reparent + optional Jira/company mapping)."""

    parent_project_id: str | None = None
    # JG-1: map an OmniAgentOS project to a live Jira project key (ACM·CA·INI·HOO·OAOS).
    jira_project_key: str | None = None
    # Company axis writer: an org_companies.id OR slug (operators think in slugs);
    # resolved server-side and always stored as the id. null clears the assignment.
    org_company_id: str | None = None


# Back-compat alias for importers/tests that still name the reparent body.
ReparentProjectRequest = PatchProjectRequest


def _live_jira_keys_from_config() -> frozenset[str]:
    """Live key invariant from ``configs/jira_fields.yaml`` (data-driven)."""
    from omniagentos.connectors.jira_client import load_jira_fields_config

    cfg = load_jira_fields_config()
    keys = cfg.get("project_keys") or []
    if not isinstance(keys, list):
        return frozenset()
    return frozenset(str(k).strip().upper() for k in keys if str(k).strip())


def _normalize_jira_project_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = str(raw).strip().upper()
    return key or None


def _validate_jira_key_before_write(key: str | None) -> None:
    """Refuse unknown keys with 400 naming the allowed set (before any write)."""
    if key is None:
        return
    allowed = _live_jira_keys_from_config()
    if key not in allowed:
        named = ", ".join(sorted(allowed))
        fail(
            400,
            "invalid_jira_project_key",
            f"jira_project_key must be one of {{{named}}}",
            {"jira_project_key": key, "allowed": sorted(allowed)},
        )


def _normalize_org_company_ref(raw: str | None) -> str | None:
    """Trim an operator-supplied company id/slug.

    ``None`` -- an EXPLICIT JSON ``null`` -- clears the assignment and is
    returned as-is. A *present* value is returned verbatim after stripping
    surrounding whitespace, including when that leaves "" -- a
    present-but-blank reference must NEVER be silently treated as a clear
    request (F-company MUST-FIX 1: Sol caught ``""`` and whitespace-only both
    nulling the column). It flows into ``_resolve_org_company_id`` like any
    other candidate reference and is rejected there with the same
    ``{field, allowed}`` shape as an unknown id/slug, since no company ever
    has an empty id or slug.
    """
    if raw is None:
        return None
    return str(raw).strip()


def _truncate_org_company_ref_for_error(value: str) -> str:
    """Never reflect an oversized reference back at full length (F-company ADOPT 3)."""
    if len(value) <= _ORG_COMPANY_REF_ERROR_PREVIEW:
        return value
    return f"{value[:_ORG_COMPANY_REF_ERROR_PREVIEW]}…(truncated, {len(value)} chars total)"


def _validate_org_company_ref_length_before_write(reference: str | None) -> None:
    """Refuse an oversized reference before any DB round trip or write.

    A bare ``Field(max_length=...)`` on the request model would 422 just as
    early, but FastAPI's default validation-error body echoes the raw input
    verbatim -- exactly the full-length reflection ADOPT 3 forbids. Doing the
    bound here keeps the response on the same ``fail()`` shape as every other
    org_company_id rejection and lets us truncate the echoed value.
    """
    if reference is not None and len(reference) > _MAX_ORG_COMPANY_REF:
        fail(
            400,
            "invalid_org_company_id",
            f"org_company_id must be at most {_MAX_ORG_COMPANY_REF} characters",
            {
                "org_company_id": _truncate_org_company_ref_for_error(reference),
                "max_length": _MAX_ORG_COMPANY_REF,
            },
        )


class _UnknownOrgCompanyError(ValueError):
    """Raised inside the write txn when no live org_companies row matches (F-company)."""

    def __init__(self, value: str, allowed: list[str]) -> None:
        super().__init__(f"unknown org company {value!r}")
        self.value = value
        self.allowed = allowed


def _resolve_org_company_id(reference: str | None, connection: sqlite3.Connection) -> str | None:
    """Resolve an org_companies id OR slug to its canonical id.

    Operators think in slugs (be liberal in what is accepted); the FK column
    always stores the id (be strict in what is stored). ``None`` clears the
    project's company assignment; "" never reaches here as a clear -- see
    ``_normalize_org_company_ref``. Runs against the connection already
    inside the write transaction so existence is checked against the live
    row, not a stale read taken before the lock -- mirrors the
    jira_project_key uniqueness re-check below.
    """
    if reference is None:
        return None
    row = connection.execute(
        "SELECT id FROM org_companies WHERE id = ? OR slug = ?", (reference, reference)
    ).fetchone()
    if row is None:
        allowed = [
            str(r["slug"])
            for r in connection.execute("SELECT slug FROM org_companies ORDER BY slug").fetchall()
        ]
        raise _UnknownOrgCompanyError(reference, allowed)
    return str(row["id"])


def _patch_project_atomic(
    database: SqliteStore,
    projects: ProjectStore,
    project_id: str,
    *,
    update_parent: bool,
    parent_project_id: str | None,
    update_jira_key: bool,
    jira_project_key: str | None,
    update_company: bool,
    org_company_id: str | None,
) -> dict[str, Any]:
    """Apply parent + jira_project_key + org_company_id in one transaction.

    A 409 (reparent cycle / duplicate jira key) or a 400 (unknown company)
    rolls all three back together -- ``_execute_write_txn`` rolls back on any
    exception raised from ``body``.
    """

    def body(connection: sqlite3.Connection) -> dict[str, Any]:
        if update_parent:
            # Mirror ProjectStore.set_parent invariants inside the same txn.
            row = connection.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectError(f"unknown project {project_id!r}")
            normalized = None if parent_project_id is None else str(parent_project_id)
            if normalized is not None:
                if normalized == project_id:
                    raise ProjectError("a project cannot be its own parent")
                parent_row = connection.execute(
                    "SELECT id FROM projects WHERE id = ?", (normalized,)
                ).fetchone()
                if parent_row is None:
                    raise ProjectError(f"unknown parent project {normalized!r}")
                ancestor: str | None = normalized
                seen: set[str] = set()
                while ancestor is not None:
                    if ancestor == project_id:
                        raise ProjectError("re-parenting would create a cycle in the project tree")
                    if ancestor in seen:
                        break
                    seen.add(ancestor)
                    prow = connection.execute(
                        "SELECT parent_project_id FROM projects WHERE id = ?",
                        (ancestor,),
                    ).fetchone()
                    ancestor = None if prow is None else prow["parent_project_id"]
            connection.execute(
                "UPDATE projects SET parent_project_id = ? WHERE id = ?",
                (normalized, project_id),
            )
        if update_jira_key:
            # Uniqueness re-check inside the txn; IntegrityError aborts parent too.
            if jira_project_key is not None:
                clash = connection.execute(
                    "SELECT id FROM projects WHERE jira_project_key = ? AND id != ?",
                    (jira_project_key, project_id),
                ).fetchone()
                if clash is not None:
                    raise sqlite3.IntegrityError("jira_project_key unique")
            connection.execute(
                "UPDATE projects SET jira_project_key = ? WHERE id = ?",
                (jira_project_key, project_id),
            )
        if update_company:
            # Existence check happens against this same connection/txn -- an
            # unknown id/slug raises before any statement below ever runs, and
            # an unrelated jira/parent failure rolls this UPDATE back too.
            resolved_company_id = _resolve_org_company_id(org_company_id, connection)
            connection.execute(
                "UPDATE projects SET org_company_id = ? WHERE id = ?",
                (resolved_company_id, project_id),
            )
        refreshed = connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if refreshed is None:
            raise ProjectError(f"unknown project {project_id!r}")
        # ProjectStore shaping (grants etc.) via get after commit.
        return {"id": project_id}

    try:
        database._execute_write_txn(body, op="projects.patch_fields")
    except _UnknownOrgCompanyError as exc:
        named = ", ".join(sorted(exc.allowed))
        if exc.value == "":
            # F-company MUST-FIX 1: blank is invalid input, not a clear
            # request -- say so explicitly, distinct from "unknown reference".
            message = (
                "org_company_id must not be blank; send null (not an empty "
                f"string) to clear the assignment -- must be one of {{{named}}}"
            )
        else:
            message = f"org_company_id must be one of {{{named}}}"
        fail(
            400,
            "invalid_org_company_id",
            message,
            {
                "org_company_id": _truncate_org_company_ref_for_error(exc.value),
                "allowed": sorted(exc.allowed),
            },
        )
    except sqlite3.IntegrityError:
        fail(
            409,
            "conflict",
            "jira_project_key already mapped to another project",
            {"jira_project_key": jira_project_key},
        )
    except sqlite3.OperationalError as exc:
        fail(
            503,
            "migration_required",
            "jira_project_key column is not available",
            {"detail": str(exc.__class__.__name__)},
        )
    except ProjectError as exc:
        fail(409, "conflict", str(exc), {"id": project_id})
    project = projects.get_project(project_id)
    if project is None:
        fail(404, "not_found", "project not found", {"id": project_id})
    return project


@router.patch("/{project_id}")
def reparent_project(
    project_id: str,
    body: PatchProjectRequest,
    store: StoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    projects = _project_store(store)
    if projects.get_project(project_id) is None:
        fail(404, "not_found", "project not found", {"id": project_id})

    fields_set = body.model_fields_set
    key_provided = "jira_project_key" in fields_set
    company_provided = "org_company_id" in fields_set
    # Reparent when the field is present (existing behaviour) or when it is the
    # only field callers historically sent (no jira/company field at all).
    parent_provided = "parent_project_id" in fields_set or not (key_provided or company_provided)

    jira_key: str | None = None
    if key_provided:
        jira_key = _normalize_jira_project_key(body.jira_project_key)
        # DEFECT 3/4: validate live key BEFORE any write.
        _validate_jira_key_before_write(jira_key)

    org_company_ref: str | None = None
    if company_provided:
        org_company_ref = _normalize_org_company_ref(body.org_company_id)
        # F-company ADOPT 3: bound BEFORE any write / DB round trip.
        _validate_org_company_ref_length_before_write(org_company_ref)

    database = cast(SqliteStore, store)
    if (key_provided or company_provided) and not hasattr(database, "_execute_write_txn"):
        fail(
            501,
            "unsupported",
            "jira_project_key/org_company_id updates require the sqlite store",
            {"id": project_id},
        )

    if parent_provided and (key_provided or company_provided):
        # One transaction: a 409/400 on jira/company leaves parent unchanged.
        project = _patch_project_atomic(
            database,
            projects,
            project_id,
            update_parent=True,
            parent_project_id=body.parent_project_id,
            update_jira_key=key_provided,
            jira_project_key=jira_key,
            update_company=company_provided,
            org_company_id=org_company_ref,
        )
    elif parent_provided:
        try:
            project = projects.set_parent(project_id, body.parent_project_id)
        except ProjectError as exc:
            fail(409, "conflict", str(exc), {"id": project_id})
    else:
        # jira_project_key and/or org_company_id only — still one transactional
        # write path so setting both together rolls back together (F-company).
        project = _patch_project_atomic(
            database,
            projects,
            project_id,
            update_parent=False,
            parent_project_id=None,
            update_jira_key=key_provided,
            jira_project_key=jira_key,
            update_company=company_provided,
            org_company_id=org_company_ref,
        )
    return _with_resolved_policy(project, policy_cfg)


# GET /api/projects/tree is owned exclusively by hierarchy_router
# (omniagentos.api.routes.hierarchy.project_tree). A second registration here
# made one implementation unreachable (L-01). Do not re-add this path.


@router.post("/{project_id}/files/upload", status_code=201)
def upload_project_files(
    project_id: str,
    store: StoreDep,
    files: list[UploadFile] | None = None,
    instructions: str = Form(""),
) -> dict[str, Any]:
    """Save operator uploads under ``<project workspace>/uploads/``.

    The app-wide session-token dependency gates this mutation.  Each upload is
    bounded before writing (shared caps/write path in
    :mod:`omniagentos.api.files_common`), and the optional operator note is
    appended to the workspace-local ``uploads/_instructions.md`` log for agents
    to reference.
    """
    project = _project_store(store).get_project(project_id)
    if project is None:
        fail(400, "not_found", "project not found", {"id": project_id})
    if not files:
        fail(400, "validation", "at least one file is required")

    safe_files = collect_safe_uploads(files, validate_name=_safe_upload_filename)

    # The managed workspace is always part of the canonical project roots.
    roots = _project_file_roots(project)
    workspace = project_log_dir(project_id).parent
    if workspace not in roots:
        fail(400, "validation", "project workspace is unavailable")
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        workspace = workspace.resolve(strict=True)
        uploads_dir = (workspace / "uploads").resolve(strict=False)
        if not _is_within(uploads_dir, workspace):
            fail(422, "validation", "uploads directory must stay within the project workspace")
        uploads_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir = uploads_dir.resolve(strict=True)
        if not _is_within(uploads_dir, workspace):
            fail(422, "validation", "uploads directory must stay within the project workspace")
    except (OSError, RuntimeError, ValueError):
        fail(400, "validation", "project upload directory is unavailable")

    saved = save_uploads_to_dir(safe_files, uploads_dir)
    uploaded: list[dict[str, str]] = []
    for item in saved:
        # Reuse the existing file-read containment boundary after the write,
        # rather than trusting the path assembled above as a capability.
        _resolve_project_file(item["path"], [workspace])
        uploaded.append({"name": item["name"], "path": item["path"]})

    append_upload_instructions(uploads_dir, [item["name"] for item in uploaded], instructions)
    return {"uploaded": uploaded, "instructions_saved": True}


@router.get("/{project_id}/files")
def list_project_files(project_id: str, store: StoreDep) -> dict[str, list[dict[str, Any]]]:
    """List files in the project's workspace and explicitly granted directories.

    The response deliberately exposes only paths relative to a grant root.  A
    later read resolves the supplied relative path again; it never trusts this
    listing as a filesystem capability.
    """
    project = _project_store(store).get_project(project_id)
    if project is None:
        fail(404, "not_found", "project not found", {"id": project_id})
    roots = _project_file_roots(project)

    # Orchestration-created projects persist their managed ``workspace/`` as a
    # normal root grant. They still need the managed parent below for dashboard
    # uploads and for resolving the ``workspace/...`` paths returned here, but
    # its logs and runner artifacts are not project deliverables. Limit only
    # this auto-created project type, leaving manually-created root grants alone.
    try:
        managed_root = project_log_dir(project_id).parent
        managed_workspace = managed_root / "workspace"
        managed_workspace_is_declared = str(project.get("name", "")).startswith(
            "Orchestration: "
        ) and any(
            isinstance(raw, str) and Path(raw) == managed_workspace
            for raw in project.get("root_dirs", [])
        )
    except (OSError, RuntimeError, ValueError):
        managed_root = None
        managed_workspace = None
        managed_workspace_is_declared = False

    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    seen_relative_paths: set[str] = set()
    for root in roots:
        # The managed parent below scans workspace/ with its prefix intact, so
        # omit the standalone workspace grant to avoid a duplicate unprefixed
        # entry for every task output.
        if managed_workspace_is_declared and root == managed_workspace and managed_root in roots:
            continue
        if not root.is_dir():
            continue
        scan_roots = (
            [managed_root / "workspace", managed_root / "uploads"]
            if managed_workspace_is_declared and root == managed_root and managed_root is not None
            else [root]
        )
        for scan_root in scan_roots:
            if not scan_root.is_dir():
                continue
            try:
                for path in scan_root.rglob("*"):
                    if len(rows) >= _MAX_FILE_COUNT:
                        break
                    if not path.is_file():
                        continue
                    # Skip runner scratch/cache dirs (Claude's node compile cache under
                    # .tmp/, .git objects, node_modules, __pycache__) so the Files tab
                    # shows real deliverables, not hundreds of hex-named cache files.
                    rel_parts = path.relative_to(scan_root).parts[:-1]
                    if any(
                        p.startswith(".") or p in ("node_modules", "__pycache__", "venv")
                        for p in rel_parts
                    ):
                        continue
                    resolved = path.resolve(strict=False)
                    if resolved in seen or not _is_within(resolved, root):
                        continue
                    relative_path = resolved.relative_to(root).as_posix()
                    # The file-read route accepts one root-relative path; preserve
                    # that contract by keeping the first matching path if grants
                    # contain two directories with the same relative filename.
                    if relative_path in seen_relative_paths:
                        continue
                    seen.add(resolved)
                    seen_relative_paths.add(relative_path)
                    rows.append(_file_row(resolved, root))
            except (OSError, RuntimeError):
                _LOG.debug("could not list files in project root %s", root, exc_info=True)
    rows.sort(key=lambda row: (str(row["path"]).lower(), str(row["name"]).lower()))
    return {"files": rows}


@router.get("/{project_id}/files/{file_path:path}")
def get_project_file(
    project_id: str, file_path: str, store: StoreDep, download: bool = False
) -> Any:
    """Read a safely-contained text file, or stream an image for browser preview."""
    project = _project_store(store).get_project(project_id)
    if project is None:
        fail(404, "not_found", "project not found", {"id": project_id})
    path = _resolve_project_file(file_path, _project_file_roots(project))
    kind = _file_kind(path)
    try:
        size = path.stat().st_size
    except OSError:
        fail(404, "not_found", "file not found", {"path": file_path})
    if kind == "image":
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=path.name if download else None)
    if kind != "text":
        return JSONResponse(
            status_code=415,
            content={
                "error": "binary_file",
                "message": "Preview is only available for text and image files.",
            },
        )
    if download:
        return FileResponse(path, media_type="text/plain", filename=path.name)
    if size > _MAX_TEXT_PREVIEW_BYTES:
        return JSONResponse(
            status_code=413,
            content={
                "error": "file_too_large",
                "message": "Text previews are limited to 1 MB. Open or download the file instead.",
            },
        )
    try:
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=415,
            content={"error": "binary_file", "message": "This file is not valid UTF-8 text."},
        )
    except OSError:
        fail(404, "not_found", "file not found", {"path": file_path})


@router.get("/{project_id}")
def get_project(project_id: str, store: StoreDep, policy_cfg: PolicyDep) -> dict[str, Any]:
    project = _project_store(store).get_project(project_id)
    if project is None:
        fail(404, "not_found", "project not found", {"id": project_id})
    return _with_resolved_policy(project, policy_cfg)


@router.get("/{project_id}/activity")
def get_project_activity(
    project_id: str,
    store: StoreDep,
    tasks_limit: int = Query(100, ge=1, le=500),
    runs_limit: int = Query(50, ge=1, le=200),
    events_limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """A project's live progress: tasks -> runs -> steps + a recent-activity tail.

    Aggregates the existing runs/steps/events tables (no new storage), keyed to
    this project transitively via task.project_id -- see
    :func:`omniagentos.projects.activity.build_project_activity`. As a
    best-effort side effect, also keeps this project's on-disk human log
    (``var/projects/<id>/logs/activity.log``) current, so an operator who wants
    to ``tail -f`` it directly never has to open the dashboard first.
    """
    project = _project_store(store).get_project(project_id)
    if project is None:
        fail(404, "not_found", "project not found", {"id": project_id})
    sqlite_store = cast(SqliteStore, store)
    result = build_project_activity(
        sqlite_store,
        project_id,
        tasks_limit=tasks_limit,
        runs_limit=runs_limit,
        events_limit=events_limit,
    )
    result["project_name"] = project.get("name")
    try:
        project_pending_activity(sqlite_store, project_id)
    except Exception:  # noqa: BLE001 -- project_pending_activity already never
        # raises; this guards a future regression from ever breaking a read.
        _LOG.debug("project activity log projection failed for %s", project_id, exc_info=True)
    return result


__all__ = ["router"]
