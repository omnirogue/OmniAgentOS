"""Operator CLI for the vault graph engine.

    python -m omniagentos.vaultgraph stats
    python -m omniagentos.vaultgraph communities [--method label_propagation|connected_components]
    python -m omniagentos.vaultgraph moc [--commit]
    python -m omniagentos.vaultgraph local <note> [--hops N]
    python -m omniagentos.vaultgraph global "<query>"
    python -m omniagentos.vaultgraph suggest <note>

All commands default to the repo vault (``OMNIAGENTOS_VAULT_DIR`` or ``vault/``).
"""

from __future__ import annotations

import argparse

from omniagentos.contracts import default_vault_dir
from omniagentos.vaultgraph.classify import classify_fact
from omniagentos.vaultgraph.community import detect_communities
from omniagentos.vaultgraph.graph import build_graph
from omniagentos.vaultgraph.moc import generate_mocs
from omniagentos.vaultgraph.search import global_search, local_search
from omniagentos.vaultgraph.suggest import propose_links

_METHODS = ("label_propagation", "connected_components")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniagentos.vaultgraph")
    parser.add_argument("--vault", default=default_vault_dir(), help="vault directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="parse the vault and print node/edge counts")

    comm = sub.add_parser("communities", help="detect communities (clusters)")
    comm.add_argument("--method", default="label_propagation", choices=_METHODS)
    comm.add_argument("--min-size", type=int, default=2)

    moc = sub.add_parser("moc", help="generate a Map-of-Content note per community")
    moc.add_argument("--method", default="label_propagation", choices=_METHODS)
    moc.add_argument("--min-size", type=int, default=2)
    moc.add_argument("--commit", action="store_true", help="git-commit the written notes")

    local = sub.add_parser("local", help="print the N-hop neighborhood of a note")
    local.add_argument("note")
    local.add_argument("--hops", type=int, default=1)

    glob = sub.add_parser("global", help="keyword search over generated MOC summaries")
    glob.add_argument("query")

    suggest = sub.add_parser("suggest", help="propose links for unlinked mentions in a note")
    suggest.add_argument("note")

    classify = sub.add_parser("classify", help="classify an incoming fact vs an existing one")
    classify.add_argument("existing")
    classify.add_argument("incoming")

    args = parser.parse_args(argv)

    if args.command == "classify":
        verdict = classify_fact(args.existing, args.incoming)
        print(f"{verdict.label.value}: {verdict.reason}")
        return 0

    if args.command == "global":
        hits = global_search(args.query, args.vault)
        if not hits:
            print("no MOC matches (run `moc` first to generate summaries)")
            return 1
        for hit in hits:
            print(f"[{hit.score:.4f}] {hit.title} ({hit.relpath})")
            print(f"    {hit.snippet}")
        return 0

    with build_graph(args.vault) as graph:
        if args.command == "stats":
            for key, value in graph.stats().items():
                print(f"{key}: {value}")
            return 0

        if args.command == "communities":
            comms = detect_communities(graph, method=args.method, min_size=args.min_size)
            print(f"{len(comms)} communities ({args.method}):")
            for community in comms:
                hubs = ", ".join(community.hubs)
                print(f"  {community.id}  size={community.size}  hubs=[{hubs}]")
            return 0

        if args.command == "moc":
            comms = detect_communities(graph, method=args.method, min_size=args.min_size)
            written = generate_mocs(graph, args.vault, comms, autocommit=args.commit or None)
            print(f"wrote {len(written)} MOC note(s):")
            for relpath in written:
                print(f"  {relpath}")
            return 0

        if args.command == "local":
            hood = local_search(graph, args.note, hops=args.hops)
            if not hood.nodes:
                print(f"note {args.note!r} not found in graph")
                return 1
            print(f"{hood.center} — {len(hood.nodes)} nodes within {hood.hops} hop(s):")
            for neighbor in hood.nodes:
                node = neighbor.node
                tag = "" if node.resolved else " (dangling)"
                print(f"  d{neighbor.distance}  {node.id}  [{node.ntype}]{tag}")
            return 0

        if args.command == "suggest":
            from omniagentos.vaultgraph.parser import slugify_target

            suggestions = propose_links(graph, slugify_target(args.note), vault_dir=args.vault)
            if not suggestions:
                print("no link suggestions")
                return 0
            print(f"{len(suggestions)} suggestion(s) for {args.note} (human approval required):")
            for s in suggestions:
                print(f"  {s.mention!r} -> {s.suggested_markdown}  ({s.reason})")
            return 0

    return 0  # pragma: no cover - unreachable (subparsers are required)


if __name__ == "__main__":
    raise SystemExit(main())
