"""CLI entrypoints for omniagentos.selfimprove.

  python -m omniagentos.selfimprove capture-skill --run-dir <dir> \\
      --metadata <skill-metadata.json> [--vault-dir <dir>] [--skills-dir <dir>] \\
      [--autocommit]
  python -m omniagentos.selfimprove append-constraint --run-dir <dir> \\
      --project <name> --rule <text> [--constraints-dir <dir>]

Both subcommands read the `VerificationGate` from `<run-dir>/status.json`
(the shared Fusion worker status schema — see
`omniagentos.selfimprove.gate`) and enforce the HARD RULE: a gate that has
not passed exits 1 with an error and writes nothing.

Manual/scripted use only for now — this CLI (and the package as a whole) is
NOT wired into the live fusion-gate yet; see the package docstring.
"""

from __future__ import annotations

import argparse
import json
import sys

from omniagentos.contracts import default_vault_dir
from omniagentos.selfimprove.constraints import append_constraint_from_run_dir
from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.models import SkillMetadata
from omniagentos.selfimprove.skills import capture_skill_from_run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniagentos.selfimprove")
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser(
        "capture-skill", help="capture a verified run/workflow as a reusable skill note"
    )
    cap.add_argument("--run-dir", required=True, help="Fusion session dir with status.json")
    cap.add_argument("--metadata", required=True, help="path to a SkillMetadata JSON file")
    cap.add_argument("--vault-dir", default=default_vault_dir())
    cap.add_argument("--skills-dir", default=None, help="optional SKILL.md mirror dir")
    cap.add_argument("--autocommit", action="store_true")

    con = sub.add_parser(
        "append-constraint", help="append a verification-fix rule to a project's CONSTRAINTS.md"
    )
    con.add_argument("--run-dir", required=True, help="Fusion session dir with status.json")
    con.add_argument("--project", required=True)
    con.add_argument("--rule", required=True)
    con.add_argument("--constraints-dir", default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "capture-skill":
            metadata = SkillMetadata.model_validate_json(_read_file(args.metadata))
            result = capture_skill_from_run_dir(
                args.run_dir,
                metadata,
                args.vault_dir,
                skills_dir=args.skills_dir,
                autocommit=True if args.autocommit else None,
            )
            print(json.dumps(result.model_dump(), indent=2))
            return 0

        path = append_constraint_from_run_dir(
            args.run_dir, args.project, args.rule, constraints_dir=args.constraints_dir
        )
        print(str(path))
        return 0
    except UnverifiedCaptureError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    raise SystemExit(main())
