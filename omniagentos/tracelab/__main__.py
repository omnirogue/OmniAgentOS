"""CLI for tracelab.

    python -m omniagentos.tracelab scan --corpus <dir> [--corpus <dir> ...]
        [--own] [--own-transcripts N] [--max-per-source N]
        [--adapters name,name] [--out var/tracelab] [--emit-db] [--db PATH]

``scan`` streams every recognized trace source, computes deterministic
metrics, mines corpus-level contrasts, generates threshold-gated improvement
hypotheses, and writes a markdown report + machine-readable JSON. With
``--emit-db`` the hypotheses are upserted as ``proposed`` rows into the
improvement lane's ``configtest_hypotheses`` table (migration 083).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from itertools import chain
from pathlib import Path

from omniagentos.tracelab.events import Trace
from omniagentos.tracelab.hypotheses import (
    emit_to_improvement_lane,
    generate_hypotheses,
    write_hypotheses_json,
)
from omniagentos.tracelab.mine import mine_traces
from omniagentos.tracelab.report import write_report
from omniagentos.tracelab.scan import scan_corpus, scan_own


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniagentos.tracelab", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="scan corpora + own telemetry into a report")
    scan_parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        type=Path,
        help="corpus root to scan (repeatable), e.g. .../AI-Traces/HF-Datasets",
    )
    scan_parser.add_argument(
        "--own", action="store_true", help="include OmniAgentOS's own telemetry"
    )
    scan_parser.add_argument(
        "--own-transcripts",
        type=int,
        default=25,
        help="how many recent own Claude Code transcripts to include (default 25)",
    )
    scan_parser.add_argument(
        "--max-per-source",
        type=int,
        default=None,
        help="cap traces read per source file (bounds giant parquet shards)",
    )
    scan_parser.add_argument(
        "--adapters",
        default="",
        help="comma-separated adapter allowlist (default: all)",
    )
    scan_parser.add_argument(
        "--out", type=Path, default=Path("var/tracelab"), help="output directory"
    )
    scan_parser.add_argument(
        "--emit-db",
        action="store_true",
        help="upsert hypotheses into configtest_hypotheses (improvement lane)",
    )
    scan_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="sqlite db path (default: contracts.default_db_path(), honors OMNIAGENTOS_DB)",
    )
    args = parser.parse_args(argv)
    if getattr(args, "db", None) is None:
        from omniagentos.contracts import default_db_path

        args.db = Path(default_db_path())

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    adapter_names = {name.strip() for name in args.adapters.split(",") if name.strip()} or None

    streams: list[Iterator[Trace]] = []
    if args.corpus:
        streams.append(
            scan_corpus(
                args.corpus,
                max_per_source=args.max_per_source,
                adapter_names=adapter_names,
            )
        )
    if args.own:
        streams.append(
            scan_own(
                transcript_limit=args.own_transcripts,
                max_per_source=args.max_per_source,
            )
        )
    if not streams:
        parser.error("nothing scanned — pass --corpus and/or --own")

    result = mine_traces(chain.from_iterable(streams))
    if result.n_traces == 0:
        parser.error("no traces parsed from the given sources")
    hypotheses = generate_hypotheses(result)
    report_path = write_report(result, hypotheses, args.out)
    hypotheses_path = write_hypotheses_json(hypotheses, args.out)

    print(f"traces: {result.n_traces} ({result.n_labeled} labeled)")
    print(f"hypotheses: {len(hypotheses)}")
    print(f"report: {report_path}")
    print(f"hypotheses json: {hypotheses_path}")

    if args.emit_db:
        if not args.db.exists():
            print(f"db missing, skipped emit: {args.db}")
            return 1
        written = emit_to_improvement_lane(hypotheses, args.db)
        print(f"configtest_hypotheses upserted: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
