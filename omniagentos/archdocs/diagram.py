"""System-map mermaid diagram for the daily archi-morning job.

A STATIC, HAND-CURATED map of the system: nodes are the subsystems from
`ARCHI.md`'s Subsystems section plus the swarm pipeline (Intake -> Planner ->
Scheduler -> Router -> Spawner -> provider CLIs -> Reviewer -> Summary ->
Optimizer), Longhaul, Runner, API, Dashboard, and the limit_state/accounts
layer. Edges are the real call/ownership relationships derived by hand from
`ARCHI.md` + `docs/architecture/*.md` — correctness over cleverness. The
diagram is regenerated VERBATIM each run by `scripts/archi-morning/
archi-morning.sh`; when the architecture changes, edit `NODES`/`SUBGRAPHS`/
`EDGES` below by hand (that edit is the review surface, exactly like a doc).

Outputs (written by :func:`write_system_map`):
  - ``docs/architecture/system-map.mmd`` — raw mermaid flowchart
  - ``docs/architecture/system-map.md``  — same diagram in a fenced block
    with a one-paragraph legend

CLI: ``python -m omniagentos.archdocs.diagram [--repo-root DIR] [--check]``

Kept deliberately under ~35 nodes so the flowchart renders legibly; the unit
test in ``tests/archdocs/test_diagram.py`` enforces the bound plus basic
mermaid well-formedness (edge endpoints defined, unique ids, balanced
brackets).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from omniagentos.archdocs.generate import _repo_root_default

# ---------------------------------------------------------------------------
# Curated map data (edit BY HAND when the architecture changes)
# ---------------------------------------------------------------------------

# id -> label. Labels must not contain [](){}"| — they are emitted inside
# double-quoted mermaid node text and the test's bracket-balance check counts
# raw brackets.
NODES: dict[str, str] = {
    # Operator surface
    "dashboard": "Dashboard - Next.js :3000",
    "api": "API - FastAPI :8485",
    # Swarm pipeline (WP1-WP8)
    "intake": "Intake",
    "planner": "Planner - brief to DAG",
    "scheduler": "Scheduler - coordinator + slots",
    "router": "Router - adaptive tiers",
    "spawner": "Spawner",
    "reviewer": "Reviewer",
    "summary": "Summary + Throughput Score",
    "optimizer": "Optimizer - 2x daily",
    # Provider CLIs (local subprocess only, never HTTP)
    "cli_claude": "claude",
    "cli_codex": "codex",
    "cli_grok": "grok",
    "cli_gemini": "gemini",
    "cli_kimi": "kimi",
    # Execution
    "runner": "Runner - polling worker",
    "orchestrator": "Orchestrator - tier cascade",
    "adapters": "CLI adapter registry",
    "db": "SQLite WAL var/omniagentos.db",
    "ledger": "Ledger - append-only JSONL",
    "vault": "Vault notes",
    # Scheduling
    "launchd": "launchd jobs - LaunchAgents",
    "routines": "Routines engine - 300s tick",
    "archdocs": "archdocs - ARCHI.md + system map",
    # Reliability / org
    "reliability": "Reliability V2 - watch, audit, daily, weekly",
    "organization": "Agent org - CTO, VPs, managers",
    # Standalone subsystems / shared layers
    "governance": "Governance - ActionClass gate, approvals, budgets",
    "knowledge": "Knowledge - skills, Synapse, memory, repomap",
    "longhaul": "Longhaul engine - attempt chain",
    "accounts": "limit_state / claude_accounts - cooldown + rotation",
    "modelintel": "modelintel registry - daily 07:15",
}

# (subgraph_id, title, [node ids]); ids must not collide with node ids.
SUBGRAPHS: list[tuple[str, str, list[str]]] = [
    ("operator", "Operator surface", ["dashboard", "api"]),
    (
        "swarm",
        "Swarm pipeline",
        [
            "intake",
            "planner",
            "scheduler",
            "router",
            "spawner",
            "reviewer",
            "summary",
            "optimizer",
        ],
    ),
    (
        "providers",
        "Provider CLIs",
        ["cli_claude", "cli_codex", "cli_grok", "cli_gemini", "cli_kimi"],
    ),
    (
        "execution",
        "Execution",
        ["runner", "orchestrator", "adapters", "db", "ledger", "vault"],
    ),
    ("scheduling", "Scheduling", ["launchd", "routines", "archdocs"]),
    ("relorg", "Reliability", ["reliability", "organization"]),
]

# (src, dst, style, label). style: "solid" = call/ownership/dataflow,
# "dashed" = advisory/context feed. Endpoints may be node ids OR subgraph ids
# (mermaid flowcharts allow edges to a subgraph — used for the provider
# fan-out so five parallel arrows don't clutter the chart).
EDGES: list[tuple[str, str, str, str]] = [
    # Operator surface
    ("dashboard", "api", "solid", "same-origin token proxy"),
    ("api", "db", "solid", ""),
    ("api", "governance", "dashed", "approvals + token gate"),
    ("api", "intake", "solid", ""),
    # Intake dispatch (swarm / fastlane / longhaul)
    ("intake", "planner", "solid", "swarm brief"),
    ("intake", "orchestrator", "solid", "fastlane / orchestrations"),
    ("intake", "longhaul", "solid", "lane=longhaul"),
    # Swarm pipeline
    ("knowledge", "planner", "dashed", "recall priors"),
    ("planner", "scheduler", "solid", "provisioned DAG"),
    ("scheduler", "db", "solid", "state rebuilt from rows"),
    ("scheduler", "router", "solid", "tier ladder"),
    ("router", "spawner", "solid", "route decision"),
    ("modelintel", "router", "dashed", "rankings"),
    ("spawner", "providers", "solid", ""),
    ("spawner", "accounts", "dashed", "account pick"),
    ("scheduler", "reviewer", "solid", "attempt terminal"),
    ("reviewer", "summary", "solid", ""),
    ("summary", "optimizer", "solid", "mined run outcomes"),
    ("optimizer", "planner", "dashed", "learned.json playbook"),
    ("optimizer", "vault", "solid", "swarm playbook"),
    # Scheduling fan-out
    ("launchd", "optimizer", "solid", "03:45 + 15:45"),
    ("launchd", "runner", "solid", "keep-alive"),
    ("launchd", "routines", "solid", "every 300s"),
    ("launchd", "reliability", "solid", "watch / audit / daily / weekly"),
    ("launchd", "modelintel", "solid", "07:15"),
    ("launchd", "archdocs", "solid", "07:05 archi-morning"),
    ("routines", "db", "solid", "routine_runs + tasks"),
    # Execution
    ("runner", "db", "solid", "polls queued runs"),
    ("runner", "adapters", "solid", "step plans"),
    ("adapters", "providers", "solid", "local CLI subprocess"),
    ("runner", "governance", "dashed", "ActionClass approval gate"),
    ("runner", "ledger", "solid", "JSONL manifest"),
    ("runner", "vault", "solid", "run notes"),
    ("orchestrator", "adapters", "solid", "tier-escalating sessions"),
    ("knowledge", "orchestrator", "dashed", "context injection"),
    # Reliability / org
    ("reliability", "organization", "solid", "dept reviews + CTO pass"),
    ("reliability", "knowledge", "dashed", "confirmed fix to skill"),
    ("archdocs", "reliability", "dashed", "arch context"),
    # Longhaul
    ("longhaul", "providers", "solid", "executor attempts"),
    ("longhaul", "accounts", "dashed", "cooldown, never disable"),
    ("longhaul", "db", "solid", "attempt chain + slots"),
    ("modelintel", "longhaul", "dashed", "registry-ranked workers"),
]

MMD_RELPATH = Path("docs") / "architecture" / "system-map.mmd"
MD_RELPATH = Path("docs") / "architecture" / "system-map.md"

_LEGEND = (
    "System map for OmniAgentOS, regenerated verbatim each morning by "
    "`scripts/archi-morning/archi-morning.sh` from the hand-curated node/edge "
    "tables in `omniagentos/archdocs/diagram.py` (edit those tables when the "
    "architecture changes — this file is output, not source). Boxes are "
    "subsystems (`ARCHI.md` Subsystems section) plus the swarm pipeline stages; "
    "solid arrows are real call/ownership/dataflow relationships, dashed arrows "
    "are advisory or context feeds (rankings, recall, learned playbooks). "
    "Provider CLIs are always local subprocesses — never direct HTTP clients. "
    "Per-domain detail lives in `docs/architecture/*.md`."
)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def build_mermaid() -> str:
    """Render the curated map as a mermaid ``flowchart LR`` string."""
    lines: list[str] = ["flowchart LR"]

    in_subgraph: set[str] = set()
    for sg_id, title, members in SUBGRAPHS:
        lines.append(f'    subgraph {sg_id}["{title}"]')
        for node_id in members:
            lines.append(f'        {node_id}["{NODES[node_id]}"]')
            in_subgraph.add(node_id)
        lines.append("    end")

    for node_id, label in NODES.items():
        if node_id not in in_subgraph:
            lines.append(f'    {node_id}["{label}"]')

    for src, dst, style, label in EDGES:
        arrow = "-.->" if style == "dashed" else "-->"
        if label:
            lines.append(f"    {src} {arrow}|{label}| {dst}")
        else:
            lines.append(f"    {src} {arrow} {dst}")

    return "\n".join(lines) + "\n"


def build_markdown() -> str:
    """Render ``system-map.md``: legend paragraph + fenced mermaid block."""
    return f"# System map — OmniAgentOS\n\n{_LEGEND}\n\n```mermaid\n{build_mermaid()}```\n"


def write_system_map(repo_root: str | Path) -> dict[str, str]:
    """Write both system-map files under ``docs/architecture/``.

    Returns ``{"mmd": path, "md": path, "nodes": n, "edges": n}`` (paths as str).
    """
    repo_root = Path(repo_root)
    mmd_path = repo_root / MMD_RELPATH
    md_path = repo_root / MD_RELPATH
    mmd_path.parent.mkdir(parents=True, exist_ok=True)
    mmd_path.write_text(build_mermaid(), encoding="utf-8")
    md_path.write_text(build_markdown(), encoding="utf-8")
    return {
        "mmd": str(mmd_path),
        "md": str(md_path),
        "nodes": str(len(NODES)),
        "edges": str(len(EDGES)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="omniagentos.archdocs.diagram", description=__doc__)
    parser.add_argument("--repo-root", default=str(_repo_root_default()))
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if regeneration would change either file (writes nothing)",
    )
    args = parser.parse_args()
    repo_root = Path(args.repo_root)

    if args.check:
        mmd_path = repo_root / MMD_RELPATH
        md_path = repo_root / MD_RELPATH
        current_mmd = mmd_path.read_text(encoding="utf-8") if mmd_path.exists() else None
        current_md = md_path.read_text(encoding="utf-8") if md_path.exists() else None
        stale = current_mmd != build_mermaid() or current_md != build_markdown()
        raise SystemExit(1 if stale else 0)

    result = write_system_map(repo_root)
    print(
        f"system-map: {result['nodes']} nodes, {result['edges']} edges -> "
        f"{result['mmd']}, {result['md']}"
    )


if __name__ == "__main__":
    main()
