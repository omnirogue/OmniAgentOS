"""CLI: python -m omniagentos.agentless '<task>' --repo DIR --tests 'pytest -q' [...]

Prints the selected diff to stdout (or a JSON report with ``--json``). Exit code
0 when a candidate was selected, 3 when none passed verification — so callers
(shell scripts, CI) can branch on Agentless success without parsing output.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from omniagentos.agentless.pipeline import run_agentless

EXIT_SELECTED = 0
EXIT_NO_CANDIDATE = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.agentless",
        description="Localize -> sample N candidate patches -> select by tests.",
    )
    parser.add_argument("task", help="Natural-language description of the fix needed.")
    parser.add_argument("--repo", required=True, help="Path to the repository to patch.")
    parser.add_argument("--tests", required=True, help="Shell command that runs the test suite.")
    parser.add_argument("--n", type=int, default=4, help="Number of candidate samples.")
    parser.add_argument("--adapter", default="cli-claude", help="Harness/adapter name.")
    parser.add_argument("--model", default=None, help="Model override, if the adapter supports it.")
    parser.add_argument("--batch", type=int, default=2, help="Samples generated per batch.")
    parser.add_argument(
        "--json", action="store_true", help="Emit a full JSON report instead of a bare diff."
    )
    return parser


def _result_to_json(result: object) -> dict[str, object]:
    return dataclasses.asdict(result)  # type: ignore[call-overload]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    result = run_agentless(
        args.task,
        args.repo,
        args.tests,
        n=args.n,
        adapter=args.adapter,
        model=args.model,
        batch_size=args.batch,
    )

    if args.json:
        print(json.dumps(_result_to_json(result), indent=2, default=str))
    elif result.selected is not None:
        diff = result.selected.patch.diff or ""
        print(diff, end="" if diff.endswith("\n") else "\n")
    else:
        print(f"No candidate passed verification: {result.selection_reason}", file=sys.stderr)

    return EXIT_SELECTED if result.selected is not None else EXIT_NO_CANDIDATE


if __name__ == "__main__":
    raise SystemExit(main())
