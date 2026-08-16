"""CLI for the model-intelligence subsystem.

  python -m omniagentos.modelintel update [--no-research]
  python -m omniagentos.modelintel route --task "..." [--mode fusionbuild]
      [--difficulty moderate] [--reasoning medium] [--mechanical]
  python -m omniagentos.modelintel digest

`route` prints a RouteVerdict JSON and exits 0 when a pick exists, 1 otherwise
(same convention as ~/.claude/skills/fusion/scripts/route-task.py).
"""

from __future__ import annotations

import argparse
import json
import sys

from omniagentos.modelintel import daily
from omniagentos.modelintel.config import FUSION_DIGEST
from omniagentos.modelintel.router import DIFFICULTIES, EFFORTS, MODES, route


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniagentos.modelintel")
    sub = parser.add_subparsers(dest="command", required=True)

    update = sub.add_parser("update", help="run the daily fetch/research/registry/vault update")
    update.add_argument("--no-research", action="store_true", help="skip the Grok web sweep")

    router = sub.add_parser("route", help="pick the best agent for a task")
    router.add_argument("--task", required=True)
    router.add_argument("--mode", default="fusionbuild", choices=MODES)
    router.add_argument("--difficulty", default="moderate", choices=DIFFICULTIES)
    router.add_argument("--reasoning", default="medium", choices=EFFORTS)
    router.add_argument(
        "--mechanical", action="store_true", help="skip the LLM, score mechanically"
    )

    sub.add_parser("digest", help="print the fusion digest (~/.claude/fusion/model-intel.json)")

    args = parser.parse_args(argv)
    if args.command == "update":
        print(json.dumps(daily.run(research_enabled=not args.no_research), indent=2))
        return 0
    if args.command == "route":
        verdict = route(
            task=args.task,
            mode=args.mode,
            difficulty=args.difficulty,
            reasoning=args.reasoning,
            force_mechanical=args.mechanical,
        )
        print(verdict.model_dump_json(indent=2))
        return 0 if verdict.pick else 1
    if args.command == "digest":
        if not FUSION_DIGEST.is_file():
            print(f"error: {FUSION_DIGEST} missing — run `update` first", file=sys.stderr)
            return 2
        print(FUSION_DIGEST.read_text(encoding="utf-8"), end="")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
