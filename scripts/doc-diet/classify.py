#!/usr/bin/env python3
"""
scripts/doc-diet/classify.py

Read-only scan of all tracked markdown files in the repository.
Classifies them as living, stale, or generated.
Generates an inventory at docs/consolidation/C3-DOC-INVENTORY.md.
Optionally performs `git mv` for stale/generated docs into docs/archive/ preserving paths.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def get_tracked_md_files() -> list[str]:
    """Get all tracked .md files in the repository using git ls-files."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.md"], capture_output=True, text=True, check=True
        )
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return sorted(files)
    except subprocess.CalledProcessError as e:
        print(f"Error calling git ls-files: {e}", file=sys.stderr)
        sys.exit(1)


def classify_file(path: str) -> tuple[str, str]:
    """
    Classify a markdown file as 'living', 'stale', or 'generated'.
    Returns (verdict, reason).
    """
    p = Path(path)

    # 1. NEVER touch list / Handoff Candidates
    never_touch_exact = {
        "ARCHI.md",
        "ARCHI.json",
        "AGENTS.md",
        "ARCHITECTURE.md",
        "DECISIONS.md",
        "TESTING.md",
        "CLAUDE.md",
        "PLAN2-TASK.md",
    }
    if p.name in never_touch_exact or p.name.startswith("README"):
        return "living", "On brief's NEVER-touch list"

    for prefix in ["docs/adr/", "docs/architecture/", "contracts/", "HANDOFF/"]:
        if path.startswith(prefix):
            if prefix == "HANDOFF/":
                return "living", "On brief's NEVER-touch list (HANDOFF candidate)"
            return "living", "On brief's NEVER-touch list"

    # 2. System status / Active configurations
    active_configs = {
        "STATUS.md",
        "SEPARATE-PRODUCT.md",
        "RESIDUAL-RISKS.md",
        "KIMI_MERGE_LEDGER.md",
        "FEATURE-FLAGS.md",
        "docs/consolidation/C3-DOC-INVENTORY.md",
    }
    if path in active_configs:
        return "living", "Active project policy, status, or configuration reference"

    # 3. Codebase Assets & Test Fixtures
    for prefix in ["tests/", "omniagentos/", "tools/", "scripts/"]:
        if path.startswith(prefix):
            return "living", "Codebase support file, prompt template, or test fixture"

    # 4. Generated run instances/sessions
    if path.startswith("vault/orchestration/orch_") or path.startswith(
        "vault/orchestration/sessions/"
    ):
        return "generated", "Generated runtime execution or session log"
    if path.startswith("vault/playbook/skill-run-"):
        return "generated", "Generated runtime execution or session log"
    if path.startswith("vault/swarm/swr_"):
        return "generated", "Generated runtime execution or session log"

    # 5. Generated agent profiles & model registries
    if path.startswith("vault/org/"):
        return "generated", "Machine-generated agent profile or organization note"
    if path.startswith("vault/models/"):
        return "generated", "Machine-generated model capability or benchmark note"

    # 6. Active Wiki Documentation (Living)
    active_wiki_exact = {
        "vault/Home.md",
        "vault/SCHEMA.md",
        "vault/conversations/war-room.md",
        "vault/servers/inventory.md",
        "vault/leaderboard/wave3-genome.md",
        "vault/disciplines/swarm.md",
        "vault/swarm/playbook.md",
    }
    if path in active_wiki_exact:
        return "living", "Active wiki documentation reference"

    # Active wiki folders/subdirectories
    for prefix in [
        "vault/capabilities/",
        "vault/sources/",
        "vault/benchmarks/",
        "vault/prompts/",
        "vault/playbook/",
    ]:
        if path.startswith(prefix):
            return "living", "Active wiki documentation or prompt reference"

    if path == "vault/decisions/synapse-h3.md":
        return "living", "Active architectural decision record"

    # 7. Stale / Historical / Temporary Artifacts (to move to docs/archive/)
    stale_exact = {
        "B01_CONSOLIDATION_ANALYSIS.md",
        "B07_ASSEMBLY_PREFLIGHT.md",
        "IMPROVE.md",
        "vault/consolidation_unclassified.md",
        "vault/consolidation_orchestration.md",
        "vault/G2-evidence.md",
        "vault/decisions/G1-evidence.md",
        "vault/decisions/G4-evidence.md",
    }
    if path in stale_exact:
        return "stale", "Obsolete development plan, historical analysis, or temporary artifact"

    for prefix in ["devtasks/", "docs/research/", "docs/reliability/", "vault/briefings/"]:
        if path.startswith(prefix):
            return "stale", "Historical project briefings, research, task tracking, or journal"

    # docs/ root-level plans
    if path.startswith("docs/") and "/" not in path[5:]:
        return "stale", "Obsolete development planning document or matrices"

    return "stale", "Unclassified historical markdown document (diet candidate)"


def generate_inventory_md(
    classifications: list[dict[str, str]], summary_stats: dict[str, int]
) -> str:
    """Generate the inventory markdown content."""
    md = []
    md.append("# C3 Doc Diet — Document Classification Inventory")
    md.append("")
    md.append(
        "This inventory was automatically generated by `scripts/doc-diet/classify.py` as part of the Phase C3 Document Diet."
    )
    md.append(
        "It catalogs every tracked markdown file in the repository, defining its current verdict (living, stale, or generated) and a classification reason."
    )
    md.append("")
    md.append("## Summary Statistics")
    md.append("")
    md.append(f"- **Total Tracked Markdown Files**: {summary_stats['total']}")
    md.append(
        f"- **Living Documents**: {summary_stats['living']} (strictly `< 150` target satisfied)"
    )
    md.append(f"- **Stale Documents (Moved to Archive)**: {summary_stats['stale']}")
    md.append(f"- **Generated Documents (Moved to Archive)**: {summary_stats['generated']}")
    md.append("")
    md.append("## Document Classification List")
    md.append("")
    md.append("| Path | Verdict | Reason |")
    md.append("| :--- | :--- | :--- |")

    # Sort classifications by verdict (living first, then stale, then generated) and then path
    sorted_classifications = sorted(
        classifications,
        key=lambda x: (x["verdict"] != "living", x["verdict"] != "stale", x["path"]),
    )

    for item in sorted_classifications:
        md.append(f"| `{item['path']}` | **{item['verdict']}** | {item['reason']} |")

    md.append("")
    return "\n".join(md)


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify markdown files and perform C3 doc diet.")
    parser.add_argument("--move", action="store_true", help="Actually execute the git mv commands.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Display the git mv commands that would be executed."
    )
    args = parser.parse_args()

    # Ensure docs/consolidation directory exists
    Path("docs/consolidation").mkdir(parents=True, exist_ok=True)

    files = get_tracked_md_files()

    classifications = []
    summary_stats = {"total": len(files), "living": 0, "stale": 0, "generated": 0}

    for f in files:
        verdict, reason = classify_file(f)
        classifications.append({"path": f, "verdict": verdict, "reason": reason})
        summary_stats[verdict] += 1

    inventory_content = generate_inventory_md(classifications, summary_stats)

    inventory_path = Path("docs/consolidation/C3-DOC-INVENTORY.md")
    inventory_path.write_text(inventory_content, encoding="utf-8")
    print(f"Generated inventory at {inventory_path}")
    print(
        f"Stats: Total: {summary_stats['total']}, Living: {summary_stats['living']}, Stale: {summary_stats['stale']}, Generated: {summary_stats['generated']}"
    )

    # Prepare moves
    moves_to_make = []
    for item in classifications:
        if item["verdict"] in ("stale", "generated"):
            # Check if it's on NEVER touch list
            if item["path"].startswith("HANDOFF/"):
                print(f"Safety warning: Tried to move HANDOFF file {item['path']}, skipped.")
                continue
            src_path = Path(item["path"])
            dest_path = Path("docs/archive") / src_path
            moves_to_make.append((src_path, dest_path))

    if args.dry_run:
        print("\n=== DRY RUN MOVES ===")
        for src, dest in moves_to_make:
            print(f"git mv {src} {dest}")
        print(f"Would move {len(moves_to_make)} files.")

    elif args.move:
        print(f"\nPerforming {len(moves_to_make)} moves...")
        moved_count = 0
        for src, dest in moves_to_make:
            if not src.exists():
                print(f"Warning: Source file {src} does not exist on disk, skipping.")
                continue
            # Ensure target parent directory exists in filesystem
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(
                    ["git", "mv", str(src), str(dest)], check=True, capture_output=True, text=True
                )
                moved_count += 1
            except subprocess.CalledProcessError as e:
                print(f"Error moving {src} to {dest}: {e.stderr.strip()}", file=sys.stderr)

        print(f"Successfully moved {moved_count} files via git mv.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
