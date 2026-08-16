"""CLI for unified skill, tool, and capability search."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from omniagentos.semsearch.constants import MAX_QUERY_LENGTH, MAX_RESULT_COUNT
from omniagentos.semsearch.index import reindex
from omniagentos.semsearch.search import search
from omniagentos.semsearch.store import ALL_KINDS


def _bounded_target(value: str) -> str:
    if len(value.strip()) > MAX_QUERY_LENGTH:
        raise argparse.ArgumentTypeError(
            f"query must contain at most {MAX_QUERY_LENGTH} characters"
        )
    return value


def _bounded_limit(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= MAX_RESULT_COUNT:
        raise argparse.ArgumentTypeError(f"limit must be between 0 and {MAX_RESULT_COUNT}")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.semsearch",
        description="Semantic search over skills, tools, and capabilities.",
    )
    parser.add_argument("target", type=_bounded_target, help="query text, or 'reindex'")
    parser.add_argument("--kind", choices=(*ALL_KINDS, "all"), default="all")
    parser.add_argument("--limit", type=_bounded_limit, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and return a shell-compatible exit status."""
    args = _parser().parse_args(argv)
    if args.target == "reindex":
        kinds = ALL_KINDS if args.kind == "all" else (args.kind,)
        try:
            summary = reindex(kinds)
        except Exception as exc:
            print(f"reindex failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({kind: asdict(stats) for kind, stats in summary.items()}, sort_keys=True))
        return 0

    hits = search(args.target, kind=args.kind, limit=args.limit)
    print(json.dumps([asdict(hit) for hit in hits], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
