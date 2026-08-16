"""M-13: Observable, gated Graph Runtime default-path soak.

L16 owns the swarm default path; L18 owns Graph Runtime storage/service.
This module only *calls* GraphRuntimeService through its published API and
records soak observability on the swarm provision result / plan payload.

Modes (``OMNIAGENTOS_GRAPH_RUNTIME``):

- ``off`` / ``0`` / ``false`` / ``no`` — no graph provision
- unset / ``shadow`` / ``soak`` — **default-path soak**: provision a diamond for
  multi-task plans, stamp observability, do **not** claim the graph is the
  completed default executor
- ``on`` / ``1`` / ``true`` / ``yes`` / ``execute`` — explicit live link (same
  provision call; still not a claim of full scheduler replacement)

Soak never marks the graph run completed. Completion remains L18's concern.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

LOG = logging.getLogger(__name__)

__all__ = [
    "GraphSoakObservation",
    "graph_runtime_mode",
    "maybe_link_graph_for_swarm",
    "should_provision_graph",
]

_OFF = frozenset({"", "0", "off", "false", "no", "disabled"})
_SOAK = frozenset({"shadow", "soak", "observe", "default"})
_ON = frozenset({"1", "true", "yes", "on", "execute", "live"})


@dataclass(frozen=True, slots=True)
class GraphSoakObservation:
    """Observable soak stamp returned to callers of ``provision_run``."""

    mode: str
    enabled: bool
    graph_run_id: str | None
    status: str
    claim: str
    reason: str
    swarm_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "graph_run_id": self.graph_run_id,
            "status": self.status,
            "claim": self.claim,
            "reason": self.reason,
            "swarm_run_id": self.swarm_run_id,
            "metadata": dict(self.metadata),
        }


def graph_runtime_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve the effective Graph Runtime integration mode.

    Unset env → ``shadow`` (default-path soak). Explicit off values disable.
    """
    source = env if env is not None else os.environ
    raw = str(source.get("OMNIAGENTOS_GRAPH_RUNTIME", "")).strip().lower()
    if raw in _OFF and raw != "":
        return "off"
    if raw == "":
        return "shadow"
    if raw in _SOAK:
        return "shadow" if raw in {"shadow", "observe", "default"} else "soak"
    if raw in _ON:
        return "on"
    # Unknown non-empty value: treat as explicit enable only when truthy-looking.
    if raw in {"maybe", "trial"}:
        return "soak"
    return "off"


def should_provision_graph(*, mode: str | None = None, target_n: int) -> bool:
    """Whether multi-task provision should link a Graph Runtime diamond."""
    resolved = mode or graph_runtime_mode()
    if resolved == "off":
        return False
    return int(target_n or 0) >= 2


def maybe_link_graph_for_swarm(
    *,
    db_path: str | None,
    swarm_run_id: str,
    project_id: str | None,
    target_n: int,
    title: str | None = None,
    env: Mapping[str, str] | None = None,
) -> GraphSoakObservation:
    """Provision a linked diamond when the soak/on gate allows it.

    Fail-open for swarm provision (graph is additive) but always return an
    observable stamp so operators can see soak state without source greps.
    """
    mode = graph_runtime_mode(env)
    if not should_provision_graph(mode=mode, target_n=target_n):
        reason = "graph_runtime_off" if mode == "off" else f"target_n={target_n}<2"
        return GraphSoakObservation(
            mode=mode,
            enabled=False,
            graph_run_id=None,
            status="skipped",
            claim="none",
            reason=reason,
            swarm_run_id=swarm_run_id,
        )

    try:
        from omniagentos.graph_runtime.service import GraphRuntimeService

        gr = GraphRuntimeService(db_path=db_path)
        graph = gr.start_from_template(
            "diamond-v1",
            title=title or f"swarm:{swarm_run_id}",
            project_id=project_id,
            completeness_policy="fail_closed",
            swarm_run_id=str(swarm_run_id),
        )
        graph_run_id = str(graph.get("id") or "") or None
        graph_status = str(graph.get("status") or "planning")
        # Never claim default-path completion from soak — only that the diamond
        # was linked and is observable for soak measurement.
        claim = "linked_for_soak" if mode in {"shadow", "soak"} else "linked_live"
        obs = GraphSoakObservation(
            mode=mode,
            enabled=True,
            graph_run_id=graph_run_id,
            status=graph_status,
            claim=claim,
            reason="provisioned_diamond_v1",
            swarm_run_id=swarm_run_id,
            metadata={
                "template": "diamond-v1",
                "completeness_policy": "fail_closed",
                "node_count": len(graph.get("nodes") or []),
                "default_executor": False,
            },
        )
        LOG.info(
            "graph_runtime soak mode=%s claim=%s graph_run_id=%s swarm_run_id=%s status=%s",
            obs.mode,
            obs.claim,
            obs.graph_run_id,
            swarm_run_id,
            obs.status,
        )
        return obs
    except Exception as exc:  # noqa: BLE001 — additive; never block provision
        LOG.warning("graph_runtime soak provision failed", exc_info=True)
        return GraphSoakObservation(
            mode=mode,
            enabled=False,
            graph_run_id=None,
            status="error",
            claim="none",
            reason=f"provision_failed:{type(exc).__name__}",
            swarm_run_id=swarm_run_id,
            metadata={"error": str(exc)[:300]},
        )
