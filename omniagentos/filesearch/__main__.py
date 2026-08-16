"""CLI for file search.

    python -m omniagentos.filesearch "<query>" [--mode keyword|semantic|hybrid] [--scope …] [--limit N]
    python -m omniagentos.filesearch --reindex           # catalog + budgeted deep semantic pass
    python -m omniagentos.filesearch --semantic-index    # ONLY the deep semantic pass

Agents call the search form via Bash to find files anywhere (local, iCloud, Google Drive,
Dropbox) without downloading cloud-only files. ``--reindex`` is what the 2-hourly
``com.omniagentos.filesearch-index`` launchd job runs: the incremental metadata catalog
first, then the deep chunk-level pgvector pass under a time budget (default ~10 min;
a cursor resumes the next cycle where this one stopped).
"""

from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(prog="omniagentos.filesearch", description=__doc__)
    parser.add_argument("query", nargs="?", default="", help="content + filename query")
    parser.add_argument(
        "--mode",
        choices=["keyword", "semantic", "hybrid"],
        default="hybrid",
        help="keyword=Spotlight, semantic=catalog embeddings, hybrid=both (default)",
    )
    parser.add_argument(
        "--scope",
        default="local,icloud",
        help="comma of local,icloud,gdrive,dropbox (keyword/hybrid; default local,icloud)",
    )
    parser.add_argument("--limit", type=int, default=20, help="max results")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="(re)build the metadata catalog, then run the budgeted deep semantic pass",
    )
    parser.add_argument(
        "--semantic-index",
        action="store_true",
        help="run ONLY the budgeted deep semantic pass (pgvector chunk index)",
    )
    parser.add_argument(
        "--semantic-budget",
        type=float,
        default=600.0,
        help="time budget in seconds for the deep semantic pass (default 600)",
    )
    parser.add_argument(
        "--no-semantic",
        action="store_true",
        help="with --reindex: skip the deep semantic pass",
    )
    args = parser.parse_args()

    if args.reindex or args.semantic_index:
        out: dict = {}
        if args.reindex:
            from omniagentos.filesearch.catalog import reindex

            out["catalog"] = reindex()
        if not (args.reindex and args.no_semantic):
            from omniagentos.filesearch.semantic import semantic_index

            out["semantic"] = semantic_index(budget_seconds=args.semantic_budget)
        print(json.dumps(out, indent=2))
        return

    if not args.query.strip():
        parser.error("a query is required (or use --reindex / --semantic-index)")

    from omniagentos.filesearch import render_hits, search

    scopes = [s.strip() for s in args.scope.split(",") if s.strip()]
    print(render_hits(search(args.query, mode=args.mode, scopes=scopes, limit=args.limit)))


if __name__ == "__main__":
    main()
