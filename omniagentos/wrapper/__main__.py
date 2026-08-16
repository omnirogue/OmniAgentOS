"""Command-line entry point for historical execution imports."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from omniagentos.contracts import default_db_path, default_ledger_dir, default_vault_dir
from omniagentos.wrapper.durable import finalize_manifest
from omniagentos.wrapper.importers import import_fusion_run, import_improve_tsv

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    """Import a Fusion directory or improve TSV. Return shell-compatible status."""
    parser = argparse.ArgumentParser(description="Import historical runs into OmniAgentOS")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fusion", metavar="DIR", help="Fusion run directory")
    source.add_argument("--improve", metavar="TSV", help="/improve results.tsv file")
    parser.add_argument("--write-vault", action="store_true", help="Render and write vault notes")
    parser.add_argument("--dry-run", action="store_true", help="Print JSONL without writing")
    store_mode = parser.add_mutually_exclusive_group()
    store_mode.add_argument("--store", dest="write_store", action="store_true", default=True)
    store_mode.add_argument("--no-store", dest="write_store", action="store_false")
    parser.add_argument("--db-path", default=default_db_path(), help="SQLite Store path")
    parser.add_argument("--ledger-dir", default=default_ledger_dir(), help="Ledger directory")
    parser.add_argument("--vault-dir", default=default_vault_dir(), help="Vault directory")
    args = parser.parse_args(argv)

    try:
        manifests = (
            import_fusion_run(args.fusion) if args.fusion else import_improve_tsv(args.improve)
        )
        if args.dry_run:
            for manifest in manifests:
                print(manifest.model_dump_json())
            return 0
        for manifest in manifests:
            finalize_manifest(
                manifest,
                task_title=str(manifest.extra.get("description") or manifest.task_id),
                task_input={"imported": True, "extra": manifest.extra},
                ledger_dir=args.ledger_dir,
                vault_dir=args.vault_dir,
                db_path=args.db_path,
                write_store=args.write_store,
                write_vault=args.write_vault,
            )
        return 0
    except Exception as exc:  # CLI boundary: imports should not leak fatal tracebacks.
        logger.error("Import failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
