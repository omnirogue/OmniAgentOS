"""Fixtures and in-memory FakeCollabStore for collab unit tests."""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import (
    Agent,
    BoardTask,
    BoardTaskStatus,
    Channel,
    ChannelKind,
    Message,
    can_claim,
)
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso


class FakeCollabStore:
    """In-memory dict-based CollabStore stand-in for API unit tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.agents: dict[str, dict[str, Any]] = {}
        self.agents_by_name: dict[str, str] = {}
        self.board_tasks: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.channel_members: dict[str, set[str]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self._direct_pairs: dict[tuple[str, str], str] = {}

    def register_agent(self, a: Agent) -> None:
        with self._lock:
            if a.name in self.agents_by_name:
                existing_id = self.agents_by_name[a.name]
                a.id = existing_id
                row = self.agents[existing_id]
                row.update(
                    {
                        "lineage": a.lineage,
                        "model": a.model,
                        "expertise": list(a.expertise),
                        "trust_level": a.trust_level,
                        "status": str(a.status),
                        "updated_at": utc_now_iso(),
                    }
                )
                a.updated_at = row["updated_at"]
                return
            row = {
                "id": a.id,
                "name": a.name,
                "lineage": a.lineage,
                "model": a.model,
                "expertise": list(a.expertise),
                "trust_level": a.trust_level,
                "status": str(a.status),
                "created_at": a.created_at,
                "updated_at": a.updated_at,
            }
            self.agents[a.id] = row
            self.agents_by_name[a.name] = a.id

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.agents.values()]

    def set_agent_status(self, agent_id: str, status: str) -> None:
        with self._lock:
            row = self.agents.get(agent_id)
            if row is None:
                return
            row["status"] = status
            row["updated_at"] = utc_now_iso()

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.agents.get(agent_id)
            return dict(row) if row is not None else None

    def create_board_task(self, t: BoardTask) -> None:
        with self._lock:
            self.board_tasks[t.id] = {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "required_expertise": list(t.required_expertise),
                "discipline": t.discipline,
                "priority": t.priority,
                "status": str(t.status),
                "claimed_by": t.claimed_by,
                "claim_version": t.claim_version,
                "result_ref": t.result_ref,
                "archived_at": None,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }

    def get_board_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.board_tasks.get(task_id)
            return dict(row) if row is not None else None

    def list_board_tasks(
        self,
        status: str | None = None,
        expertise: list[str] | None = None,
        archived: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [dict(row) for row in self.board_tasks.values()]
        # Mirror the real store: exclude archived by default, or show only archived.
        if archived:
            tasks = [t for t in tasks if t.get("archived_at")]
        else:
            tasks = [t for t in tasks if not t.get("archived_at")]
        if status is not None:
            tasks = [t for t in tasks if t["status"] == status]
        # Mirror the real store's ORDER BY created_at DESC, id DESC. The id tiebreak
        # is load-bearing, not cosmetic: utc_now_iso has one-second resolution, so
        # cards created in the same second tie — and with `limit` a created_at-only
        # sort would hand this double a DIFFERENT page than production.
        tasks = sorted(
            tasks, key=lambda t: (t.get("created_at", ""), t.get("id", "")), reverse=True
        )
        # LIMIT is applied at the "SQL" layer, i.e. BEFORE the in-Python
        # expertise filter — same as the real store.
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be >= 0")
            tasks = tasks[:limit]
        if expertise is not None:
            tasks = [
                t for t in tasks if can_claim(list(t.get("required_expertise") or []), expertise)
            ]
        return tasks

    def open_tasks_for(self, agent_expertise: list[str]) -> list[dict[str, Any]]:
        return [
            t
            for t in self.list_board_tasks(status=BoardTaskStatus.OPEN.value)
            if can_claim(list(t.get("required_expertise") or []), agent_expertise)
        ]

    def claim_task(self, task_id: str, agent_id: str, expect_version: int) -> bool:
        with self._lock:
            row = self.board_tasks.get(task_id)
            if row is None:
                return False
            if (
                row["status"] != BoardTaskStatus.OPEN.value
                or int(row["claim_version"]) != expect_version
            ):
                return False
            row["status"] = BoardTaskStatus.CLAIMED.value
            row["claimed_by"] = agent_id
            row["claim_version"] = expect_version + 1
            row["updated_at"] = utc_now_iso()
            return True

    def update_board_task(
        self, task_id: str, fields: dict[str, Any], *, actor: str = "system"
    ) -> None:
        # `actor` mirrors the real CollabStore signature (audit-trail identity);
        # this fake keeps no event trail, so the value is accepted and unused.
        with self._lock:
            row = self.board_tasks.get(task_id)
            if row is None:
                return
            for key, value in fields.items():
                if key == "required_expertise":
                    row["required_expertise"] = list(value or [])
                else:
                    row[key] = value
            row["updated_at"] = utc_now_iso()

    def restore_archived_board_task(self, task_id: str) -> bool:
        with self._lock:
            row = self.board_tasks.get(task_id)
            if row is None or row.get("archived_at") is None:
                return False
            row["archived_at"] = None
            row["updated_at"] = utc_now_iso()
            return True

    def create_channel(self, c: Channel) -> None:
        with self._lock:
            members = list(c.members)
            if str(c.kind) == ChannelKind.DIRECT.value and len(members) == 2:
                pair = tuple(sorted(members))
                existing = self._direct_pairs.get(pair)  # type: ignore[arg-type]
                if existing is not None:
                    c.id = existing
                    self.channel_members.setdefault(existing, set()).update(members)
                    return
                self._direct_pairs[pair] = c.id  # type: ignore[index]
            self.channels[c.id] = {
                "id": c.id,
                "name": c.name,
                "kind": str(c.kind),
                "topic": c.topic,
                "created_at": c.created_at,
            }
            self.channel_members[c.id] = set(members)

    def add_member(self, channel_id: str, agent_id: str) -> None:
        with self._lock:
            self.channel_members.setdefault(channel_id, set()).add(agent_id)

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.channels.get(channel_id)
            if row is None:
                return None
            out = dict(row)
            out["members"] = sorted(self.channel_members.get(channel_id, set()))
            return out

    def list_channels(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            result: list[dict[str, Any]] = []
            for channel_id, row in self.channels.items():
                members = self.channel_members.get(channel_id, set())
                if agent_id is not None and agent_id not in members:
                    continue
                out = dict(row)
                out["members"] = sorted(members)
                result.append(out)
            return sorted(result, key=lambda c: c.get("created_at", ""))

    def post_message(self, m: Message) -> None:
        with self._lock:
            self.messages[m.id] = {
                "id": m.id,
                "channel_id": m.channel_id,
                "from_agent": m.from_agent,
                "to_agent": m.to_agent,
                "kind": str(m.kind),
                "body": m.body,
                "ref": m.ref,
                "ts": m.ts,
            }

    def list_messages(self, channel_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self.messages.values() if row["channel_id"] == channel_id]
        rows.sort(key=lambda r: (r.get("ts", ""), r.get("id", "")))
        return rows[:limit]

    def search_messages(self, q: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self.messages.values() if q in str(row.get("body", ""))]
        rows.sort(key=lambda r: (r.get("ts", ""), r.get("id", "")))
        return rows[:limit]


@pytest.fixture
def collab_store() -> CollabStore:
    """Real SQLite-backed CollabStore (in-memory)."""
    return CollabStore(":memory:")


@pytest.fixture
def fake_collab_store() -> FakeCollabStore:
    return FakeCollabStore()


@pytest.fixture
def collab_client(fake_collab_store: FakeCollabStore) -> httpx.AsyncClient:
    """ASGI client with FakeCollabStore (no filesystem DB)."""
    app.dependency_overrides[get_collab_store] = lambda: fake_collab_store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
