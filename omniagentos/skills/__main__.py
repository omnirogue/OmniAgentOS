"""Operator CLI for the skill library.

    .venv/bin/python -m omniagentos.skills export --out skills-lib/
    .venv/bin/python -m omniagentos.skills export --out skills-lib/ --json

``export`` writes every servable skill (the statuses
:mod:`omniagentos.skills.resolve` will actually serve) to
``<out>/<slug>/SKILL.md``, dropping anything that fails digest verification or
the content scanner. Dropped skills are printed — an export that silently
exported fewer skills than the library holds would read as a clean run.

The exit code is 0 whenever the export completed, including when skills were
dropped: a quarantined or tampered skill is a normal, reportable outcome, not a
tool failure. Use ``--strict`` to make any drop a non-zero exit for a cron that
wants to be paged about it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from omniagentos.skills.export import EXPORTABLE_STATUSES, export_skills


def _cmd_export(args: argparse.Namespace) -> int:
    report = export_skills(
        args.out,
        args.status or EXPORTABLE_STATUSES,
        database=args.db,
        prune=not args.no_prune,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "out_dir": str(report.out_dir),
                    "exported": [
                        {
                            "slug": item.slug,
                            "version": item.version,
                            "status": item.status,
                            "content_digest": item.content_digest,
                            "path": str(item.path),
                            "changed": item.changed,
                        }
                        for item in report.exported
                    ],
                    "skipped": [
                        {
                            "slug": item.slug,
                            "version": item.version,
                            "reason": item.reason,
                            "detail": item.detail,
                        }
                        for item in report.skipped
                    ],
                    "removed": list(report.removed),
                    "changed_count": report.changed_count,
                },
                indent=1,
                sort_keys=True,
            )
        )
    else:
        for written in report.exported:
            if written.changed:
                print(f"  wrote    {written.slug}@{written.version}")
        for slug in report.removed:
            print(f"  removed  {slug}")
        for dropped in report.skipped:
            print(
                f"  DROPPED  {dropped.slug}@{dropped.version}: {dropped.reason} — {dropped.detail}"
            )
        print(
            f"{len(report.exported)} skill(s) exported to {report.out_dir} "
            f"({report.changed_count} changed, {len(report.skipped)} dropped)"
        )
    return 1 if (args.strict and report.skipped) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run one subcommand."""
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.skills",
        description="Operate on the skill library (skills / skill_versions).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="export servable skills to <out>/<slug>/SKILL.md")
    p_export.add_argument("--out", required=True, help="output directory, e.g. skills-lib/")
    p_export.add_argument(
        "--status",
        action="append",
        choices=list(EXPORTABLE_STATUSES),
        help=(f"export only this status (repeatable); default: {', '.join(EXPORTABLE_STATUSES)}"),
    )
    p_export.add_argument("--db", help="database path (default: the configured library DB)")
    p_export.add_argument(
        "--no-prune",
        action="store_true",
        help="keep previously exported dirs that are no longer servable",
    )
    p_export.add_argument("--json", action="store_true", help="print the report as JSON")
    p_export.add_argument(
        "--strict", action="store_true", help="exit non-zero when any skill was dropped"
    )
    p_export.set_defaults(func=_cmd_export)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
