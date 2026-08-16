"""The Phase-3 toolplane exposure computation module.

This module restricts what an identity should be able to see ahead of any search,
so that an unauthorized tool is never serialized into a prompt at all — not its
name, not its schema.

The Escalation Contract:
------------------------
* **Deferred is not denied.** A deferred tool is authorized; its full definition
  simply loads on demand via ``toolplane/search.py``. Deferral is a context-budget
  decision, never an access decision.
* **Hidden is invisible AND fail-closed.** An unauthorized tool is never named or
  described. The existing grant path (``connectors/store.py`` + ``api/routes/access.py``)
  is what an agent's *need* routes to; this module never widens a grant, it only
  narrows what is shown.
* **Not in the registry at all** -> the agent reports the gap. There is no implicit tool.
* **Search outage** -> deterministic full-allowed-list fallback. Losing search
  must degrade context efficiency, never availability.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry, RiskLevel, default_catalog
from omniagentos.toolplane.config import (
    DEFAULT_SMALL_CATALOG_TOKENS,
    DEFAULT_SMALL_CATALOG_TOOLS,
    ToolCatalogMode,
    core_tools,
    small_catalog_max_tokens,
    small_catalog_max_tools,
    tool_catalog_mode,
)
from omniagentos.toolplane.observe import emit_observation
from omniagentos.toolplane.scrub import scrub_value
from omniagentos.toolplane.session import SESSION_TOOL_CAPABILITY

__all__ = [
    "DEFAULT_CORE_ORDER",
    "EXPOSURE_PSEUDO_TOOL",
    "EXPOSURE_SOURCE",
    "ExposureContext",
    "ExposureDecision",
    "RISK_ORDER",
    "compute_exposure",
    "enforce_argv_patch",
    "estimate_schema_tokens",
    "log_exposure_decision",
    "native_tools_for_hidden",
]

RISK_ORDER: tuple[RiskLevel, ...] = ("low", "medium", "high")

DEFAULT_CORE_ORDER: tuple[str, ...] = (
    "read_file",
    "search_files",
    "list_files",
    "edit_file",
    "write_file",
)

EXPOSURE_SOURCE = "toolplane-exposure"
EXPOSURE_PSEUDO_TOOL = "<exposure>"

_PER_TOOL_OVERHEAD_TOKENS = 8


@dataclass(frozen=True, slots=True)
class ExposureContext:
    """Who is asking. All optional so a caller can compute an exposure with only what it has."""

    session_id: str = ""
    run_id: str = ""
    agent_id: str = ""
    lane: str = "session"
    holder_generation: int | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class ExposureDecision:
    """The result of an exposure computation."""

    core_tools: tuple[str, ...]  # always loaded in full, subset of `allowed`
    allowed: tuple[str, ...]  # loaded in full this turn
    deferred: tuple[str, ...]  # authorized, discoverable by search, not yet loaded
    hidden: tuple[str, ...]  # NEVER serialized to the model in any form
    reason: str  # short machine-ish reason, e.g. "small-catalog-bypass"
    mode: ToolCatalogMode
    bypassed: bool  # small-catalog bypass fired
    fallback: bool  # an error forced the deterministic fallback
    estimated_tokens: int  # heuristic schema cost of `allowed`

    @property
    def visible(self) -> tuple[str, ...]:
        """allowed + deferred, sorted. Everything this identity may reach."""
        return tuple(sorted(set(self.allowed) | set(self.deferred)))


def estimate_schema_tokens(entries: Iterable[CatalogEntry]) -> int:
    """Heuristic schema cost, in tokens, of serializing these tool definitions.

    Deliberately a character/4 heuristic and not a real tokenizer: this decides a
    context-budget threshold, not a billing number, and adding a tokenizer dependency
    to a policy path would be a poor trade. Documented as approximate; the bound it
    feeds is configurable for exactly that reason.
    """
    total = 0
    for entry in entries:
        char_count = (
            len(entry.id)
            + len(entry.namespace)
            + len(entry.compact_hint)
            + len(entry.description)
            + sum(len(p) for p in entry.parameter_names)
            + sum(len(x) for x in entry.input_examples)
        )
        total += (char_count // 4) + _PER_TOOL_OVERHEAD_TOKENS
    return total


def _fallback_decision(
    grants: Sequence[str],
    catalog: Mapping[str, CatalogEntry] | None,
    exc: Exception,
    role: str | None = None,
) -> ExposureDecision:
    """Deterministic full-allowed-list fallback: every tool the identity is entitled to, loaded.

    Losing the exposure computation must cost context efficiency, never availability — an agent
    that suddenly cannot see its own tools is a worse failure than a large prompt. It must equally
    never WIDEN access, so this is built from `grants` (a caller-supplied fact we already trust and
    that never passed through the code that just failed) plus the built-ins, and never from the
    whole catalog.
    """
    try:
        # Fallback reasoning:
        # If catalog lookup fails, allowed is the sorted grants themselves.
        # Otherwise, built-in ids present in catalog + grant ids present in catalog.
        if not catalog:
            allowed_set = set(grants)
        else:
            allowed_set = set()
            for entry_id, entry in catalog.items():
                if entry.source == "builtin":
                    allowed_set.add(entry_id)
                elif entry.source == "connector" and entry_id in grants:
                    allowed_set.add(entry_id)

        allowed_tuple = tuple(sorted(allowed_set))
    except Exception:
        try:
            allowed_tuple = tuple(sorted(set(grants)))
        except Exception:
            allowed_tuple = ()

    hidden_tuple: tuple[str, ...] = ()
    try:
        if role == "classifier":
            hidden_tuple = tuple(sorted(catalog)) if catalog else tuple(sorted(set(grants)))
            allowed_tuple = ()
        elif role == "coordinator":
            if catalog:
                hidden_tuple = tuple(
                    sorted(
                        cap_id
                        for cap_id, entry in catalog.items()
                        if entry.action_class is not ActionClass.READ_ONLY
                    )
                )
                allowed_tuple = tuple(
                    cap_id for cap_id in allowed_tuple if cap_id not in hidden_tuple
                )
            else:
                hidden_tuple = tuple(sorted(set(grants)))
                allowed_tuple = ()
    except Exception:
        if role in {"classifier", "coordinator"}:
            allowed_tuple = ()

    try:
        mode = tool_catalog_mode()
    except Exception:
        mode = "off"

    return ExposureDecision(
        core_tools=(),
        allowed=allowed_tuple,
        deferred=(),
        hidden=hidden_tuple,
        reason=f"fallback:{type(exc).__name__}",
        mode=mode,
        bypassed=False,
        fallback=True,
        estimated_tokens=0,
    )


def compute_exposure(
    ctx: ExposureContext,
    grants: Sequence[str],
    risk: RiskLevel = "high",
    catalog: Mapping[str, CatalogEntry] | None = None,
) -> ExposureDecision:
    """Compute what an identity should be able to see, ahead of any search."""
    try:
        # 1. Resolve catalog
        cat = catalog if catalog is not None else default_catalog()

        # 2. Policy filter, BEFORE anything else.
        visible_set: set[str] = set()
        hidden_set: set[str] = set()

        for cap_id, entry in cat.items():
            # Policy filter:
            # - connector entries are visible only if in grants (fail-closed)
            # - builtin entries are always grant-visible
            is_grant_visible = False
            if entry.source == "builtin":
                # Built-ins are gated at call time by the capability manifest's
                # allowed_ops, not by connector grants.
                is_grant_visible = True
            elif entry.source == "connector":
                if cap_id in grants:
                    is_grant_visible = True

            # Additionally, ANY entry whose risk exceeds the risk ceiling is hidden
            # compare via RISK_ORDER.index.
            is_risk_allowed = True
            try:
                entry_risk_idx = RISK_ORDER.index(entry.risk)
                risk_ceiling_idx = RISK_ORDER.index(risk)
                if entry_risk_idx > risk_ceiling_idx:
                    is_risk_allowed = False
            except ValueError:
                # If risk levels are unknown, we fail closed by hiding the tool.
                is_risk_allowed = False

            if is_grant_visible and is_risk_allowed:
                visible_set.add(cap_id)
            else:
                hidden_set.add(cap_id)

        # 2b. Role subtraction applies only after grant and risk filtering.
        if ctx.role == "classifier":
            hidden_set.update(visible_set)
            visible_set.clear()
        elif ctx.role == "coordinator":
            role_hidden = {
                cap_id
                for cap_id in visible_set
                if cat[cap_id].action_class is not ActionClass.READ_ONLY
            }
            visible_set.difference_update(role_hidden)
            hidden_set.update(role_hidden)

        # Nothing downstream may reintroduce a hidden id — the
        # filter runs before search precisely so hidden ids never enter an index.

        # 3. Core tools
        config_core = core_tools()
        if not config_core:
            config_core = DEFAULT_CORE_ORDER

        # Intersect with visible, order preserved, capped at 5
        core_list: list[str] = []
        for t in config_core:
            if t in visible_set:
                core_list.append(t)
        core_list = core_list[:5]
        core_tuple = tuple(core_list)

        # 4. Small-catalog bypass
        # estimate_schema_tokens of visible entries
        visible_entries = [cat[cap_id] for cap_id in visible_set]
        estimated_tokens = estimate_schema_tokens(visible_entries)

        try:
            max_tools = small_catalog_max_tools()
            max_tokens = small_catalog_max_tokens()
        except Exception:
            max_tools = DEFAULT_SMALL_CATALOG_TOOLS
            max_tokens = DEFAULT_SMALL_CATALOG_TOKENS

        allowed_tuple: tuple[str, ...]
        deferred_tuple: tuple[str, ...]

        if len(visible_set) <= max_tools or estimated_tokens <= max_tokens:
            allowed_tuple = tuple(sorted(visible_set))
            deferred_tuple = ()
            bypassed = True
            reason = "small-catalog-bypass"
        else:
            # 5. Otherwise
            allowed_tuple = tuple(sorted(core_list))
            deferred_tuple = tuple(sorted(visible_set - set(core_list)))
            bypassed = False
            reason = "deferred"

        try:
            mode = tool_catalog_mode()
        except Exception:
            mode = "off"

        return ExposureDecision(
            core_tools=core_tuple,
            allowed=allowed_tuple,
            deferred=deferred_tuple,
            hidden=tuple(sorted(hidden_set)),
            reason=reason,
            mode=mode,
            bypassed=bypassed,
            fallback=False,
            estimated_tokens=estimated_tokens,
        )

    except Exception as exc:
        return _fallback_decision(grants, catalog, exc, ctx.role)


def log_exposure_decision(decision: ExposureDecision, ctx: ExposureContext) -> bool:
    """Durably record one exposure decision via the toolplane observation sink.

    Metadata only, matching the sink's contract: COUNTS, not membership. The hidden and
    deferred lists are deliberately NOT recorded — writing them would put the exact shape
    of an identity's catalog into the ledger, which is the thing the hidden set exists to
    withhold. `core_tools` IS recorded because it is the smallest, least sensitive set and
    the one an operator needs to debug a bad exposure.

    Never raises: a logging failure must not change an exposure decision.
    """
    try:
        record = {
            "version": "1",
            "ts": datetime.now(UTC).isoformat(),
            "tool": EXPOSURE_PSEUDO_TOOL,
            "run_id": ctx.run_id,
            "session_id": ctx.session_id,
            "holder_generation": ctx.holder_generation,
            "correlation_id": uuid.uuid4().hex[:16],
            "status": "success",
            "ok": True,
            "error": None,
            "duration_ms": 0,
            "source": EXPOSURE_SOURCE,
            "mode": decision.mode,
            "bypassed": decision.bypassed,
            "fallback": decision.fallback,
            "reason": decision.reason,
            "estimated_tokens": decision.estimated_tokens,
            "n_core": len(decision.core_tools),
            "n_allowed": len(decision.allowed),
            "n_deferred": len(decision.deferred),
            "n_hidden": len(decision.hidden),
            "core_tools": list(decision.core_tools),
            "lane": ctx.lane,
            "agent_id": ctx.agent_id,
        }
        scrubbed = scrub_value(record)
        if not isinstance(scrubbed, dict):
            scrubbed = record
        return emit_observation(scrubbed)
    except Exception:
        return False


def native_tools_for_hidden(decision: ExposureDecision) -> tuple[str, ...]:
    """Invert ``SESSION_TOOL_CAPABILITY`` and return sorted native tool names.

    Returns the sorted native tool names whose mapped capability is in
    ``decision.hidden``. Never returns a name with no mapping.
    """
    hidden_caps = set(decision.hidden)
    native_tools = []
    for native, cap in SESSION_TOOL_CAPABILITY.items():
        if cap in hidden_caps:
            native_tools.append(native)
    return tuple(sorted(native_tools))


def enforce_argv_patch(argv: Sequence[str], decision: ExposureDecision) -> list[str]:
    """Pure patch for argv specifying disallowed native tools.

    Returns a new list of arguments. This only ever narrows.
    """
    names = native_tools_for_hidden(decision)
    if not names:
        return list(argv)

    argv_list = list(argv)
    try:
        idx = argv_list.index("--disallowedTools")
    except ValueError:
        idx = -1

    if idx != -1:
        if idx == len(argv_list) - 1:
            argv_list.append(",".join(sorted(names)))
        else:
            existing_val = argv_list[idx + 1]
            existing_parts = [p.strip() for p in existing_val.split(",") if p.strip()]
            union_set = set(existing_parts) | set(names)
            new_val = ",".join(sorted(union_set))
            argv_list[idx + 1] = new_val
    else:
        argv_list.extend(["--disallowedTools", ",".join(sorted(names))])

    return argv_list
