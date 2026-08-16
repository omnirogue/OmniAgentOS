"""Thread-safe SQLite data access for chats and hidden companion board tasks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore


class ChatError(ValueError):
    """Raised when a chat or its companion task cannot be persisted safely."""


class UnknownProjectError(ChatError):
    """PATCH referenced a project that does not exist (route maps to 404)."""


class BoardTaskConflictError(ChatError):
    """POST tried to link a board card that already has a chat (route maps to 409)."""


class UnknownBoardTaskError(ChatError):
    """POST tried to link a board card that does not exist (route maps to 404)."""


class UnknownFolderError(ChatError):
    """Folder op referenced a name with no registry row and no chats (404)."""


# Folder color palette (088): named tokens only, never hex — the dashboard
# maps each token to a --ds-folder-<token> CSS variable per theme.
FOLDER_COLORS: tuple[str, ...] = (
    "gray",
    "red",
    "orange",
    "yellow",
    "green",
    "teal",
    "blue",
    "violet",
)

_DEFAULT_FOLDER_COLOR = "gray"


def _validate_folder_name(name: str) -> str:
    """Normalize + validate a folder name used as a registry key/path param."""
    name = (name or "").strip()
    if not name:
        raise ChatError("folder name is required")
    if "/" in name:
        raise ChatError("folder name cannot contain '/'")
    if len(name) > 100:
        raise ChatError("folder name too long (max 100 characters)")
    return name


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("ChatStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _iso_seconds_ago(seconds: int) -> str:
    """UTC timestamp ``seconds`` in the past, in the repo's ``…Z`` second-precision
    form so it compares lexicographically against stored timestamps in SQL."""
    moment = datetime.now(UTC) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _chat_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["meta"] = _parse_json(out.pop("meta_json", None), {})
    return out


_VALID_ORCH_MODES = frozenset({"auto", "solo", "fanout"})

_DEFAULT_ROUTING: dict[str, Any] = {
    "allow": [],
    "deny": [],
    "speed": None,
    "effort": None,
    "hint": None,
}


def normalize_routing(value: Any) -> dict[str, Any]:
    """Normalize a ``meta.routing`` blob to the pinned ChatDTO shape."""
    out = dict(_DEFAULT_ROUTING)
    if not isinstance(value, dict):
        return out
    for key in ("allow", "deny"):
        raw = value.get(key)
        if isinstance(raw, list):
            out[key] = [str(item) for item in raw]
    for key in ("speed", "effort", "hint"):
        raw = value.get(key)
        out[key] = str(raw) if raw is not None else None
    return out


def to_dto(
    chat: dict[str, Any],
    *,
    message_stats: dict[str, tuple[int, str | None]] | None = None,
    project_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Project a raw chat row dict to the pinned ChatDTO (§3.1, fixes P0-3).

    Flat fields are authoritative; ``meta`` is retained for forward-compat.
    ``message_stats`` maps chat_id → (message_count, last_message_at) from the
    grouped conversations join; ``project_names`` maps project_id → name.
    """
    raw_meta = chat.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    project_id = chat.get("project_id")
    orch_mode = meta.get("orch_mode")
    stats = (message_stats or {}).get(str(chat.get("id")), (0, None))
    return {
        "id": chat.get("id"),
        "title": chat.get("title"),
        "status": chat.get("status"),
        "project_id": project_id,
        "project_name": (project_names or {}).get(str(project_id)) if project_id else None,
        "board_task_id": chat.get("board_task_id"),
        "preferred_model": meta.get("preferred_model") or None,
        "orch_mode": orch_mode if orch_mode in _VALID_ORCH_MODES else "solo",
        "plan_mode": bool(meta.get("plan_mode")),
        "routing": normalize_routing(meta.get("routing")),
        "project_suggestion": meta.get("project_suggestion")
        if isinstance(meta.get("project_suggestion"), dict)
        else None,
        "message_count": int(stats[0]),
        "last_message_at": stats[1],
        "promoted_at": chat.get("promoted_at"),
        "created_at": chat.get("created_at"),
        "updated_at": chat.get("updated_at"),
        "meta": meta,
    }


def to_dto_list(
    chats: list[dict[str, Any]],
    *,
    message_stats: dict[str, tuple[int, str | None]] | None = None,
    project_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    return [
        to_dto(chat, message_stats=message_stats, project_names=project_names)
        for chat in chats
    ]


# Sentinel for update_chat's project_id: omitted (no change) vs explicit None
# (unassign) vs a value. Public: routes must pass THIS object for "omitted".
PROJECT_UNSET: Any = object()


class ChatStore:
    """Chats + hidden companion board tasks DAL over a shared SqliteStore."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._board_project_column: bool | None = None

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection live from the composed store."""
        return self._store._connection

    def _has_board_project_column(self) -> bool:
        """Whether migration 087's ``board_tasks.project_id`` column exists.

        Cached per instance; the I7 merge-order contract requires every
        project-mirror write to be a no-op when the column is missing.
        """
        if self._board_project_column is None:
            try:
                rows = self._connection.execute("PRAGMA table_info(board_tasks)").fetchall()
                self._board_project_column = any(row["name"] == "project_id" for row in rows)
            except Exception:  # noqa: BLE001
                self._board_project_column = False
        return self._board_project_column

    def _board_task_project_id(self, board_task_id: str) -> str | None:
        if not self._has_board_project_column():
            return None
        row = self._connection.execute(
            "SELECT project_id FROM board_tasks WHERE id = ?", (board_task_id,)
        ).fetchone()
        return None if row is None else row["project_id"]

    def _mirror_project_to_board_task(self, board_task_id: str, project_id: str | None) -> None:
        """Mirror the chat's project onto its companion board card (P0-7.4).

        No-op when the 087 column is missing (either merge order boots).
        Caller holds the store lock; runs inside the caller's transaction.
        """
        if not self._has_board_project_column():
            return
        self._connection.execute(
            "UPDATE board_tasks SET project_id = ? WHERE id = ?",
            (project_id, board_task_id),
        )

    @_serialized
    def create_chat(
        self,
        title: str,
        project_id: str | None = None,
        meta: dict[str, Any] | None = None,
        board_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a chat and automatically generate its hidden companion board task.

        Both creations run in a single transaction under the store connection lock.

        With ``board_task_id`` (§3.3 grow-a-chat), the chat links to that
        EXISTING board card instead of creating a companion: the card keeps its
        ``origin`` (stays a visible board card) and the chat inherits the
        card's ``project_id`` when none is passed. 404 unknown card;
        409 conflict when the card already has a chat (board_task_id UNIQUE).
        """
        title = title.strip()
        if not title:
            raise ChatError("chat title is required")

        chat_id = new_id("cht")
        now = utc_now_iso()
        meta_json = _json(meta or {})

        self._store._begin()
        try:
            if board_task_id is not None:
                row = self._connection.execute(
                    "SELECT id FROM board_tasks WHERE id = ?", (board_task_id,)
                ).fetchone()
                if row is None:
                    raise UnknownBoardTaskError(f"unknown board task {board_task_id!r}")
                existing = self._connection.execute(
                    "SELECT id FROM chats WHERE board_task_id = ? AND status != 'deleted'",
                    (board_task_id,),
                ).fetchone()
                if existing is not None:
                    raise BoardTaskConflictError(
                        f"board task {board_task_id!r} already has a chat"
                    )
                if project_id is None:
                    project_id = self._board_task_project_id(board_task_id)
            else:
                board_task_id = new_id("btk")
                columns = [
                    "id",
                    "title",
                    "description",
                    "required_expertise_json",
                    "discipline",
                    "priority",
                    "status",
                    "claimed_by",
                    "claim_version",
                    "result_ref",
                    "created_at",
                    "updated_at",
                    "origin",
                ]
                values: list[Any] = [
                    board_task_id,
                    f"Companion task for chat: {title}",
                    f"Automatically created companion board task for chat {chat_id}.",
                    "[]",
                    None,
                    "normal",
                    "open",
                    None,
                    0,
                    None,
                    now,
                    now,
                    "chat",
                ]
                if self._has_board_project_column():
                    # Companion inherits the chat's project (§3.8 write path).
                    columns.append("project_id")
                    values.append(project_id)
                placeholders = ", ".join("?" for _ in values)
                self._connection.execute(
                    f"INSERT INTO board_tasks ({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(values),
                )

            # 2. Create the chat itself
            self._connection.execute(
                "INSERT INTO chats "
                "(id, project_id, board_task_id, title, status, promoted_at, meta_json, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', NULL, ?, ?, ?)",
                (
                    chat_id,
                    project_id,
                    board_task_id,
                    title,
                    meta_json,
                    now,
                    now,
                ),
            )
            self._store._commit()
        except BaseException as exc:
            self._store._rollback()
            if isinstance(exc, ChatError):
                raise
            raise ChatError(f"failed to create chat {title!r}: {exc}") from exc

        result = self._get_chat_unlocked(chat_id)
        assert result is not None
        return result

    @_serialized
    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        return self._get_chat_unlocked(chat_id)

    @_serialized
    def list_chats(
        self,
        project_id: str | None = None,
        folder: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """List chat workspaces, excluding soft-deleted chats by default.

        ``folder`` filters by ``meta_json.folder``. Pass ``""`` to select chats
        with no folder. ``include_deleted`` surfaces ``status='deleted'`` rows
        needed only by admin/export paths.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if not include_deleted:
            clauses.append("status != 'deleted'")
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM chats {where} ORDER BY created_at DESC, id DESC"
        rows = self._connection.execute(query, tuple(params)).fetchall()
        chats = [_chat_dict(dict(row)) for row in rows]

        if folder is not None:
            if folder == "":
                chats = [c for c in chats if not c["meta"].get("folder")]
            else:
                chats = [c for c in chats if c["meta"].get("folder") == folder]

        return chats

    @_serialized
    def list_folders(self) -> list[str]:
        """Return distinct non-empty folder names from active chats' meta_json."""
        rows = self._connection.execute(
            "SELECT meta_json FROM chats WHERE status != 'deleted'"
        ).fetchall()
        seen: set[str] = set()
        for (raw,) in rows:
            meta = _parse_json(raw, {})
            folder = meta.get("folder")
            if folder and isinstance(folder, str) and folder.strip():
                seen.add(folder.strip())
        return sorted(seen)

    # -- Folder registry (088) -------------------------------------------------

    def _chat_folder_rows(self, include_deleted: bool = False) -> list[tuple[str, str, dict[str, Any]]]:
        """(chat_id, folder, meta) for every chat whose meta names a folder."""
        where = "" if include_deleted else "WHERE status != 'deleted'"
        rows = self._connection.execute(
            f"SELECT id, meta_json FROM chats {where}"
        ).fetchall()
        out: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            meta = _parse_json(row["meta_json"], {})
            folder = meta.get("folder")
            if isinstance(folder, str) and folder.strip():
                out.append((str(row["id"]), folder.strip(), meta))
        return out

    def _folder_registry_rows(self) -> dict[str, dict[str, Any]]:
        """Raw chat_folders rows keyed by name; {} when the table is absent."""
        try:
            rows = self._connection.execute(
                "SELECT name, color, position FROM chat_folders"
            ).fetchall()
        except Exception:  # noqa: BLE001 -- pre-088 stores have no registry
            return {}
        return {
            str(row["name"]): {
                "color": str(row["color"]) if row["color"] in FOLDER_COLORS else _DEFAULT_FOLDER_COLOR,
                "position": row["position"],
            }
            for row in rows
        }

    def _folder_entry_unlocked(self, name: str) -> dict[str, Any]:
        """One folder projected as {name, color, position, chat_count}."""
        registry = self._folder_registry_rows().get(name)
        count = sum(1 for _, folder, _ in self._chat_folder_rows() if folder == name)
        return {
            "name": name,
            "color": (registry or {}).get("color") or _DEFAULT_FOLDER_COLOR,
            "position": (registry or {}).get("position"),
            "chat_count": count,
        }

    @_serialized
    def list_folder_registry(self) -> list[dict[str, Any]]:
        """Folder registry unioned with folders discovered on live chats.

        Returns ``[{name, color, position, chat_count}]`` ordered by position
        (NULLs last), then case-insensitive name. Folders that exist only as
        free text on chats appear with the default color and NULL position;
        registered folders appear even when no chat is filed in them yet.
        """
        registry = self._folder_registry_rows()
        counts: dict[str, int] = {}
        for _, folder, _ in self._chat_folder_rows():
            counts[folder] = counts.get(folder, 0) + 1
        entries = []
        for name in set(registry) | set(counts):
            reg = registry.get(name)
            entries.append(
                {
                    "name": name,
                    "color": (reg or {}).get("color") or _DEFAULT_FOLDER_COLOR,
                    "position": (reg or {}).get("position"),
                    "chat_count": counts.get(name, 0),
                }
            )
        entries.sort(
            key=lambda f: (
                f["position"] is None,
                f["position"] if f["position"] is not None else 0,
                str(f["name"]).lower(),
            )
        )
        return entries

    @_serialized
    def set_folder_color(self, name: str, color: str) -> dict[str, Any]:
        """Set (or register) a folder's color token.

        Upsert: an unknown name creates the registry row at the end of the
        manual order — this is also how the UI creates an empty folder before
        any chat is filed into it. Returns the folder entry.
        """
        name = _validate_folder_name(name)
        if color not in FOLDER_COLORS:
            raise ChatError(f"color must be one of {list(FOLDER_COLORS)}")
        now = utc_now_iso()
        self._store._begin()
        try:
            row = self._connection.execute(
                "SELECT name FROM chat_folders WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                max_row = self._connection.execute(
                    "SELECT MAX(position) AS m FROM chat_folders"
                ).fetchone()
                next_pos = (max_row["m"] + 1) if max_row and max_row["m"] is not None else 0
                self._connection.execute(
                    "INSERT INTO chat_folders (name, color, position, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, color, next_pos, now, now),
                )
            else:
                self._connection.execute(
                    "UPDATE chat_folders SET color = ?, updated_at = ? WHERE name = ?",
                    (color, now, name),
                )
            self._store._commit()
        except BaseException as exc:
            self._store._rollback()
            if isinstance(exc, ChatError):
                raise
            raise ChatError(f"failed to set color for folder {name!r}: {exc}") from exc
        return self._folder_entry_unlocked(name)

    @_serialized
    def rename_folder(self, name: str, new_name: str) -> dict[str, Any]:
        """Rename a folder: move every chat to the new name, re-key the registry.

        Chats in ANY status (deleted included) are rewritten so an undeleted
        chat can never resurrect the old name. Renaming onto an existing
        folder MERGES into it — the target keeps its color and position.
        Unknown source (no registry row, no chats) raises UnknownFolderError.
        """
        name = (name or "").strip()
        if not name:
            raise ChatError("folder name is required")
        new_name = _validate_folder_name(new_name)
        now = utc_now_iso()
        self._store._begin()
        try:
            src_row = self._connection.execute(
                "SELECT name FROM chat_folders WHERE name = ?", (name,)
            ).fetchone()
            members = [
                (chat_id, meta)
                for chat_id, folder, meta in self._chat_folder_rows(include_deleted=True)
                if folder == name
            ]
            if src_row is None and not members:
                raise UnknownFolderError(f"unknown folder {name!r}")
            if new_name != name:
                for chat_id, meta in members:
                    meta["folder"] = new_name
                    self._connection.execute(
                        "UPDATE chats SET meta_json = ?, updated_at = ? WHERE id = ?",
                        (_json(meta), now, chat_id),
                    )
                if src_row is not None:
                    dst_row = self._connection.execute(
                        "SELECT name FROM chat_folders WHERE name = ?", (new_name,)
                    ).fetchone()
                    if dst_row is None:
                        self._connection.execute(
                            "UPDATE chat_folders SET name = ?, updated_at = ? WHERE name = ?",
                            (new_name, now, name),
                        )
                    else:
                        self._connection.execute(
                            "DELETE FROM chat_folders WHERE name = ?", (name,)
                        )
            self._store._commit()
        except BaseException as exc:
            self._store._rollback()
            if isinstance(exc, ChatError):
                raise
            raise ChatError(f"failed to rename folder {name!r}: {exc}") from exc
        return self._folder_entry_unlocked(new_name)

    @_serialized
    def delete_folder(self, name: str) -> int:
        """Delete a folder: registry row removed, member chats fall back to
        the Inbox (``meta_json.folder`` cleared). Returns chats moved.

        Like rename, deleted chats are cleaned too so undelete cannot bring
        the folder back. Unknown name raises UnknownFolderError.
        """
        name = (name or "").strip()
        if not name:
            raise ChatError("folder name is required")
        now = utc_now_iso()
        self._store._begin()
        try:
            src_row = self._connection.execute(
                "SELECT name FROM chat_folders WHERE name = ?", (name,)
            ).fetchone()
            members = [
                (chat_id, meta)
                for chat_id, folder, meta in self._chat_folder_rows(include_deleted=True)
                if folder == name
            ]
            if src_row is None and not members:
                raise UnknownFolderError(f"unknown folder {name!r}")
            for chat_id, meta in members:
                meta.pop("folder", None)
                self._connection.execute(
                    "UPDATE chats SET meta_json = ?, updated_at = ? WHERE id = ?",
                    (_json(meta), now, chat_id),
                )
            if src_row is not None:
                self._connection.execute(
                    "DELETE FROM chat_folders WHERE name = ?", (name,)
                )
            self._store._commit()
        except BaseException as exc:
            self._store._rollback()
            if isinstance(exc, ChatError):
                raise
            raise ChatError(f"failed to delete folder {name!r}: {exc}") from exc
        return len(members)

    # -- ChatDTO projection (§3.1) ---------------------------------------------

    def _message_stats(self, chat_ids: list[str]) -> dict[str, tuple[int, str | None]]:
        """ONE grouped conversations join: chat_id → (message_count, last_message_at)."""
        if not chat_ids:
            return {}
        placeholders = ",".join("?" for _ in chat_ids)
        try:
            rows = self._connection.execute(
                "SELECT scope_id, COUNT(*) AS n, MAX(created_at) AS last_at "
                f"FROM conversations WHERE scope_type = 'chat' AND scope_id IN ({placeholders}) "
                "GROUP BY scope_id",
                tuple(chat_ids),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- conversations table absent in minimal stores
            return {}
        return {str(row["scope_id"]): (int(row["n"]), row["last_at"]) for row in rows}

    def _project_names(self) -> dict[str, str]:
        """ONE prefetched projects map per list call: project_id → name."""
        try:
            rows = self._connection.execute("SELECT id, name FROM projects").fetchall()
        except Exception:  # noqa: BLE001 -- projects table absent in minimal stores
            return {}
        return {str(row["id"]): str(row["name"]) for row in rows}

    @_serialized
    def get_chat_dto(self, chat_id: str) -> dict[str, Any] | None:
        """Fetch one chat projected as the pinned ChatDTO (§3.1)."""
        chat = self._get_chat_unlocked(chat_id)
        if chat is None:
            return None
        return to_dto(
            chat,
            message_stats=self._message_stats([chat_id]),
            project_names=self._project_names(),
        )

    @_serialized
    def list_chat_dtos(
        self,
        project_id: str | None = None,
        folder: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """List chats projected as ChatDTOs with message stats + project names."""
        chats = self.list_chats(
            project_id=project_id, folder=folder, include_deleted=include_deleted
        )
        stats = self._message_stats([str(chat["id"]) for chat in chats])
        return to_dto_list(chats, message_stats=stats, project_names=self._project_names())

    @_serialized
    def update_chat(
        self,
        chat_id: str,
        title: str | None = None,
        status: str | None = None,
        meta: dict[str, Any] | None = None,
        project_id: Any = PROJECT_UNSET,
    ) -> dict[str, Any]:
        """Update fields of a chat.

        ``project_id`` is tri-state: omitted = no change, ``None`` = unassign,
        a value = assign (validated against ``projects``; unknown ids raise
        :class:`UnknownProjectError`). Setting it mirrors onto the companion
        board task's ``project_id`` column in the same transaction (P0-7.4).
        """
        chat = self._get_chat_unlocked(chat_id)
        if chat is None:
            raise ChatError(f"unknown chat {chat_id!r}")

        now = utc_now_iso()
        updates = []
        params = []

        if title is not None:
            title = title.strip()
            if not title:
                raise ChatError("chat title cannot be empty")
            updates.append("title = ?")
            params.append(title)
        if status is not None:
            status = status.strip()
            updates.append("status = ?")
            params.append(status)
            if status == "promoted" and chat.get("promoted_at") is None:
                updates.append("promoted_at = ?")
                params.append(now)
        if meta is not None:
            merged = {**chat.get("meta", {}), **meta}
            updates.append("meta_json = ?")
            params.append(_json(merged))
        if project_id is not PROJECT_UNSET:
            if project_id is not None:
                row = self._connection.execute(
                    "SELECT id FROM projects WHERE id = ?", (str(project_id),)
                ).fetchone()
                if row is None:
                    raise UnknownProjectError(f"unknown project {project_id!r}")
            updates.append("project_id = ?")
            params.append(project_id)

        if updates:
            updates.append("updated_at = ?")
            params.append(now)
            query = f"UPDATE chats SET {', '.join(updates)} WHERE id = ?"
            params.append(chat_id)
            self._store._begin()
            try:
                self._connection.execute(query, tuple(params))
                if project_id is not PROJECT_UNSET:
                    self._mirror_project_to_board_task(
                        str(chat["board_task_id"]), project_id
                    )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise

        result = self._get_chat_unlocked(chat_id)
        assert result is not None
        return result

    @_serialized
    def soft_delete_chat(self, chat_id: str) -> dict[str, Any]:
        """Mark a chat as deleted (soft delete). Excluded from list queries."""
        chat = self._get_chat_unlocked(chat_id)
        if chat is None:
            raise ChatError(f"unknown chat {chat_id!r}")
        now = utc_now_iso()
        self._store._begin()
        try:
            self._connection.execute(
                "UPDATE chats SET status = 'deleted', updated_at = ? WHERE id = ?",
                (now, chat_id),
            )
            board_task_id = chat.get("board_task_id")
            if board_task_id:
                self._connection.execute(
                    "UPDATE board_tasks SET status = 'cancelled', updated_at = ? "
                    "WHERE id = ? AND status IN ('open', 'pending', 'in_progress')",
                    (now, board_task_id),
                )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        result = self._get_chat_unlocked(chat_id)
        assert result is not None
        return result

    @_serialized
    def promote_chat(self, chat_id: str) -> dict[str, Any]:
        """Promote/close a chat workspace, stamping promoted_at."""
        return self.update_chat(chat_id, status="promoted")

    @_serialized
    def find_promoted_task_by_item_key(self, chat_id: str, item_key: str) -> dict[str, Any] | None:
        """Return the board card previously promoted for this chat + item key.

        Matched with ``json_extract`` EQUALITY on both provenance fields. A LIKE
        prefix match over the raw ``org_json`` text was two bugs at once: it made
        ``index":1`` match ``index":10``, and an unescaped LIKE metacharacter in a
        chat id (``%``) matched every promoted card in the table.

        ``json_valid`` guards the extract: SQLite RAISES ``malformed JSON`` rather
        than returning NULL, so one unparseable ``org_json`` row anywhere in
        ``board_tasks`` would otherwise fail every promotion in the workspace.

        The guard FAILS OPEN, deliberately: a card whose OWN ``org_json`` has been
        corrupted becomes invisible to this lookup, so a later promote of that item
        creates a fresh card instead of refusing. A corrupted card cannot be proven
        to be the right card, and a promotion that silently does nothing is the
        failure this whole lane exists to remove — a duplicate card is visible and
        an operator can archive it. Asserted by
        ``test_a_corrupted_promoted_card_fails_open_to_a_new_card``.
        """
        readable = "CASE WHEN json_valid(org_json) THEN org_json ELSE '{}' END"
        row = self._connection.execute(
            "SELECT * FROM board_tasks "
            f"WHERE json_extract({readable}, '$.chat_id') = ? "
            f"AND json_extract({readable}, '$.promoted_item_key') = ? "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            (chat_id, item_key),
        ).fetchone()
        return None if row is None else dict(row)

    @_serialized
    def reserve_promoted_card(
        self,
        *,
        chat_id: str,
        parent_task_id: str,
        item_key: str,
        title: str,
        description: str,
        project_id: str | None = None,
        source_message_index: int | None = None,
        working_dir: str | None = None,
    ) -> str | None:
        """Create the promoted card AND take its item claim in ONE statement.

        ``item_key`` is the durable identity of the promoted item: a digest of the
        chat id and the SOURCE MESSAGE's content (see
        ``api.routes.chats._promoted_item_key``). It survives LLM re-wording — the
        model's prose changes, the operator's message does not — and it survives
        the thread GROWING or an earlier message being deleted, neither of which a
        positional index survives. EDITING the source message deliberately yields a
        NEW key: a re-promote after an edit creates a new card, because an edited
        ask is a different ask. ``source_message_index`` is retained as a DISPLAY
        HINT only (it may be ``None`` when the extractor could not name a valid
        source); nothing keys off it.

        THE CLAIM COMES BEFORE ANY EXECUTOR EXISTS. ``INSERT … SELECT … WHERE NOT
        EXISTS(another card in this chat already holding this item_key)`` is a
        single statement, so two racing promotes — in one process or in two —
        cannot both insert. Returns the new card id to the winner and ``None`` to
        the loser, who has created NOTHING: no card to retire, and crucially no
        ``dispatch_spec`` call, so no second run or session is ever spawned for
        one ask. The caller then hands this id to ``dispatch_spec`` as
        ``board_task_id``, which attaches the executor to the card already owned.

        Claiming AFTER dispatch (the previous shape) left the loser holding a live
        session that outlived the card it cancelled.
        """
        task_id = new_id("btk")
        now = utc_now_iso()
        org_json: dict[str, Any] = {
            "chat_id": chat_id,
            "parent_task_id": parent_task_id,
            "promoted": True,
            "promoted_item_key": item_key,
            "promoted_source_message_index": source_message_index,
        }
        if working_dir is not None:
            org_json["working_dir"] = working_dir

        columns = [
            "id",
            "title",
            "description",
            "required_expertise_json",
            "priority",
            "status",
            "claim_version",
            "created_at",
            "updated_at",
            "org_json",
        ]
        values: list[Any] = [
            task_id,
            title,
            description,
            "[]",
            "normal",
            # PENDING, not OPEN: the board hands out `open` cards to any agent whose
            # expertise matches, and a reservation has no executor yet. dispatch_spec's
            # reuse path flips it to `open` when it attaches one, so the claimable
            # window never precedes the work.
            "pending",
            0,
            now,
            now,
            _json(org_json),
        ]
        if self._has_board_project_column():
            columns.append("project_id")
            values.append(project_id)

        readable = "CASE WHEN json_valid(other.org_json) THEN other.org_json ELSE '{}' END"
        placeholders = ", ".join("?" for _ in values)
        inserted = self._store._write_count(
            f"INSERT INTO board_tasks ({', '.join(columns)}) "
            f"SELECT {placeholders} WHERE NOT EXISTS ("
            "SELECT 1 FROM board_tasks other "
            f"WHERE json_extract({readable}, '$.chat_id') = ? "
            f"AND json_extract({readable}, '$.promoted_item_key') = ?)",
            (*values, chat_id, item_key),
        )
        return task_id if inserted else None

    @_serialized
    def release_promotion_reservation(self, task_id: str) -> bool:
        """Give an item's claim back after a dispatch that never got off the ground.

        Only ever applied to a card this module reserved moments earlier whose
        dispatch attached NO executor (the caller proves that; see
        ``api.routes.chats._settle_failed_dispatch``). The row is cancelled,
        archived and stripped of ``promoted_item_key`` so the board does not show
        it and the operator can promote that ask again.

        Refuses on a card another agent has claimed: between the reservation and
        this call the card may have become ``open`` and been claimed, and
        cancelling somebody else's claimed work is not this module's business.
        Returns False in that case, leaving the item held.
        """
        row = self._connection.execute(
            "SELECT org_json, claimed_by FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        if row["claimed_by"]:
            return False
        org_json = _parse_json(row["org_json"], {})
        if not isinstance(org_json, dict):
            org_json = {}
        org_json.pop("promoted_item_key", None)
        org_json["promoted"] = False
        org_json["promotion_released"] = True
        now = utc_now_iso()
        released = self._store._write_count(
            "UPDATE board_tasks SET org_json = ?, status = 'cancelled', "
            "archived_at = COALESCE(archived_at, ?), updated_at = ? "
            "WHERE id = ? AND claimed_by IS NULL",
            (_json(org_json), now, now, task_id),
        )
        return released > 0

    @_serialized
    def adopt_stranded_reservation(self, task_id: str, *, stale_after_s: int = 600) -> bool:
        """Compare-and-swap the right to re-dispatch a reservation nothing attached to.

        A crash between ``reserve_promoted_card`` and its ``dispatch_spec`` leaves a
        ``pending`` card holding the item with no run and no session: every later
        promote would find it and answer "already promoted", stranding the ask
        forever. This hands exactly ONE caller the right to dispatch onto that card
        again — the timestamp it writes is itself the lock, so two concurrent
        retries (in one process or two) cannot both re-dispatch, and a retry that
        crashes in turn is adoptable again once ``stale_after_s`` has passed.
        """
        readable = "CASE WHEN json_valid(org_json) THEN org_json ELSE '{}' END"
        cutoff = _iso_seconds_ago(stale_after_s)
        now = utc_now_iso()
        adopted = self._store._write_count(
            f"UPDATE board_tasks SET org_json = json_set({readable}, "
            "'$.promotion_redispatch_at', ?), updated_at = ? "
            "WHERE id = ? AND status = 'pending' AND run_id IS NULL "
            "AND result_ref IS NULL AND claimed_by IS NULL AND archived_at IS NULL "
            f"AND (json_extract({readable}, '$.promotion_redispatch_at') IS NULL "
            f"OR json_extract({readable}, '$.promotion_redispatch_at') < ?)",
            (now, now, task_id, cutoff),
        )
        return adopted > 0

    @_serialized
    def find_chat_by_board_task_id(self, board_task_id: str) -> dict[str, Any] | None:
        """Look up a chat by its companion board task id (for reply write-back)."""
        row = self._connection.execute(
            "SELECT * FROM chats WHERE board_task_id = ? AND status != 'deleted'",
            (board_task_id,),
        ).fetchone()
        return None if row is None else _chat_dict(dict(row))

    @_serialized
    def create_spawn_task(
        self,
        chat_id: str,
        title: str,
        description: str,
    ) -> str:
        """Create a board task parented to a chat's companion task.

        Spawned tasks carry ``origin='chat'`` but are real work, not companion
        plumbing: the live board excludes only cards pointed at by a chat
        (P0-9), so spawned sub-tasks ARE visible — in their project (they
        inherit the chat's ``project_id`` here, per §3.8). The parent
        relationship is recorded in the task's ``org_json`` as
        ``{"parent_task_id": <companion_id>}``. Returns the new task id.
        """
        chat = self._get_chat_unlocked(chat_id)
        if chat is None:
            raise ChatError(f"unknown chat {chat_id!r}")

        task_id = new_id("btk")
        now = utc_now_iso()
        companion_id = chat["board_task_id"]
        org_json = _json({"parent_task_id": companion_id, "chat_id": chat_id, "spawned": True})

        columns = [
            "id",
            "title",
            "description",
            "required_expertise_json",
            "discipline",
            "priority",
            "status",
            "claimed_by",
            "claim_version",
            "result_ref",
            "created_at",
            "updated_at",
            "origin",
            "org_json",
        ]
        values: list[Any] = [
            task_id,
            title,
            description,
            "[]",
            None,
            "normal",
            "open",
            None,
            0,
            None,
            now,
            now,
            "chat",
            org_json,
        ]
        if self._has_board_project_column():
            # Spawned sub-tasks inherit the chat's project (P0-9).
            columns.append("project_id")
            values.append(chat.get("project_id"))
        placeholders = ", ".join("?" for _ in values)

        self._store._begin()
        try:
            self._connection.execute(
                f"INSERT INTO board_tasks ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )
            self._store._commit()
        except BaseException as exc:
            self._store._rollback()
            raise ChatError(f"failed to create spawn task: {exc}") from exc
        return task_id

    def _get_chat_unlocked(self, chat_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return None if row is None else _chat_dict(dict(row))


__all__ = [
    "FOLDER_COLORS",
    "BoardTaskConflictError",
    "ChatError",
    "ChatStore",
    "UnknownBoardTaskError",
    "UnknownFolderError",
    "UnknownProjectError",
    "normalize_routing",
    "to_dto",
    "to_dto_list",
]
