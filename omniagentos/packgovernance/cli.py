"""Operator CLI for checksum-bound prompt-pack inspection.

This command performs intake and reporting only.  It never executes a pack item
or treats pack prose as instructions.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from omniagentos.packgovernance.checksum import bind_artifact, sha256_file
from omniagentos.packgovernance.contracts import ArtifactKind, Capability, freeze_capabilities
from omniagentos.packgovernance.decision import parse_operator_decision
from omniagentos.packgovernance.packparse import parse_pack


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, type=Path)
    parser.add_argument("--pack-sha256", required=True)
    parser.add_argument("--decision", required=True, type=Path)
    parser.add_argument("--decision-sha256", required=True)
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        choices=[entry.value for entry in Capability],
        help="Declared runtime capability to include in the read-only report.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Bind, parse, and summarize an untrusted pack plus operator authority."""
    args = _parser().parse_args(argv)
    pack_digest = sha256_file(args.pack)
    decision_digest = sha256_file(args.decision)
    pack = parse_pack(
        bind_artifact(
            args.pack,
            kind=ArtifactKind.UNTRUSTED_PACK_FIXTURE,
            declared_sha256=args.pack_sha256,
        )
    )
    decision = parse_operator_decision(
        bind_artifact(
            args.decision,
            kind=ArtifactKind.OPERATOR_AUTHORITY,
            declared_sha256=args.decision_sha256,
        )
    )
    capabilities = freeze_capabilities(Capability(value) for value in args.capability)
    report = {
        "pack": {
            "path": str(args.pack),
            "sha256": pack_digest,
            "declared_repository": pack.declared_repository,
            "item_ids": list(pack.item_ids),
            "injection_signal_count": len(pack.injection_signals),
        },
        "decision": {
            "path": str(args.decision),
            "sha256": decision_digest,
            "target_repository": decision.target_repository,
            "approval_is_exclusive": decision.approval_is_exclusive,
            "dispositions": [
                {"item_id": entry.item_id, "kind": entry.kind.value}
                for entry in decision.dispositions
            ],
            "unclassified_directives": list(decision.unclassified_directives),
        },
        "declared_capabilities": sorted(entry.value for entry in capabilities),
        "execution_performed": False,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
