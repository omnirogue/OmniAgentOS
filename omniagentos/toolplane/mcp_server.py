"""In-process MCP SERVER exposing skills + memory through exposure policy.

No socket, no side transport. Gated by ``OMNIAGENTOS_MCP_BRIDGE_MODE``.
Memory writes are separately gated and default OFF even when the bridge is on.

Authorization is deny-by-default: empty grants yield no tools on list or call.
Both listing and invocation route through :func:`compute_exposure` and only
serve tools that are both in the server inventory and explicitly granted.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniagentos.toolplane.exposure import ExposureContext, compute_exposure
from omniagentos.toolplane.mcp_mode import mcp_bridge_enabled, mcp_bridge_mode
from omniagentos.toolplane.mcp_types import McpCallResult, McpDenial, McpToolDescriptor

__all__ = [
    "MEMORY_WRITE_ENV",
    "McpServer",
    "memory_write_enabled",
]

MEMORY_WRITE_ENV = "OMNIAGENTOS_MCP_MEMORY_WRITE"
_MEMORY_WRITE_DEFAULT = "off"


@dataclass(frozen=True, slots=True)
class _ServerExposureEntry:
    """Minimal connector-style catalog entry consumed by ``compute_exposure``."""

    id: str
    risk: str
    source: str = "connector"
    namespace: str = "mcp-server"
    compact_hint: str = ""
    description: str = ""
    parameter_names: tuple[str, ...] = ()
    input_examples: tuple[str, ...] = ()


def memory_write_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get(MEMORY_WRITE_ENV, _MEMORY_WRITE_DEFAULT)).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enforce",
    }


_SERVER_TOOLS: tuple[McpToolDescriptor, ...] = (
    McpToolDescriptor(
        name="skills_list",
        description="List skills from the existing skills registry.",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        risk="low",
        source="skills",
    ),
    McpToolDescriptor(
        name="skills_get",
        description="Fetch one skill by id or slug.",
        input_schema={
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
        risk="low",
        source="skills",
    ),
    McpToolDescriptor(
        name="memory_list",
        description="List recent memory turns for a scope (read-first).",
        input_schema={
            "type": "object",
            "properties": {
                "scope_type": {"type": "string"},
                "scope_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["scope_type", "scope_id"],
        },
        risk="low",
        source="memory",
    ),
    McpToolDescriptor(
        name="memory_read",
        description="Read a rolling summary for a scope when available.",
        input_schema={
            "type": "object",
            "properties": {
                "scope_type": {"type": "string"},
                "scope_id": {"type": "string"},
            },
            "required": ["scope_type", "scope_id"],
        },
        risk="low",
        source="memory",
    ),
    McpToolDescriptor(
        name="memory_search",
        description="Search recent turns by substring (local, no network).",
        input_schema={
            "type": "object",
            "properties": {
                "scope_type": {"type": "string"},
                "scope_id": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["scope_type", "scope_id", "query"],
        },
        risk="low",
        source="memory",
    ),
    McpToolDescriptor(
        name="memory_write",
        description="Append a memory turn (flag-gated, default OFF).",
        input_schema={
            "type": "object",
            "properties": {
                "scope_type": {"type": "string"},
                "scope_id": {"type": "string"},
                "role": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["scope_type", "scope_id", "content"],
        },
        risk="medium",
        source="memory",
    ),
)


class McpServer:
    """Serve skills + memory as MCP tools, policy-filtered via exposure.py."""

    def __init__(
        self,
        *,
        ctx: ExposureContext | None = None,
        grants: Sequence[str] = (),
        catalog: Mapping[str, Any] | None = None,
        allowed_tools: Sequence[str] | None = None,
        skills_list_fn: Callable[[], list[dict[str, Any]]] | None = None,
        skills_get_fn: Callable[[str], dict[str, Any] | None] | None = None,
        memory_store: Any = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.ctx = ctx or ExposureContext()
        self.grants = tuple(grants)
        self.catalog = catalog
        self.env = env
        self._allowed = (
            set(allowed_tools) if allowed_tools is not None else {t.name for t in _SERVER_TOOLS}
        )
        self._skills_list = skills_list_fn
        self._skills_get = skills_get_fn
        self._memory = memory_store
        self._descriptors = {t.name: t for t in _SERVER_TOOLS if t.name in self._allowed}

    @property
    def mode(self) -> str:
        return mcp_bridge_mode(self.env)

    def _policy_visible(self) -> set[str]:
        """Return inventory tools authorized by the exposure decision.

        Server tools are not builtins in the default catalog — they behave like
        connector capabilities and must be named in ``grants``. Empty grants are
        deny-by-default (no skills/memory cross-session surface).
        """
        if not self.grants:
            return set()
        policy_catalog = dict(self.catalog or {})
        for name, descriptor in self._descriptors.items():
            policy_catalog.setdefault(
                name,
                _ServerExposureEntry(
                    id=name,
                    risk=descriptor.risk,
                    compact_hint=descriptor.description,
                    description=descriptor.description,
                    parameter_names=tuple(
                        str(key) for key in (descriptor.input_schema.get("properties") or {}).keys()
                    ),
                ),
            )
        decision = compute_exposure(
            self.ctx,
            self.grants,
            catalog=policy_catalog,  # type: ignore[arg-type]
        )
        visible = set(decision.visible)
        return set(self._descriptors).intersection(self.grants, visible)

    def list_tools(self) -> list[McpToolDescriptor]:
        if not mcp_bridge_enabled(self.env):
            return []
        visible = self._policy_visible()
        return [self._descriptors[n] for n in sorted(visible)]

    def call_tool(self, name: str, args: Mapping[str, Any] | None = None) -> McpCallResult:
        if not mcp_bridge_enabled(self.env):
            return McpCallResult(ok=False, error="bridge_off", detail="MCP bridge mode is off")
        visible = self._policy_visible()
        if name not in visible:
            denial = McpDenial(
                tool=name, reason=f"capability {name!r} is not served to this identity"
            )
            return McpCallResult(ok=False, error=denial.code, detail=denial.reason, denial=denial)
        payload = dict(args or {})
        try:
            if name == "skills_list":
                return self._skills_list_call(payload)
            if name == "skills_get":
                return self._skills_get_call(payload)
            if name == "memory_list":
                return self._memory_list(payload)
            if name == "memory_read":
                return self._memory_read(payload)
            if name == "memory_search":
                return self._memory_search(payload)
            if name == "memory_write":
                return self._memory_write(payload)
        except Exception as exc:  # noqa: BLE001
            return McpCallResult(ok=False, error="server_failed", detail=str(exc))
        return McpCallResult(ok=False, error="unknown_tool", detail=name)

    def _skills_list_call(self, payload: dict[str, Any]) -> McpCallResult:
        limit = int(payload.get("limit") or 50)
        if self._skills_list is not None:
            rows = self._skills_list()[: max(0, limit)]
            return McpCallResult(ok=True, content={"skills": rows})
        try:
            from omniagentos.skills import list_tree

            tree = list_tree()
            flat: list[dict[str, Any]] = []
            for category in tree:
                for sub in category.get("subcategories") or category.get("skills") or []:
                    if isinstance(sub, dict) and "skills" in sub:
                        for skill in sub.get("skills") or []:
                            if isinstance(skill, dict):
                                flat.append(skill)
                    elif isinstance(sub, dict):
                        flat.append(sub)
            return McpCallResult(ok=True, content={"skills": flat[: max(0, limit)]})
        except Exception as exc:  # noqa: BLE001
            return McpCallResult(ok=False, error="skills_unavailable", detail=str(exc))

    def _skills_get_call(self, payload: dict[str, Any]) -> McpCallResult:
        skill_id = str(payload.get("skill_id") or "")
        if not skill_id:
            return McpCallResult(ok=False, error="invalid_args", detail="skill_id required")
        if self._skills_get is not None:
            row = self._skills_get(skill_id)
            if row is None:
                return McpCallResult(ok=False, error="not_found", detail=skill_id)
            return McpCallResult(ok=True, content=row)
        try:
            from omniagentos.skills import get_skill

            row = get_skill(skill_id)
            if row is None:
                return McpCallResult(ok=False, error="not_found", detail=skill_id)
            return McpCallResult(ok=True, content=row)
        except Exception as exc:  # noqa: BLE001
            return McpCallResult(ok=False, error="skills_unavailable", detail=str(exc))

    def _memory_list(self, payload: dict[str, Any]) -> McpCallResult:
        store = self._memory
        if store is None:
            return McpCallResult(ok=True, content={"turns": []})
        limit = int(payload.get("limit") or 20)
        turns = store.recent_turns(
            str(payload.get("scope_type") or ""),
            str(payload.get("scope_id") or ""),
            limit,
        )
        content = [
            {
                "seq": t.seq,
                "role": t.role,
                "content": t.content,
                "created_at": t.created_at,
            }
            for t in turns
        ]
        return McpCallResult(ok=True, content={"turns": content})

    def _memory_read(self, payload: dict[str, Any]) -> McpCallResult:
        store = self._memory
        if store is None:
            return McpCallResult(ok=True, content={"summary": None})
        summary = store.rolling_summary(
            str(payload.get("scope_type") or ""),
            str(payload.get("scope_id") or ""),
        )
        return McpCallResult(ok=True, content={"summary": summary})

    def _memory_search(self, payload: dict[str, Any]) -> McpCallResult:
        store = self._memory
        if store is None:
            return McpCallResult(ok=True, content={"matches": []})
        query = str(payload.get("query") or "").lower()
        limit = int(payload.get("limit") or 20)
        turns = store.recent_turns(
            str(payload.get("scope_type") or ""),
            str(payload.get("scope_id") or ""),
            max(limit * 5, 50),
        )
        matches = []
        for t in turns:
            if query and query in (t.content or "").lower():
                matches.append({"seq": t.seq, "role": t.role, "content": t.content})
            if len(matches) >= limit:
                break
        return McpCallResult(ok=True, content={"matches": matches})

    def _memory_write(self, payload: dict[str, Any]) -> McpCallResult:
        if not memory_write_enabled(self.env):
            return McpCallResult(
                ok=False,
                error="memory_write_off",
                detail=f"{MEMORY_WRITE_ENV} is off",
            )
        store = self._memory
        if store is None:
            return McpCallResult(ok=False, error="no_store", detail="memory store not configured")
        turn = store.append_turn(
            str(payload.get("scope_type") or ""),
            str(payload.get("scope_id") or ""),
            str(payload.get("role") or "system"),
            str(payload.get("content") or ""),
        )
        return McpCallResult(
            ok=True,
            content={"seq": None if turn is None else turn.seq},
        )
