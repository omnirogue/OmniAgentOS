"""CLI entrypoint: ``python -m omniagentos.lab.curator [--dry-run]``.

Drives the 2x-daily curation loop (contracts/lab-interfaces.md §L06-curator):
`curate()` against the (optionally overridden) LabStore/ledger/vault, followed
by the offline knowledge consolidator (promotion, dedup, decay, adjudication),
and then an OPTIONAL live Sonnet-high narrative pass
(`omniagentos.lab.curator.agent.run_curation_agent`) that is a no-op unless the
operator opted in via ``OMNIAGENTOS_CURATOR_LIVE_AGENT=1`` -- a bare
``--dry-run`` (or a CI run) never attempts a live model call.

`scripts/curator/run.sh` installs a launchd job whose ``ProgramArguments``
invoke exactly this module (2x/day) with the venv python, no ``--dry-run``, so
the scheduled run actually persists leaderboard/log-book + playbook notes.
"""

from __future__ import annotations

import argparse
import json
import logging

from omniagentos.contracts import default_db_path, default_ledger_dir, default_vault_dir
from omniagentos.knowledge.config import admin_dsn, knowledge_enabled
from omniagentos.knowledge.consolidate import consolidate
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.lab.curator.agent import run_curation_agent
from omniagentos.lab.curator.curate import curate
from omniagentos.lab.curator.prompt import curation_prompt
from omniagentos.lab.db import LabStore

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="recompute and print without writing anything"
    )
    parser.add_argument("--db-path", default=None, help="override the lab db path")
    parser.add_argument("--ledger-dir", default=None, help="override the run ledger directory")
    parser.add_argument("--vault-dir", default=None, help="override the vault directory")
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    ledger_dir = args.ledger_dir or default_ledger_dir()
    vault_dir = args.vault_dir or default_vault_dir()

    store = LabStore(db_path)
    summary = curate(store, ledger_dir, vault_dir, dry_run=args.dry_run)

    # Run knowledge consolidation (promotion, dedup, decay, adjudication) if enabled
    consolidation_result = None
    if knowledge_enabled():
        try:
            admin_dsn_val = admin_dsn()
            if admin_dsn_val:
                # Use the SAME embedder recall queries in (real Ollama), NOT a fake one:
                # consolidate() re-embeds promoted facts + backfills, and writing a fake
                # hash vector into an active fact would put it in a different vector space
                # than recall's Ollama queries, silently degrading recall (re-review).
                from omniagentos.knowledge.embeddings import OllamaEmbedding

                embedder = OllamaEmbedding()
                with KnowledgeStore(dsn=admin_dsn_val, embedder=embedder) as kb_store:
                    consolidation_result = consolidate(kb_store, vault_dir, dry_run=args.dry_run)
                    summary["consolidation"] = consolidation_result
            else:
                logger.warning(
                    "Knowledge consolidation enabled but OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN not set; skipping"
                )
        except Exception as e:
            logger.exception(f"Knowledge consolidation failed: {e}")
            summary["consolidation_error"] = str(e)

    prompt = curation_prompt(summary)
    result = run_curation_agent(prompt, dry_run=args.dry_run)
    if result is not None:
        summary = {
            **summary,
            "agent_status": result.status.value,
            "agent_narrative": result.output_text,
        }

    print(json.dumps(summary, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
