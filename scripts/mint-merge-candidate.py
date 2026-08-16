#!/usr/bin/env python3
"""CLI entrypoint that wires mint_candidate_receipt for production reachability.

merge-gate and operators call this (or ``python -m omniagentos.scheduler.gate_evidence
mint-candidate``). The dedicated script ensures the public mint symbol has a
production caller outside its defining module (GLA-11 / reachability-gate).
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    from omniagentos.scheduler.gate_evidence import (
        GateEvidenceError,
        mint_candidate_receipt,
    )

    parser = argparse.ArgumentParser(description="Mint a signed merge-candidate receipt")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--merge-base-sha", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--command",
        default=(
            "pytest tests/scheduler/test_gate_evidence.py"
            "::test_normalize_gate_command_is_stable_across_formatting"
        ),
    )
    args = parser.parse_args(argv)
    try:
        path = mint_candidate_receipt(
            candidate_sha=args.candidate_sha,
            merge_base_sha=args.merge_base_sha,
            evidence_root=args.evidence_root,
            workspace=args.workspace,
            command=args.command,
        )
    except GateEvidenceError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
