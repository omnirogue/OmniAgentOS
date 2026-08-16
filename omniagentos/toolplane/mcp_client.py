"""In-process MCP CLIENT shim riding the existing exposure.py policy surface.

No side transport, no network listener, no third-party MCP dependency. Every
list/call is filtered through :func:`compute_exposure`. Media tools always ride
the same manifest/dispatch gate as toolplane (allowed_ops, write-root
confinement, artifact verification, scrubbing) — there is no unconstrained
``run_media_tool`` side path. Gated by ``OMNIAGENTOS_MCP_BRIDGE_MODE``
(default ``off``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniagentos.toolplane.exposure import ExposureContext, compute_exposure
from omniagentos.toolplane.mcp_mode import mcp_bridge_enabled, mcp_bridge_mode
from omniagentos.toolplane.mcp_types import McpCallResult, McpDenial, McpToolDescriptor
from omniagentos.toolplane.media_bridge import (
    MEDIA_TOOL_NAMES,
    media_tool_descriptors,
)
from omniagentos.toolplane.workmodes_bridge import (
    VALIDATOR_TOOL_NAMES,
    run_workmodes_tool,
    workmodes_tool_descriptors,
)

__all__ = ["McpClient", "default_bridge_descriptors"]

ToolHandler = Callable[[str, Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _BridgeExposureEntry:
    """Minimal catalog entry for bridge descriptors absent from the main catalog."""

    id: str
    risk: str
    source: str
    namespace: str = "mcp-client"
    compact_hint: str = ""
    description: str = ""
    parameter_names: tuple[str, ...] = ()
    input_examples: tuple[str, ...] = ()


def default_bridge_descriptors() -> list[McpToolDescriptor]:
    """Descriptors for tools the bridge registers beyond the builtin inventory."""
    return [*media_tool_descriptors(), *workmodes_tool_descriptors()]


def _builtin_descriptors() -> list[McpToolDescriptor]:
    from omniagentos.toolplane.tools import CAPABILITY_INVENTORY

    out: list[McpToolDescriptor] = []
    for name, meta in CAPABILITY_INVENTORY.items():
        out.append(
            McpToolDescriptor(
                name=name,
                description=f"toolplane builtin: {name}",
                input_schema={"type": "object"},
                risk=str(meta.get("risk") or "low"),
                source="builtin",
            )
        )
    return out


class McpClient:
    """Policy-checked MCP client for in-process list/call of exposed tools.

    Capability manifests for media tools are rebuilt exclusively from trusted
    server-side state: the authenticated :attr:`ctx` identity, constructor-
    configured filesystem grants (``write_roots`` / ``read_roots``), and the
    same exposure-policy visibility set used for list/call. Callers cannot
    supply ``_manifest``; any attempt is rejected outright.
    """

    def __init__(
        self,
        *,
        ctx: ExposureContext | None = None,
        grants: Sequence[str] = (),
        write_roots: Sequence[str] = (),
        read_roots: Sequence[str] = (),
        catalog: Mapping[str, Any] | None = None,
        extra_descriptors: Sequence[McpToolDescriptor] | None = None,
        handler: ToolHandler | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.ctx = ctx or ExposureContext()
        self.grants = tuple(grants)
        # Server-side filesystem grants — never taken from call arguments.
        self.write_roots = tuple(write_roots)
        self.read_roots = tuple(read_roots) if read_roots else tuple(write_roots)
        self.catalog = catalog
        self.env = env
        self._extra = (
            list(extra_descriptors)
            if extra_descriptors is not None
            else default_bridge_descriptors()
        )
        self._handler = handler
        self._by_name: dict[str, McpToolDescriptor] = {
            d.name: d for d in [*_builtin_descriptors(), *self._extra]
        }

    @property
    def mode(self) -> str:
        return mcp_bridge_mode(self.env)

    def _visible_names(self) -> set[str]:
        from omniagentos.toolplane.catalog import default_catalog

        policy_catalog = dict(default_catalog() if self.catalog is None else self.catalog)
        for name, descriptor in self._by_name.items():
            if name in policy_catalog:
                continue
            # Local workmode validators are builtin/read-only. Any arbitrary
            # extra descriptor is connector-style and requires an explicit grant.
            source = "builtin" if name in VALIDATOR_TOOL_NAMES else "connector"
            policy_catalog[name] = _BridgeExposureEntry(
                id=name,
                risk=descriptor.risk,
                source=source,
                compact_hint=descriptor.description,
                description=descriptor.description,
                parameter_names=tuple(
                    str(key) for key in (descriptor.input_schema.get("properties") or {}).keys()
                ),
            )
        decision = compute_exposure(
            self.ctx,
            self.grants,
            catalog=policy_catalog,  # type: ignore[arg-type]
        )
        return set(decision.visible).intersection(self._by_name)

    def list_tools(self) -> list[McpToolDescriptor]:
        if not mcp_bridge_enabled(self.env):
            return []
        visible = self._visible_names()
        return [self._by_name[n] for n in sorted(visible) if n in self._by_name]

    def call_tool(self, name: str, args: Mapping[str, Any] | None = None) -> McpCallResult:
        if not mcp_bridge_enabled(self.env):
            return McpCallResult(
                ok=False,
                error="bridge_off",
                detail="OMNIAGENTOS_MCP_BRIDGE_MODE is off",
            )
        payload = dict(args or {})
        # Caller-supplied manifests are never accepted — not for media, not for
        # any other tool. Capabilities come only from server-side trusted state.
        if "_manifest" in payload:
            denial = McpDenial(
                tool=name,
                reason=(
                    "caller-supplied _manifest is rejected; capability manifests "
                    "are rebuilt exclusively from server-side context"
                ),
            )
            return McpCallResult(
                ok=False,
                error="caller_manifest_rejected",
                detail=denial.reason,
                denial=denial,
            )

        visible = self._visible_names()
        if name not in visible:
            denial = McpDenial(tool=name, reason=f"tool {name!r} is not exposed to this identity")
            return McpCallResult(ok=False, error=denial.code, detail=denial.reason, denial=denial)

        # Media always rides toolplane dispatch with a server-built manifest.
        if name in MEDIA_TOOL_NAMES:
            return self._call_media_gated(name, payload)

        if self._handler is not None:
            try:
                out = self._handler(name, payload)
            except Exception as exc:  # noqa: BLE001
                return McpCallResult(ok=False, error="handler_failed", detail=str(exc))
            return _from_handler_dict(out)

        if name in VALIDATOR_TOOL_NAMES:
            return _from_handler_dict(run_workmodes_tool(name, payload))

        return McpCallResult(
            ok=False,
            error="no_handler",
            detail=f"no in-process handler for tool {name!r}",
        )

    def _trusted_media_manifest(self) -> Any:
        """Rebuild a capability manifest from server-side trusted state only.

        * Identity fields come from :attr:`ctx` (authenticated exposure context).
        * ``allowed_ops`` is the exposure-policy visibility set (same source as
          list/call gating).
        * ``write_roots`` / ``read_roots`` come from constructor-configured
          filesystem grants — never from call arguments.
        """
        from omniagentos.toolplane.manifest import CapabilityManifest

        session_id = str(self.ctx.session_id or "").strip() or "mcp-session"
        run_id = str(self.ctx.run_id or "").strip() or "mcp-run"
        holder = self.ctx.holder_generation
        if holder is None:
            # Authenticated sessions always have a generation; bridge tests and
            # non-session callers default to a stable zero so dispatch's
            # holder_generation presence check still passes without inventing
            # a privileged generation number.
            holder = 0
        allowed_ops = sorted(self._visible_names())
        write_roots = [str(root) for root in self.write_roots if str(root).strip()]
        read_roots = [str(root) for root in self.read_roots if str(root).strip()]
        if not read_roots:
            read_roots = list(write_roots)
        return CapabilityManifest(
            run_id=run_id,
            session_id=session_id,
            holder_generation=int(holder),
            read_roots=read_roots,
            write_roots=write_roots,
            allowed_ops=allowed_ops,
        )

    def _call_media_gated(self, name: str, payload: dict[str, Any]) -> McpCallResult:
        """Route media tools through the same manifest/dispatch gate as toolplane.

        No unconstrained ``run_media_tool`` path and no caller-supplied
        ``_manifest``: every call rebuilds the capability ceiling from trusted
        server-side state so allowed_ops, write-root confinement, artifact
        verification, and scrubbing all apply.
        """
        manifest = self._trusted_media_manifest()
        return self._dispatch_with_manifest(name, manifest, payload)

    def _dispatch_with_manifest(
        self,
        name: str,
        manifest: Any,
        payload: dict[str, Any],
    ) -> McpCallResult:
        from omniagentos.toolplane.tools import dispatch

        # There is exactly one manifest-backed execution path. Unknown tools,
        # including an accidentally unregistered media descriptor, fail closed
        # inside dispatch rather than falling back to a bridge handler.
        return _from_handler_dict(dispatch(name, manifest, payload))


def _from_handler_dict(out: dict[str, Any]) -> McpCallResult:
    ok = bool(out.get("ok"))
    return McpCallResult(
        ok=ok,
        content=out.get("content", out.get("result", out)),
        error=None if ok else str(out.get("error") or "failed"),
        detail=None if ok else (str(out.get("detail")) if out.get("detail") is not None else None),
    )
