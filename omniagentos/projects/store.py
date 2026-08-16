"""Thread-safe SQLite data access for W2 projects and their permission grants.

Composed over an already-configured :class:`SqliteStore` (the same pattern as
:class:`omniagentos.steward.store.StewardStore`) so writes serialize against the
one connection lock rather than opening a second connection.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import ActionClass, new_id, utc_now_iso
from omniagentos.db.store import SqliteStore

_PROJECT_FIELDS = frozenset(
    {
        "id",
        "name",
        "root_dirs",
        "vault_subfolder",
        "budget_usd",
        "allowed_tools",
        "allowed_dirs",
        "parent_project_id",
        "created_at",
        "kind",
        "org_company_id",
        "org_product_id",
    }
)

# Portfolio redesign Phase A: durable vs orchestration scratch.
_PROJECT_KINDS = frozenset({"project", "scratch"})

# Task states that no longer contribute active work, used to roll a project's
# tree status up from its own tasks (see :meth:`ProjectStore.project_tree`).
_TERMINAL_TASK_STATES = frozenset({"completed", "cancelled", "failed"})


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("ProjectStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _checked(values: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = values.keys() - allowed
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    return values


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _project(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["root_dirs"] = _parse_json(out.pop("root_dirs_json", None), [])
    out["allowed_tools"] = _parse_json(out.pop("allowed_tools_json", None), [])
    out["allowed_dirs"] = _parse_json(out.pop("allowed_dirs_json", None), [])
    out["kind"] = str(out.get("kind") or "project")
    out["org_company_id"] = row.get("org_company_id")
    out["org_product_id"] = row.get("org_product_id")
    return out


def _rollup_status(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "empty"
    if any(str(task.get("state")) not in _TERMINAL_TASK_STATES for task in tasks):
        return "active"
    return "done"


def _grant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "action_class": row["action_class"],
        "requires_approval": bool(row["requires_approval"]),
        "always_human": bool(row["always_human"]),
    }


class ProjectError(ValueError):
    """Raised when a project payload cannot be persisted safely."""


class ProjectStore:
    """Project + per-project permission-grant DAL over a shared SqliteStore."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance. ``SqliteStore`` hands out one
        connection per thread and opens them with ``check_same_thread=False``,
        so a handle captured at construction time would bind this DAL to
        whichever thread built it and silently interleave its statements into
        another thread's transaction rather than raising.
        """
        return self._store._connection

    @_serialized
    def create_project(
        self, data: dict[str, Any], grants: Iterable[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        values = dict(_checked(dict(data), _PROJECT_FIELDS))
        name = str(values.get("name") or "").strip()
        if not name:
            raise ProjectError("project name is required")
        project_id = str(values.get("id") or new_id("proj"))
        now = values.get("created_at") or utc_now_iso()
        kind = str(values.get("kind") or "project").strip().lower()
        if kind not in _PROJECT_KINDS:
            raise ProjectError(f"unknown project kind {kind!r}")
        parent_project_id = values.get("parent_project_id")
        if parent_project_id is not None:
            parent_project_id = str(parent_project_id)
            if parent_project_id == project_id:
                raise ProjectError("a project cannot be its own parent")
            if self._get_project_unlocked(parent_project_id) is None:
                raise ProjectError(f"unknown parent project {parent_project_id!r}")
        # Pre-validate *every* grant before any write so a bad grant can never
        # leave a half-created project behind (F9 atomicity precondition).
        normalized_grants = [self._normalize_grant(grant) for grant in (grants or [])]
        parameters = (
            project_id,
            name,
            _json(values.get("root_dirs", [])),
            str(values.get("vault_subfolder", "") or ""),
            values.get("budget_usd"),
            _json(values.get("allowed_tools", [])),
            _json(values.get("allowed_dirs", [])),
            parent_project_id,
            now,
            kind,
            values.get("org_company_id"),
            values.get("org_product_id"),
        )
        # Single transaction: project row + all grants commit together or not at
        # all. self._store holds the lock (via @_serialized) so nothing else can
        # interleave writes on the shared connection.
        self._store._begin()
        try:
            # kind column added in migration 064; fall back if pre-migration DB.
            try:
                self._connection.execute(
                    "INSERT INTO projects (id, name, root_dirs_json, vault_subfolder, "
                    "budget_usd, allowed_tools_json, allowed_dirs_json, parent_project_id, "
                    "created_at, kind, org_company_id, org_product_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    parameters,
                )
            except sqlite3.OperationalError:
                try:
                    self._connection.execute(
                        "INSERT INTO projects (id, name, root_dirs_json, vault_subfolder, "
                        "budget_usd, allowed_tools_json, allowed_dirs_json, parent_project_id, "
                        "created_at, kind) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        parameters[:-2],
                    )
                except sqlite3.OperationalError:
                    self._connection.execute(
                        "INSERT INTO projects (id, name, root_dirs_json, vault_subfolder, "
                        "budget_usd, allowed_tools_json, allowed_dirs_json, parent_project_id, "
                        "created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        parameters[:-3],
                    )
            for action_class, requires_approval, always_human in normalized_grants:
                self._connection.execute(
                    "INSERT INTO project_permission_grants "
                    "(project_id, action_class, requires_approval, always_human) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, action_class) DO UPDATE SET "
                    "requires_approval = excluded.requires_approval, "
                    "always_human = excluded.always_human",
                    (project_id, action_class, requires_approval, always_human),
                )
            self._store._commit()
        except sqlite3.IntegrityError as exc:
            self._store._rollback()
            raise ProjectError(f"project name {name!r} already exists") from exc
        except BaseException:
            self._store._rollback()
            raise
        result = self._get_project_unlocked(project_id)
        assert result is not None
        return result

    @_serialized
    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM projects ORDER BY created_at DESC, id DESC"
        ).fetchall()
        # Embed grants so the list response matches the declared Project shape
        # (grants + resolved_policy are computed by the route). Without this the
        # dashboard crashes on `p.grants.length` (F5).
        projects: list[dict[str, Any]] = []
        for row in rows:
            project = _project(dict(row))
            project["grants"] = self._grants_unlocked(str(project["id"]))
            projects.append(project)
        return projects

    @_serialized
    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self._get_project_unlocked(project_id)

    @_serialized
    def set_grant(self, project_id: str, grant: dict[str, Any]) -> dict[str, Any]:
        if self._get_project_unlocked(project_id) is None:
            raise ProjectError(f"unknown project {project_id!r}")
        return self._set_grant_unlocked(project_id, grant)

    @_serialized
    def list_grants(self, project_id: str) -> list[dict[str, Any]]:
        return self._grants_unlocked(project_id)

    # --- hierarchy ---

    def create_subproject(
        self,
        parent_project_id: str,
        data: dict[str, Any],
        grants: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a project rooted under ``parent_project_id``.

        Thin wrapper over :meth:`create_project`; parent existence (and the
        self-parent guard) is enforced there, inside the same lock, so a bad
        parent never leaves a half-created row behind.
        """
        payload = dict(data)
        payload["parent_project_id"] = parent_project_id
        return self.create_project(payload, grants=grants)

    @_serialized
    def set_parent(self, project_id: str, parent_project_id: str | None) -> dict[str, Any]:
        """Re-parent ``project_id`` (or make it top-level when parent is None).

        Enforces the tree invariants the schema alone cannot: the project and any
        named parent must exist, a project cannot parent itself, and the new
        parent must not already sit *below* the project -- walking the ancestor
        chain rejects any move that would close a cycle.
        """
        if self._get_project_unlocked(project_id) is None:
            raise ProjectError(f"unknown project {project_id!r}")
        normalized = None if parent_project_id is None else str(parent_project_id)
        if normalized is not None:
            if normalized == project_id:
                raise ProjectError("a project cannot be its own parent")
            if self._get_project_unlocked(normalized) is None:
                raise ProjectError(f"unknown parent project {normalized!r}")
            # Reject a move that would create a cycle: the candidate parent must
            # not be reachable by climbing from itself back to `project_id`.
            ancestor: str | None = normalized
            seen: set[str] = set()
            while ancestor is not None:
                if ancestor == project_id:
                    raise ProjectError("re-parenting would create a cycle in the project tree")
                if ancestor in seen:  # defensive: pre-existing cycle, stop walking
                    break
                seen.add(ancestor)
                row = self._connection.execute(
                    "SELECT parent_project_id FROM projects WHERE id = ?", (ancestor,)
                ).fetchone()
                ancestor = None if row is None else row["parent_project_id"]
        self._store._write(
            "UPDATE projects SET parent_project_id = ? WHERE id = ?",
            (normalized, project_id),
        )
        result = self._get_project_unlocked(project_id)
        assert result is not None
        return result

    @_serialized
    def project_tree(self) -> list[dict[str, Any]]:
        """Return the full project forest, top-level projects first.

        Each node is ``{project, status, sub_projects, tasks}`` where ``tasks``
        are this project's own direct tasks (``{id, title, state}``) and
        ``status`` is a rollup of *those* tasks: ``active`` if any is still
        working, ``done`` if all are terminal, ``empty`` if there are none.
        The walk is bounded against a corrupt/self-referential row by tracking
        visited ids, so a stray cycle can never spin the response forever.
        """
        rows = self._connection.execute("SELECT * FROM projects").fetchall()
        children: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            project = _project(dict(row))
            project["grants"] = self._grants_unlocked(str(project["id"]))
            parent = project.get("parent_project_id")
            children.setdefault(parent, []).append(project)
        for bucket in children.values():
            bucket.sort(key=lambda p: (str(p.get("created_at") or ""), str(p["id"])))

        tasks_by_project: dict[str, list[dict[str, Any]]] = {}
        for task in self._connection.execute(
            "SELECT id, title, state, project_id FROM tasks "
            "WHERE project_id IS NOT NULL ORDER BY created_at ASC, id ASC"
        ).fetchall():
            tasks_by_project.setdefault(str(task["project_id"]), []).append(
                {"id": task["id"], "title": task["title"], "state": task["state"]}
            )

        def _node(project: dict[str, Any], seen: frozenset[str]) -> dict[str, Any]:
            project_id = str(project["id"])
            guard = seen | {project_id}
            tasks = tasks_by_project.get(project_id, [])
            sub_projects = [
                _node(child, guard)
                for child in children.get(project_id, [])
                if str(child["id"]) not in seen
            ]
            return {
                "project": project,
                "status": _rollup_status(tasks),
                "sub_projects": sub_projects,
                "tasks": tasks,
            }

        return [_node(project, frozenset()) for project in children.get(None, [])]

    @_serialized
    def ensure_org_folder_projects(self) -> list[dict[str, Any]]:
        """For every active company and product, ensure an org-linked project exists.

        Idempotent: if a project with (org_company_id, org_product_id) already exists,
        it is skipped. Otherwise, a new project is created, with:
          - id = "proj_" + product_id[4:]
          - name = f"{company_name} - {product_name}"
          - root_dirs = [f"{base_dir}/{company_slug}/{product_slug}"]
          - vault_subfolder = f"{company_slug}/{product_slug}"
          - org_company_id = company_id
          - org_product_id = product_id
        """
        import os

        try:
            rows = self._connection.execute(
                "SELECT c.id AS company_id, c.name AS company_name, c.slug AS company_slug, "
                "       p.id AS product_id, p.name AS product_name, p.slug AS product_slug "
                "FROM org_companies c "
                "JOIN org_products p ON p.company_id = c.id "
                "WHERE c.status = 'active' AND p.status = 'active'"
            ).fetchall()
        except sqlite3.OperationalError:
            # If org_companies / org_products table does not exist in the context
            return []

        raw_bases = os.environ.get("OMNIAGENTOS_PROJECT_BASES", "")
        base_dir = ""
        if raw_bases:
            for item in raw_bases.split(os.pathsep):
                if item.strip():
                    base_dir = os.path.abspath(os.path.expanduser(item.strip()))
                    break
        if not base_dir:
            var_dir = os.environ.get("OMNIAGENTOS_VAR_DIR", "")
            if var_dir:
                base_dir = os.path.abspath(os.path.expanduser(var_dir))
            else:
                base_dir = os.path.abspath(os.path.expanduser("~/OmniAgentOS"))

        created_projects = []
        for row in rows:
            company_id = str(row["company_id"])
            company_name = str(row["company_name"])
            company_slug = str(row["company_slug"])
            product_id = str(row["product_id"])
            product_name = str(row["product_name"])
            product_slug = str(row["product_slug"])

            existing = self._connection.execute(
                "SELECT id FROM projects WHERE org_company_id = ? AND org_product_id = ?",
                (company_id, product_id),
            ).fetchone()

            if existing is not None:
                continue

            suffix = product_id[4:] if product_id.startswith("prd_") else product_id
            project_id = f"proj_{suffix}"

            if self._get_project_unlocked(project_id) is not None:
                continue

            proj_dir = os.path.join(base_dir, company_slug, product_slug)
            os.makedirs(proj_dir, exist_ok=True)
            canonical_path = os.path.abspath(proj_dir)

            proj_name = f"{company_name} - {product_name}"
            name_check = self._connection.execute(
                "SELECT id FROM projects WHERE name = ?", (proj_name,)
            ).fetchone()
            if name_check is not None:
                proj_name = f"{company_name} - {product_name} ({product_slug})"

            proj_data = {
                "id": project_id,
                "name": proj_name,
                "root_dirs": [canonical_path],
                "vault_subfolder": f"{company_slug}/{product_slug}",
                "kind": "project",
                "org_company_id": company_id,
                "org_product_id": product_id,
            }

            self.create_project(proj_data)
            created = self._get_project_unlocked(project_id)
            assert created is not None  # just inserted above while holding the store lock
            created_projects.append(created)

        return created_projects

    # --- internals (assume the store lock is already held) ---

    def _get_project_unlocked(self, project_id: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        )
        if row is None:
            return None
        project = _project(row)
        project["grants"] = self._grants_unlocked(project_id)
        return project

    def _grants_unlocked(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM project_permission_grants WHERE project_id = ? "
            "ORDER BY action_class ASC",
            (project_id,),
        ).fetchall()
        return [_grant(dict(row)) for row in rows]

    @staticmethod
    def _normalize_grant(grant: dict[str, Any]) -> tuple[str, int, int]:
        """Validate a grant and return (action_class, requires_approval, always_human).

        Raises :class:`ProjectError` for an unknown action class so callers can
        reject the whole payload *before* touching the database.
        """
        try:
            action_class = ActionClass(str(grant["action_class"]))
        except (KeyError, ValueError) as exc:
            raise ProjectError(f"invalid action_class in grant: {grant!r}") from exc
        return (
            action_class.value,
            int(bool(grant.get("requires_approval", True))),
            int(bool(grant.get("always_human", False))),
        )

    def _set_grant_unlocked(self, project_id: str, grant: dict[str, Any]) -> dict[str, Any]:
        action_class, requires_approval, always_human = self._normalize_grant(grant)
        self._store._write(
            "INSERT INTO project_permission_grants "
            "(project_id, action_class, requires_approval, always_human) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, action_class) DO UPDATE SET "
            "requires_approval = excluded.requires_approval, "
            "always_human = excluded.always_human",
            (project_id, action_class, requires_approval, always_human),
        )
        return {
            "project_id": project_id,
            "action_class": action_class,
            "requires_approval": bool(requires_approval),
            "always_human": bool(always_human),
        }


__all__ = ["ProjectError", "ProjectStore"]
