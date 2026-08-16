"""Side-effect-free knowledge recall for briefs assembled outside the runner."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from omniagentos.knowledge.config import knowledge_enabled


def compose_prompt(task_summary: str, paths: Sequence[str] = (), role: str | None = None) -> str:
    """Build the stable recall query used by external brief producers."""
    lines = ["Task summary:", task_summary.strip()]
    if paths:
        lines.extend(["", "Owned paths:", *(f"- {path}" for path in paths)])
    if role:
        lines.extend(["", "Role:", role.strip()])
    return "\n".join(lines)


def recall_lessons(
    task_summary: str,
    *,
    paths: Sequence[str] = (),
    role: str | None = None,
    budget_tokens: int | None = None,
) -> str:
    """Return a rendered recall block without recording runner feedback."""
    from omniagentos.knowledge.recall import _get_store, recall, render_recall_block

    result = recall(
        _get_store(),
        prompt=compose_prompt(task_summary, paths, role),
        # `role` is a prompt-text hint, not a knowledge discipline -- the
        # discipline taxonomy is unrelated to the builder ROLE constant.
        discipline=None,
        budget_tokens=budget_tokens,
        run_id=None,
    )
    return render_recall_block(result, budget_tokens=budget_tokens) or ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-summary", required=True)
    parser.add_argument(
        "--paths",
        action="append",
        default=None,
        help="Owned path (repeatable); each value may also be comma-separated",
    )
    parser.add_argument("--role")
    parser.add_argument("--budget-tokens", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not knowledge_enabled():
        print("knowledge subsystem is disabled", file=sys.stderr)
        return 3
    raw_paths = args.paths or []
    paths = tuple(part.strip() for value in raw_paths for part in value.split(",") if part.strip())
    try:
        block = recall_lessons(
            task_summary=args.task_summary,
            paths=paths,
            role=args.role,
            budget_tokens=args.budget_tokens,
        )
    except Exception as exc:  # CLI convention: unavailable store is exit 2, never fake data.
        print(f"brief recall unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if block:
        sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
