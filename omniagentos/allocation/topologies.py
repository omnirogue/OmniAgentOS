"""Supported collaboration topologies."""

from typing import Literal

Topology = Literal[
    "sequential",
    "map_reduce",
    "generator_critic",
    "specialist_panel",
    "hierarchical",
    "parallel_sections",
]

SEQUENTIAL: Topology = "sequential"
MAP_REDUCE: Topology = "map_reduce"
GENERATOR_CRITIC: Topology = "generator_critic"
SPECIALIST_PANEL: Topology = "specialist_panel"
HIERARCHICAL: Topology = "hierarchical"
PARALLEL_SECTIONS: Topology = "parallel_sections"
