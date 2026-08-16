"""Chat → project auto-classify suggestion (§3.5).

Fires once per chat (guarded by ``meta.classified_at``) on the first user
message, or on demand via ``POST /api/chats/{id}/classify``. Uses the budgeted
:class:`~omniagentos.llm.client.ShortCallClient` with
``purpose="chat_project_classify"``; input is the project tree's
``(id, name, description)`` triples + the chat's first user message.

HONESTY RULES (pinned): the result is a SUGGESTION stored at
``meta.project_suggestion`` — this module never writes ``chats.project_id``
(the regression test asserts the column is unchanged after classify).
Confidence < 0.5 is discarded server-side (``classified_at`` is still stamped
so it fires exactly once). Any failure: debug log, store nothing, the
suggestion bar simply never appears.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omniagentos.chats.store import ChatStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore

_LOG = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.5

_PROMPT = """You are routing a new chat conversation to the best-fit project.

Projects (id, name, description):
{projects}

First user message of the chat (title: {title}):
\"\"\"{message}\"\"\"

Return ONE JSON object: {{"project_id": <id or null>, "confidence": <0.0-1.0>, "rationale": "<one sentence>"}}.
Use null project_id when no project is a reasonable fit. Be conservative: only
confidence >= 0.5 will be shown to the user as a suggestion."""


def _first_user_message(store: SqliteStore, chat_id: str) -> str | None:
    try:
        row = store._connection.execute(
            "SELECT content FROM conversations "
            "WHERE scope_type = 'chat' AND scope_id = ? AND role = 'user' "
            "ORDER BY seq ASC LIMIT 1",
            (chat_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    content = str(row["content"] or "").strip()
    return content or None


def _project_candidates(store: SqliteStore) -> list[dict[str, Any]]:
    # ``projects`` has no description column — the closest prose signal is its
    # workspace roots, so the first root dir rides as the description.
    try:
        rows = store._connection.execute(
            "SELECT id, name, root_dirs_json FROM projects ORDER BY name"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        roots = row["root_dirs_json"]
        description = ""
        try:
            parsed = json.loads(roots) if isinstance(roots, str) else roots
            if isinstance(parsed, list) and parsed:
                description = str(parsed[0])
        except (TypeError, json.JSONDecodeError):
            description = ""
        out.append(
            {"id": str(row["id"]), "name": str(row["name"]), "description": description}
        )
    return out


def classify_chat_project(
    store: SqliteStore,
    chat_id: str,
    *,
    force: bool = False,
    client: Any | None = None,
) -> dict[str, Any] | None:
    """Classify one chat to a suggested project; store the suggestion.

    Returns the stored suggestion ``{"project_id", "name", "confidence",
    "rationale"}``, or ``None`` when nothing was stored (below threshold, no
    projects, no message, or any failure). NEVER writes ``chats.project_id``.
    """
    chat_store = ChatStore(store)
    chat = chat_store.get_chat(chat_id)
    if chat is None:
        return None
    meta = chat.get("meta") or {}
    if meta.get("classified_at") and not force:
        cached_suggestion = meta.get("project_suggestion")
        return cached_suggestion if isinstance(cached_suggestion, dict) else None

    message = _first_user_message(store, chat_id)
    projects = _project_candidates(store)
    if not message or not projects:
        return None

    if client is None:
        try:
            from omniagentos.llm.client import ShortCallClient

            client = ShortCallClient()
        except Exception:  # noqa: BLE001
            _LOG.debug("classify: LLM client unavailable", exc_info=True)
            return None

    project_lines = "\n".join(
        f"- {p['id']}: {p['name']} — {p['description'] or '(no description)'}"
        for p in projects
    )
    prompt = _PROMPT.format(
        projects=project_lines,
        title=str(chat.get("title") or ""),
        message=message[:2000],
    )
    try:
        raw = client.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=256,
            purpose="chat_project_classify",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        _LOG.debug("classify: LLM call failed for %s", chat_id, exc_info=True)
        return None

    if not isinstance(parsed, dict):
        return None
    project_id = parsed.get("project_id")
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    rationale = str(parsed.get("rationale") or "")

    by_id = {p["id"]: p for p in projects}
    update: dict[str, Any] = {"classified_at": utc_now_iso()}
    suggestion: dict[str, Any] | None = None
    if (
        isinstance(project_id, str)
        and project_id in by_id
        and confidence >= CONFIDENCE_THRESHOLD
    ):
        suggestion = {
            "project_id": project_id,
            "name": by_id[project_id]["name"],
            "confidence": round(confidence, 3),
            "rationale": rationale[:500],
        }
        update["project_suggestion"] = suggestion
    try:
        # meta-only write — this module has no access to the project-update
        # path; chats.project_id can never change from here.
        chat_store.update_chat(chat_id, meta=update)
    except Exception:  # noqa: BLE001
        _LOG.debug("classify: could not store suggestion for %s", chat_id, exc_info=True)
        return None
    return suggestion


__all__ = ["CONFIDENCE_THRESHOLD", "classify_chat_project"]
