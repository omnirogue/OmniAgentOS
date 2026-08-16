"""CLI: ``python -m omniagentos.repomap <repo> [--focus a.py,b.py] [--terms x,y] [--tokens N]``.

Prints a compact, ranked, optionally task-focused map of the repository's symbols.
"""

from __future__ import annotations

import argparse

from omniagentos.repomap.service import build_repo_map


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(prog="omniagentos.repomap", description=__doc__)
    parser.add_argument("repo", help="repository directory to map")
    parser.add_argument(
        "--focus", default="", help="comma-separated files the task touches (biases ranking)"
    )
    parser.add_argument(
        "--terms", default="", help="comma-separated symbols/keywords the task mentions"
    )
    parser.add_argument("--tokens", type=int, default=1024, help="output token budget")
    args = parser.parse_args()
    print(
        build_repo_map(
            args.repo,
            focus_files=_split(args.focus),
            focus_terms=_split(args.terms),
            max_tokens=args.tokens,
        )
    )


if __name__ == "__main__":
    main()
