from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Route = Literal["solo_strong", "centralized_team", "parallel_review"]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: Route
    topology: str
    worker_count: int
    rationale: str


def _cap_for_topology(topology: str, hard_capacity: int) -> int:
    if topology == "parallel_sections":
        from omniagentos.allocation.fanout import TOPOLOGY_CAPS

        return TOPOLOGY_CAPS["parallel_sections"]
    if topology == "sequential":
        return 1
    if topology == "generator_critic":
        return 2
    if topology == "specialist_panel":
        return 3
    if topology in ("map_reduce", "hierarchical"):
        return hard_capacity
    return hard_capacity


def decide_route(
    char: Any,
    fanout_decision: Any,
    formation_binding: Any = None,
    ratio: float = 0.0,
    solo: bool = False,
    *,
    min_confidence: float = 0.5,
) -> RouteDecision:
    char_confidence = getattr(char, "confidence", 0.0)
    char_S = getattr(char, "S", 0.0)
    char_M = getattr(char, "M", 0.0)
    char_U = getattr(char, "U", 0.0)
    char_V = getattr(char, "V", 0.0)
    char_D = getattr(char, "D", 0.0)
    char_I = getattr(char, "I", 0.0)

    hard_capacity = getattr(fanout_decision, "hard_capacity", 1)

    # 1. solo is True -> solo_strong, sequential, 1 worker.
    if solo:
        return RouteDecision("solo_strong", "sequential", 1, "solo is True")
    # 2. char.confidence < min_confidence -> solo_strong, sequential, 1.
    if char_confidence < min_confidence:
        return RouteDecision(
            "solo_strong",
            "sequential",
            1,
            f"char.confidence ({char_confidence:.2f}) < min_confidence ({min_confidence:.2f})",
        )
    # 3. char.S >= 0.6 -> solo_strong, sequential, 1.
    if char_S >= 0.6:
        return RouteDecision("solo_strong", "sequential", 1, f"char.S ({char_S:.2f}) >= 0.6")
    # 4. fanout_decision.hard_capacity <= 1 -> solo_strong, sequential, 1.
    if hard_capacity <= 1:
        return RouteDecision(
            "solo_strong", "sequential", 1, f"fanout_decision.hard_capacity ({hard_capacity}) <= 1"
        )

    # 5. char.M >= 0.7 -> parallel_review, specialist_panel.
    if char_M >= 0.7:
        route: Route = "parallel_review"
        topology = "specialist_panel"
        rationale = f"char.M ({char_M:.2f}) >= 0.7"
    # 6. char.U >= 0.7 and char.V >= 0.5 -> parallel_review, generator_critic.
    elif char_U >= 0.7 and char_V >= 0.5:
        route = "parallel_review"
        topology = "generator_critic"
        rationale = f"char.U ({char_U:.2f}) >= 0.7 and char.V ({char_V:.2f}) >= 0.5"
    # 7. char.D >= 0.6 and char.I >= 0.5 -> centralized_team, map_reduce.
    elif char_D >= 0.6 and char_I >= 0.5:
        route = "centralized_team"
        topology = "map_reduce"
        rationale = f"char.D ({char_D:.2f}) >= 0.6 and char.I ({char_I:.2f}) >= 0.5"
    # 8. otherwise -> centralized_team, hierarchical.
    else:
        route = "centralized_team"
        topology = "hierarchical"
        rationale = "otherwise (default fallback)"

    # formation_binding override
    if formation_binding is not None:
        form_topo = getattr(formation_binding, "topology", None)
        form_low = getattr(formation_binding, "low_confidence", None)
        if form_low is None:
            form_low = False

        if form_topo:
            form_topo_str = str(form_topo).strip().lower()
            if form_topo_str and not form_low:
                if form_topo_str == "sequential":
                    return RouteDecision(
                        "solo_strong",
                        "sequential",
                        1,
                        f"{rationale}; overridden by confident formation topology='sequential' -> sequential",
                    )

                # Apply formation topology and determine route based on topology
                if form_topo_str in ("specialist_panel", "generator_critic"):
                    route = "parallel_review"
                elif form_topo_str in ("map_reduce", "hierarchical"):
                    route = "centralized_team"
                else:
                    route = "centralized_team"  # Fallback for unknown topology

                topology = form_topo_str
                rationale = (
                    f"{rationale}; overridden by confident formation topology='{form_topo_str}'"
                )

    # Compute worker count
    per_topology_cap = _cap_for_topology(topology, hard_capacity)
    f_wc = getattr(fanout_decision, "worker_count", None)
    val = f_wc if f_wc is not None else hard_capacity
    if val == 0:
        val = hard_capacity

    w = min(val, hard_capacity, per_topology_cap)
    w_clamped = max(2, min(4, w))
    w_final = min(w_clamped, hard_capacity, per_topology_cap)

    return RouteDecision(route, topology, w_final, rationale)
