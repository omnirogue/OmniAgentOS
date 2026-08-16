"""Operator CLI for the system-prompt registry.

    .venv/bin/python -m omniagentos.prompts list
    .venv/bin/python -m omniagentos.prompts show job.implementer
    .venv/bin/python -m omniagentos.prompts check

This is the entry point ``system-prompts/README.md`` tells the operator to run, and
the production caller of :func:`omniagentos.prompts.registry.get_prompt` and
friends.  Every failure is printed as the loader worded it — the loader's messages
already name the file, the field, and the remedy, so the CLI does not paraphrase
them into something vaguer.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from omniagentos.prompts.registry import (
    KINDS,
    PromptRegistryError,
    get_prompt,
    get_role,
    list_roles,
    load_registry,
)


def _cmd_list(args: argparse.Namespace) -> int:
    roles = list_roles(live_only=args.live, kind=args.kind)
    if not roles:
        # An empty result is a real answer, not a silent success.
        print("no roles match that filter", file=sys.stderr)
        return 1
    width = max(len(role.id) for role in roles)
    for role in roles:
        flag = "live" if role.live else "    "
        where = role.prompt_file or role.source_ref or "?"
        print(f"{flag}  {role.id:<{width}}  {role.location:<8}  {where}")
    print(f"\n{len(roles)} role(s). Read one with: python -m omniagentos.prompts show <id>")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    entry = get_role(args.role_id)
    if args.meta:
        print(f"id:          {entry.id}")
        print(f"description: {entry.description}")
        print(f"owner:       {entry.owner}")
        print(f"version:     {entry.version}")
        print(f"live:        {entry.live}")
        print(f"kind:        {entry.kind}")
        print(f"location:    {entry.location}")
        print(f"path:        {entry.prompt_file or entry.source_ref}")
        for consumer in entry.consumers:
            print(f"consumer:    {consumer}")
        if entry.notes:
            print(f"notes:       {entry.notes}")
        return 0
    sys.stdout.write(get_prompt(args.role_id))
    return 0


def _cmd_check(_args: argparse.Namespace) -> int:
    registry = load_registry()
    registry.validate_files()
    resolvable = sum(1 for entry in registry if entry.resolvable)
    print(
        f"OK — {len(registry)} role(s) in {registry.path.name}; "
        f"all {resolvable} resolvable prompt file(s) exist and hold text."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run one subcommand."""
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.prompts",
        description="Inspect the system-prompt role registry (system-prompts/).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list every registered role")
    p_list.add_argument("--live", action="store_true", help="only roles in use today")
    p_list.add_argument("--kind", choices=sorted(KINDS), help="filter by kind")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="print one role's prompt text")
    p_show.add_argument("role_id")
    p_show.add_argument(
        "--meta",
        action="store_true",
        help="print the registry entry (owner, version, consumers) instead of the prompt",
    )
    p_show.set_defaults(func=_cmd_show)

    p_check = sub.add_parser(
        "check", help="verify every registry entry still points at real, non-empty text"
    )
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PromptRegistryError as exc:
        # The loader's own wording names the file, the field, and the fix.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
